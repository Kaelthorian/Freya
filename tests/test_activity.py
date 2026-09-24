"""Timestamp-derived orchestration activity and non-additive performance summaries."""
import unittest

from control_center.activity import build_orchestration_activity


class ActivityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "id": "run-1", "status": "Success",
            "created_at": "2026-09-24T10:00:00.000+00:00",
            "updated_at": "2026-09-24T10:00:20.000+00:00",
            "planning_metrics": {},
        }

    def project(self, events, **kwargs):
        return build_orchestration_activity(
            self.run, events, now="2026-09-24T10:00:20.000+00:00", **kwargs,
        )

    def test_system_actors_appear_without_agent_ids(self):
        events = [
            {"id": "1", "timestamp": "2026-09-24T10:00:01+00:00",
             "event_type": "task_analysis.started", "status": "Analyzing",
             "agent_id": None, "actor_type": "task_analyst"},
            {"id": "2", "timestamp": "2026-09-24T10:00:03+00:00",
             "event_type": "task_analysis.updated", "status": "Planning",
             "agent_id": None, "actor_type": "task_analyst",
             "metrics": {"mode": "llm", "model_calls": 1}},
            {"id": "3", "timestamp": "2026-09-24T10:00:04+00:00",
             "event_type": "freya.planning.started", "status": "Planning",
             "agent_id": None, "actor_type": "planner"},
            {"id": "4", "timestamp": "2026-09-24T10:00:08+00:00",
             "event_type": "freya.plan.created", "status": "Planned",
             "agent_id": None, "actor_type": "planner", "planning_metrics": {}},
            {"id": "5", "timestamp": "2026-09-24T10:00:20+00:00",
             "event_type": "freya.completed", "status": "Success", "agent_id": None},
        ]
        activity = self.project(events)
        components = {event["component"] for event in activity["events"]}
        self.assertIn("Task Analyst", components)
        self.assertIn("Planner", components)
        self.assertEqual(activity["total_elapsed_seconds"], 20.0)

    def test_phase_duration_uses_persisted_timestamps_to_milliseconds(self):
        activity = self.project([
            {"timestamp": "2026-09-24T10:00:00.000+00:00",
             "event_type": "freya.planning.started", "status": "Planning"},
            {"timestamp": "2026-09-24T10:00:05.500+00:00",
             "event_type": "freya.plan.created", "status": "Planned"},
            {"timestamp": "2026-09-24T10:00:20+00:00",
             "event_type": "freya.completed", "status": "Success"},
        ])
        planner = next(item for item in activity["phases"] if item["name"] == "planning")
        self.assertEqual(planner["duration_seconds"], 5.5)
        self.assertEqual(planner["status"], "Success")

    def test_clarification_and_approval_waits_are_separate_from_processing(self):
        activity = self.project([
            {"timestamp": "2026-09-24T10:00:02+00:00",
             "event_type": "task_analysis.clarification_required", "status": "NeedsClarification"},
            {"timestamp": "2026-09-24T10:00:08+00:00",
             "event_type": "task_analysis.clarification_received", "status": "Analyzing"},
            {"timestamp": "2026-09-24T10:00:09+00:00", "event_type": "approval.requested",
             "approval_id": "approval-1", "task_id": "task-1", "status": "WaitingForApproval"},
            {"timestamp": "2026-09-24T10:00:16+00:00", "event_type": "approval.resolved",
             "approval_id": "approval-1", "task_id": "task-1", "status": "approved_task"},
            {"timestamp": "2026-09-24T10:00:20+00:00",
             "event_type": "freya.completed", "status": "Success"},
        ])
        self.assertEqual(activity["waiting_for_user_seconds"], 6.0)
        self.assertEqual(activity["clarification_waiting_seconds"], 6.0)
        self.assertEqual(activity["waiting_for_approval_seconds"], 7.0)
        self.assertEqual(activity["processing_seconds"], 7.0)
        self.assertEqual(activity["execution_seconds"], 0)

    def test_failed_planner_keeps_its_duration(self):
        self.run["status"] = "Failed"
        activity = self.project([
            {"timestamp": "2026-09-24T10:00:01+00:00",
             "event_type": "freya.planning.started", "status": "Planning"},
            {"timestamp": "2026-09-24T10:00:06.500+00:00",
             "event_type": "freya.planning.failed", "status": "Failed",
             "message": "invalid plan"},
            {"timestamp": "2026-09-24T10:00:07+00:00",
             "event_type": "freya.failed", "status": "Failed"},
        ])
        planner = next(item for item in activity["phases"] if item["name"] == "planning")
        self.assertEqual(planner["duration_seconds"], 5.5)
        self.assertEqual(planner["status"], "Failed")

    def test_cancelled_run_is_terminal_and_keeps_its_status(self):
        self.run["status"] = "Cancelled"
        activity = self.project([])
        orchestration = next(item for item in activity["phases"]
                             if item["name"] == "orchestration")
        terminal = next(item for item in activity["events"]
                        if item["event_type"] == "freya.cancelled")
        self.assertEqual(orchestration["status"], "Cancelled")
        self.assertEqual(orchestration["duration_seconds"], 20.0)
        self.assertEqual(terminal["status"], "Cancelled")

    def test_llm_metrics_are_aggregated_from_component_and_runtime_calls(self):
        events = [
            {"timestamp": "2026-09-24T10:00:01+00:00", "event_type": "task_analysis.updated",
             "metrics": {"model_calls": 1, "prompt_tokens": 10, "generated_tokens": 4,
                         "model_call_details": [{"prompt_tokens": 10, "generated_tokens": 4,
                                                  "duration_seconds": 2.0,
                                                  "first_token_latency": 0.5, "streaming": False}]}},
            {"timestamp": "2026-09-24T10:00:03+00:00", "event_type": "freya.plan.created",
             "planning_metrics": {"model_calls": 1, "prompt_tokens": 20, "generated_tokens": 8,
                                  "model_call_details": [{"prompt_tokens": 20, "generated_tokens": 8,
                                                           "total_duration": 3.0,
                                                           "first_token_latency": 1.0, "streaming": True}]}},
            {"timestamp": "2026-09-24T10:00:06+00:00", "event_type": "model.finished",
             "model": "local-model", "prompt_tokens": 5, "generated_tokens": 3,
             "duration_seconds": 1.5},
            {"timestamp": "2026-09-24T10:00:20+00:00", "event_type": "freya.completed",
             "status": "Success"},
        ]
        evaluations = [{"metrics": {"model_calls": 1, "prompt_tokens": 2, "generated_tokens": 1,
                                     "model_call_details": [{"prompt_tokens": 2, "generated_tokens": 1,
                                                              "duration_seconds": 0.5}]}}]
        integrations = [{"metrics": {"model_calls": 1, "prompt_tokens": 3, "generated_tokens": 2,
                                      "model_call_details": [{"prompt_tokens": 3, "generated_tokens": 2,
                                                               "duration_seconds": 0.75}]}}]
        llm = self.project(events, evaluations=evaluations, integrations=integrations)["llm"]
        self.assertEqual(llm["calls"], 5)
        self.assertEqual(llm["prompt_tokens"], 40)
        self.assertEqual(llm["generated_tokens"], 18)
        self.assertEqual(llm["total_tokens"], 58)
        self.assertEqual(llm["duration_seconds"], 7.75)
        self.assertEqual(llm["first_token_latency_seconds"], 0.75)

    def test_elapsed_time_is_not_sum_of_overlapping_phase_or_llm_durations(self):
        events = [
            {"timestamp": "2026-09-24T10:00:00+00:00", "event_type": "task.started", "task_id": "a"},
            {"timestamp": "2026-09-24T10:00:00+00:00", "event_type": "task.started", "task_id": "b"},
            {"timestamp": "2026-09-24T10:00:10+00:00", "event_type": "task.success", "task_id": "a"},
            {"timestamp": "2026-09-24T10:00:15+00:00", "event_type": "task.success", "task_id": "b"},
            {"timestamp": "2026-09-24T10:00:20+00:00", "event_type": "freya.completed", "status": "Success"},
        ]
        activity = self.project(events)
        self.assertEqual(activity["total_elapsed_seconds"], 20.0)
        self.assertEqual(activity["execution_seconds"], 15.0)
        self.assertEqual(sum(item.get("duration_seconds", 0) for item in activity["phases"]), 45.0)
        self.assertIn("do not add", activity["duration_semantics"]["phases"])


if __name__ == "__main__":
    unittest.main()
