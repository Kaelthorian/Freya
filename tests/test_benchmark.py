from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import _append_csv, summarize, warm_up_model


class BenchmarkSummaryTests(unittest.TestCase):
    def test_summary_aggregates_quality_speed_and_hardware(self) -> None:
        record = {
            "success": True,
            "metrics": {
                "wall_seconds": 10,
                "model_api_seconds": 8,
                "load_seconds": 2,
                "agent_steps": 3,
                "tool_calls": 4,
                "tool_errors": 1,
                "test_cases_run": 6,
                "test_cases_passed": 6,
                "prompt_tokens": 200,
                "generated_tokens": 50,
                "total_tokens": 250,
                "generation_seconds": 2,
                "prompt_tokens_per_second": 100,
                "tokens_per_second": 25,
            },
            "hardware": {
                "available": True,
                "gpu_utilization_percent_average": 72.5,
                "vram_used_mib_average": 5900,
                "vram_used_mib_peak": 6100,
                "temperature_c_average": 62,
                "temperature_c_peak": 70,
                "power_watts_average": 120,
                "power_watts_peak": 150,
            },
        }
        summary = summarize("test-model", [record, record])

        self.assertEqual(summary["success_rate"], 1.0)
        self.assertEqual(summary["mean_wall_seconds"], 10)
        self.assertEqual(summary["aggregate_tokens_per_second"], 25)
        self.assertEqual(summary["mean_gpu_utilization_percent"], 72.5)
        self.assertEqual(summary["peak_vram_mib"], 6100)
        self.assertEqual(summary["mean_test_cases_passed"], 6)
        self.assertEqual(summary["mean_tool_errors"], 1)
        self.assertEqual(summary["peak_temperature_c"], 70)
        self.assertEqual(summary["peak_power_watts"], 150)
        self.assertEqual(summary["p95_wall_seconds"], 10)
        self.assertEqual(summary["benchmark_schema_version"], 3)

    def test_warmup_reports_load_separately(self) -> None:
        response = {"total_duration": 2_500_000_000, "load_duration": 1_000_000_000}
        with patch("benchmark.chat", return_value=response):
            warmup = warm_up_model("test-model", host="http://127.0.0.1:11434")
        self.assertEqual(warmup["model_api_seconds"], 2.5)
        self.assertEqual(warmup["load_seconds"], 1.0)

    def test_summary_includes_warmup_metrics(self) -> None:
        summary = summarize("test-model", [], {"wall_seconds": 3.5, "load_seconds": 3.0})
        self.assertEqual(summary["warmup_wall_seconds"], 3.5)
        self.assertEqual(summary["warmup_load_seconds"], 3.0)

    def test_csv_schema_update_preserves_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.csv"
            path.write_text("model,runs\nold-model,1\n", encoding="utf-8")
            _append_csv(path, [summarize("new-model", [], {"wall_seconds": 2.0, "load_seconds": 1.0})])
            with path.open("r", encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))

        self.assertEqual([row["model"] for row in rows], ["old-model", "new-model"])
        self.assertIn("warmup_wall_seconds", rows[1])


if __name__ == "__main__":
    unittest.main()
