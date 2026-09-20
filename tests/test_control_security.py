from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_center.config import normalize_agent, validate_endpoint
from control_center.security import sanitize


class ControlConfigurationTests(unittest.TestCase):
    def test_legacy_identity_columns_stay_synchronized(self):
        agent = normalize_agent({
            "name": "Atlas",
            "role": "Engineer",
            "description": "First description",
            "config": {"identity": {"description": "stale description"}},
        })
        self.assertEqual(agent["config"]["identity"]["description"], "First description")
        updated = normalize_agent({"description": "Updated description"}, agent)
        self.assertEqual(updated["description"], "Updated description")
        self.assertEqual(updated["config"]["identity"]["description"], "Updated description")
    def test_merge_configuration_does_not_reset_tools_or_other_limits(self):
        agent = normalize_agent({"name": "Reader", "tools": ["read_file"], "config": {"max_steps": 8}})
        updated = normalize_agent({"config": {"temperature": 0.7}}, agent)
        self.assertEqual(updated["tools"], ["read_file"])
        self.assertEqual(updated["config"]["max_steps"], 8)
        self.assertEqual(updated["config"]["temperature"], 0.7)

    def test_invalid_permissions_and_limits_rejected(self):
        for config in ({"max_steps": True}, {"max_tokens": -1}, {"temperature": float("nan")},
                       {"allowed_directories": ["../evaluator"]}, {"allowed_directories": ["C:\\"]},
                       {"secret_env": "my-raw-key"}, {"password": "unsafe"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_agent({"name": "A", "config": config})
        with self.assertRaises(ValueError):
            normalize_agent({"name": "A", "tools": ["run_command"]})
        with self.assertRaises(ValueError):
            normalize_agent({"name": "A", "tools": ["web_search"]})

    def test_only_local_base_urls_without_embedded_secrets(self):
        self.assertEqual(validate_endpoint("http://localhost:11434/"), "http://localhost:11434")
        for endpoint in ("http://example.org", "file:///etc/passwd", "http://localhost:11434/api/chat",
                         "http://user:secret@localhost:11434", "http://localhost:11434?key=secret"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_endpoint(endpoint)

    def test_workspace_must_be_an_existing_absolute_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            resolved = str(Path(directory).resolve())
            agent = normalize_agent({"name": "A", "config": {"workspace_path": resolved}})
            self.assertEqual(agent["config"]["workspace_path"], resolved)
            file_path = Path(directory) / "file.txt"
            file_path.write_text("x")
            for value in ("relative/path", str(Path(directory) / "missing"), str(file_path)):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    normalize_agent({"name": "A", "config": {"workspace_path": value}})


class RedactionTests(unittest.TestCase):
    def test_recursive_redaction_preserves_usage_and_operational_reasons(self):
        value = {"input": {"api_key": "abc", "password": "def"}, "reason": "Read configuration",
                 "prompt_tokens": 8, "total_tokens": 9, "secret_env": "ACC_SECRET_TEST",
                 "thinking": "private", "output": "token=hidden; Bearer abcdef"}
        result = sanitize(value)
        self.assertEqual(result["total_tokens"], 9)
        self.assertEqual(result["secret_env"], "ACC_SECRET_TEST")
        self.assertEqual(result["reason"], value["reason"])
        self.assertNotIn("thinking", result)
        self.assertNotIn("hidden", json.dumps(result))
        self.assertNotIn("abcdef", json.dumps(result))
        self.assertEqual(result["input"]["password"], "[REDACTED]")

    def test_bare_configured_secret_and_private_thinking_removed(self):
        with patch.dict(os.environ, {"ACC_SECRET_TEST": "bare-real-secret"}):
            self.assertEqual(sanitize("<think>private</think>Done bare-real-secret"), "Done [REDACTED]")
            self.assertEqual(sanitize("Done <think>unfinished private"), "Done ")


if __name__ == "__main__":
    unittest.main()
