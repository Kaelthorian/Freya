from __future__ import annotations

import unittest

from hardware import NvidiaSmiMonitor


class HardwareMetricsTests(unittest.TestCase):
    def test_host_telemetry_is_reported_when_nvidia_tool_is_missing(self) -> None:
        monitor = NvidiaSmiMonitor()
        monitor.executable = None
        monitor.samples = [
            {"memory_used_mib": 100.0, "memory_used_percent": 25.0},
            {
                "memory_used_mib": 140.0,
                "memory_used_percent": 35.0,
                "cpu_utilization_percent": 50.0,
            },
        ]

        summary = monitor.stop()

        self.assertFalse(summary["available"])
        self.assertTrue(summary["system_available"])
        self.assertEqual(summary["memory_used_mib_average"], 120.0)
        self.assertEqual(summary["memory_used_mib_peak"], 140.0)
        self.assertEqual(summary["memory_used_percent_peak"], 35.0)
        self.assertEqual(summary["cpu_utilization_percent_average"], 50.0)


if __name__ == "__main__":
    unittest.main()
