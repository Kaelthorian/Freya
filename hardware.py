"""Optional host and NVIDIA telemetry sampler for agent and benchmark runs."""

from __future__ import annotations

import shutil
import subprocess
import os
import threading
import time
from typing import Any


class NvidiaSmiMonitor:
    def __init__(self, interval_seconds: float = 0.5) -> None:
        self.interval_seconds = interval_seconds
        self.executable = shutil.which("nvidia-smi")
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cpu_counters: tuple[int, int] | None = None

    def start(self) -> None:
        self._take_sample()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        fields = {
            "gpu_utilization_percent": "utilization",
            "vram_used_mib": "memory_used_mib",
            "temperature_c": "temperature_c",
            "power_watts": "power_watts",
            "cpu_utilization_percent": "cpu_utilization_percent",
            "memory_used_mib": "memory_used_mib",
            "memory_used_percent": "memory_used_percent",
        }
        summary: dict[str, Any] = {
            "available": any("utilization" in sample for sample in self.samples),
            "system_available": any(
                "memory_used_mib" in sample or "cpu_utilization_percent" in sample
                for sample in self.samples
            ),
            "sample_count": len(self.samples),
            "gpu_sample_count": sum("utilization" in sample for sample in self.samples),
            "system_sample_count": sum("memory_used_mib" in sample for sample in self.samples),
        }
        if not summary["available"]:
            summary["reason"] = (
                "nvidia-smi was not found" if not self.executable
                else "nvidia-smi returned no samples"
            )
        for output_name, sample_key in fields.items():
            values = [sample[sample_key] for sample in self.samples if sample_key in sample]
            if values:
                summary["{}_average".format(output_name)] = round(sum(values) / len(values), 2)
                summary["{}_peak".format(output_name)] = round(max(values), 2)
        return summary

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._take_sample()

    def _take_sample(self) -> None:
        if not self.executable:
            gpu_sample: dict[str, float] = {}
        else:
            gpu_sample = self._take_gpu_sample()
        sample = _system_sample(self)
        sample.update(gpu_sample)
        if sample:
            self.samples.append(sample)

    def _take_gpu_sample(self) -> dict[str, float]:
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "--query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0:
            return {}
        row = (result.stdout or "").splitlines()
        if not row:
            return {}
        values = [part.strip() for part in row[0].split(",")]
        keys = ["utilization", "memory_used_mib", "temperature_c", "power_watts"]
        sample: dict[str, float] = {}
        for key, value in zip(keys, values):
            try:
                sample[key] = float(value)
            except ValueError:
                continue
        return sample


def _system_sample(monitor: NvidiaSmiMonitor) -> dict[str, float]:
    """Read host CPU and memory counters using only the Python standard library."""
    sample: dict[str, float] = {}
    if os.name == "nt":
        try:
            import ctypes

            class FileTime(ctypes.Structure):
                _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

            idle, kernel, user = FileTime(), FileTime(), FileTime()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                counters = (
                    (kernel.high << 32) | kernel.low,
                    (user.high << 32) | user.low,
                )
                idle_ticks = (idle.high << 32) | idle.low
                total_ticks = sum(counters)
                previous = getattr(monitor, "_last_windows_cpu", None)
                if previous:
                    total_delta = total_ticks - previous[0]
                    idle_delta = idle_ticks - previous[1]
                    if total_delta > 0:
                        sample["cpu_utilization_percent"] = max(
                            0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)
                        )
                monitor._last_windows_cpu = (total_ticks, idle_ticks)

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            memory = MemoryStatusEx()
            memory.dwLength = ctypes.sizeof(memory)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) and memory.ullTotalPhys:
                used = memory.ullTotalPhys - memory.ullAvailPhys
                sample["memory_used_mib"] = used / (1024 * 1024)
                sample["memory_used_percent"] = used * 100.0 / memory.ullTotalPhys
        except (AttributeError, OSError, TypeError, ValueError):
            return sample
    elif os.path.exists("/proc/stat") and os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/stat", "r", encoding="ascii") as source:
                cpu_fields = [int(value) for value in source.readline().split()[1:]]
            total_ticks = sum(cpu_fields)
            idle_ticks = cpu_fields[3] + (cpu_fields[4] if len(cpu_fields) > 4 else 0)
            previous = getattr(monitor, "_last_linux_cpu", None)
            if previous:
                total_delta = total_ticks - previous[0]
                idle_delta = idle_ticks - previous[1]
                if total_delta > 0:
                    sample["cpu_utilization_percent"] = max(
                        0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)
                    )
            monitor._last_linux_cpu = (total_ticks, idle_ticks)

            memory_values: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="ascii") as source:
                for line in source:
                    key, _, value = line.partition(":")
                    if key in {"MemTotal", "MemAvailable"}:
                        memory_values[key] = int(value.strip().split()[0])
            total_kib = memory_values.get("MemTotal", 0)
            available_kib = memory_values.get("MemAvailable", 0)
            if total_kib:
                used_kib = total_kib - available_kib
                sample["memory_used_mib"] = used_kib / 1024.0
                sample["memory_used_percent"] = used_kib * 100.0 / total_kib
        except (OSError, ValueError, IndexError):
            return sample
    return sample
