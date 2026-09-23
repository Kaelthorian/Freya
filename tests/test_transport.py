"""Local streamed-Ollama lifecycle and error-classification regressions."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from control_center.transport import (
    TransportError, model_profile, provider_health, request_json,
)
from control_center.planner import OllamaPlanner, Planner
from tests.test_planner import plan


class StreamingOllama:
    def __init__(self, parts=(), *, status=200, header_delay=0, responses=None):
        owner = self
        self.parts = list(parts)
        self.responses = list(responses or [])
        self.status = status
        self.header_delay = header_delay
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(
                    int(self.headers["Content-Length"]))))
                parts = owner.responses.pop(0) if owner.responses else owner.parts
                if owner.header_delay:
                    time.sleep(owner.header_delay)
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    if owner.status != 200:
                        self.wfile.write(b"server error")
                        return
                    for delay, item in parts:
                        time.sleep(delay)
                        self.wfile.write(json.dumps(item).encode() + b"\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def chunk(content="", *, done=False):
    return {"message": {"role": "assistant", "content": content}, "done": done,
            **({"prompt_eval_count": 12, "eval_count": 9, "done_reason": "stop"} if done else {})}


class StreamingTransportTests(unittest.TestCase):
    def request(self, server, *, timeout=.08, hard_timeout=1):
        return request_json("POST", server.url + "/api/chat",
                            {"model": "fixture", "messages": [], "stream": False,
                             "options": {"num_predict": 80}},
                            timeout=timeout, hard_timeout=hard_timeout,
                            component="planner")

    def server(self, parts=(), **kwargs):
        server = StreamingOllama(parts, **kwargs)
        self.addCleanup(server.close)
        return server

    def test_connection_refused_is_unreachable_and_circuit_opens(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        started = time.monotonic()
        with self.assertRaises(TransportError) as raised:
            request_json("POST", url + "/api/chat",
                         {"model": "fixture"}, timeout=.1, component="planner")
        self.assertEqual(raised.exception.code, "OLLAMA_UNREACHABLE")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(provider_health(url)["reachable"])
        with self.assertRaises(TransportError) as next_raised:
            request_json("POST", url + "/api/chat",
                         {"model": "fixture"}, timeout=.1, component="planner")
        self.assertIn("circuit open", str(next_raised.exception))

    def test_connected_server_without_data_is_request_timeout(self):
        server = self.server([(0.25, chunk("late", done=True))])
        with self.assertRaises(TransportError) as raised:
            self.request(server, timeout=.05)
        self.assertEqual(raised.exception.code, "OLLAMA_REQUEST_TIMEOUT")
        self.assertTrue(raised.exception.metrics["connection_established"])
        self.assertEqual(raised.exception.metrics["timeout_type"], "inactivity")

    def test_active_stream_outlives_old_per_request_timeout(self):
        server = self.server([(0.03, chunk('{"a":')), (0.03, chunk("1}")),
                              (0.03, chunk(done=True))])
        result = self.request(server, timeout=.05)
        self.assertEqual(result["message"]["content"], '{"a":1}')
        self.assertGreater(result["_freya_transport"]["total_duration"], .05)
        self.assertEqual(result["_freya_transport"]["generated_tokens"], 9)
        self.assertTrue(server.requests[0]["stream"])

    def test_continuously_active_stream_still_hits_hard_ceiling(self):
        server = self.server([(0.025, chunk("x")) for _ in range(20)])
        with self.assertRaises(TransportError) as raised:
            self.request(server, timeout=.07, hard_timeout=.12)
        self.assertEqual(raised.exception.code, "OLLAMA_GENERATION_TIMEOUT")
        self.assertEqual(raised.exception.metrics["timeout_type"], "hard")
        self.assertTrue(raised.exception.metrics["first_token_received"])
        self.assertNotIn("Could not reach", str(raised.exception))
        self.assertTrue(provider_health(server.url)["reachable"])

    def test_stream_stalling_after_first_token_is_generation_timeout(self):
        server = self.server([(0, chunk("first")), (0.25, chunk(done=True))])
        with self.assertRaises(TransportError) as raised:
            self.request(server, timeout=.05)
        self.assertEqual(raised.exception.code, "OLLAMA_GENERATION_TIMEOUT")
        self.assertEqual(raised.exception.metrics["timeout_type"], "inactivity")
        self.assertTrue(raised.exception.metrics["first_token_received"])
        self.assertTrue(provider_health(server.url)["reachable"])

    def test_incomplete_stream_is_invalid_response(self):
        server = self.server([(0, chunk("partial"))])
        with self.assertRaises(TransportError) as raised:
            self.request(server)
        self.assertEqual(raised.exception.code, "OLLAMA_INVALID_RESPONSE")

    def test_real_http_500_preserves_status(self):
        server = self.server(status=500)
        with self.assertRaises(TransportError) as raised:
            self.request(server)
        self.assertEqual(raised.exception.code, "OLLAMA_HTTP_ERROR")
        self.assertEqual(raised.exception.metrics["http_status"], 500)

    def test_streamed_tool_calls_keep_worker_contract(self):
        item = chunk()
        item["message"]["tool_calls"] = [
            {"function": {"name": "write_file",
                          "arguments": {"path": "hello.txt", "content": "hola mundo"}}}]
        server = self.server([(0, item), (0, chunk(done=True))])
        result = self.request(server)
        self.assertEqual(result["message"]["tool_calls"][0]["function"]["name"], "write_file")
        self.assertTrue(result["_freya_transport"]["first_token_received"])

    def test_streamed_structured_planner_output_keeps_parser_contract(self):
        content = json.dumps(plan(), ensure_ascii=False)
        middle = len(content) // 2
        server = self.server([(0, chunk(content[:middle])),
                              (0, chunk(content[middle:])), (0, chunk(done=True))])
        planner = Planner(OllamaPlanner(model="fixture", endpoint=server.url,
                                       timeout_seconds=1))
        result = planner.create_plan("Complete the user request")
        self.assertEqual(result["tasks"][0]["id"], "task-1")
        self.assertEqual(planner.metrics["model_calls"], 1)
        self.assertTrue(planner.metrics["model_call_details"][0]["streaming"])

    def test_streamed_invalid_plan_still_gets_exactly_one_repair(self):
        content = json.dumps(plan(), ensure_ascii=False)
        server = self.server(responses=[
            [(0, chunk("{bad")), (0, chunk(done=True))],
            [(0, chunk(content)), (0, chunk(done=True))],
        ])
        planner = Planner(OllamaPlanner(model="fixture", endpoint=server.url,
                                       timeout_seconds=1))
        result = planner.create_plan("Complete the user request")
        self.assertEqual(result["tasks"][0]["id"], "task-1")
        self.assertEqual(planner.metrics["model_calls"], 2)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(server.requests[1]["options"]["num_predict"],
                         model_profile("planner").repair_output_tokens)

    def test_profiles_keep_repair_bounded_below_normal_output(self):
        for component in ("task_analyst", "planner", "worker", "evaluator"):
            profile = model_profile(component)
            self.assertLess(profile.repair_output_tokens, profile.max_output_tokens)
            self.assertLess(profile.connect_timeout, profile.hard_timeout)
        # A live calculator plan repair consumed the old 1024-token cap and
        # ended with done_reason=length before completing its JSON object.
        self.assertGreaterEqual(model_profile("planner").repair_output_tokens, 2048)
