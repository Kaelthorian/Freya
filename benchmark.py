"""Repeatable sequential model comparison runner for Mini-Coder."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import DEFAULT_TASK_FILE, DEFAULT_WORKSPACE, PROJECT_ROOT, run_agent
from hardware import NvidiaSmiMonitor
from ollama_client import DEFAULT_MODEL, OllamaError, chat, list_models


DEFAULT_RESULTS = PROJECT_ROOT / "results"


def warm_up_model(model: str, host: str | None = None) -> dict[str, float]:
    """Load a model before measured trials so cached and cold states are comparable."""
    started = time.perf_counter()
    response = chat(
        model,
        [{"role": "user", "content": "Reply with READY."}],
        [],
        host=host,
        temperature=0.0,
    )
    return {
        "wall_seconds": round(time.perf_counter() - started, 4),
        "model_api_seconds": round(int(response.get("total_duration", 0) or 0) / 1_000_000_000, 4),
        "load_seconds": round(int(response.get("load_duration", 0) or 0) / 1_000_000_000, 4),
    }


def summarize(
    model: str,
    records: list[dict[str, Any]],
    warmup: dict[str, float] | None = None,
) -> dict[str, Any]:
    metrics = [record.get("metrics", {}) for record in records]
    successful = sum(1 for record in records if record.get("success"))
    wall_times = [float(metric.get("wall_seconds", 0) or 0) for metric in metrics]
    output_tokens = sum(int(metric.get("generated_tokens", 0) or 0) for metric in metrics)
    generation_seconds = sum(float(metric.get("generation_seconds", 0) or 0) for metric in metrics)
    hardware = [record.get("hardware", {}) for record in records]
    vram_peaks = [
        float(sample["vram_used_mib_peak"])
        for sample in hardware
        if sample.get("available") and "vram_used_mib_peak" in sample
    ]
    gpu_averages = [
        float(sample["gpu_utilization_percent_average"])
        for sample in hardware
        if sample.get("available") and "gpu_utilization_percent_average" in sample
    ]
    def mean_metric(name: str, digits: int = 3) -> float:
        values = [float(metric.get(name, 0) or 0) for metric in metrics]
        return round(statistics.mean(values), digits) if values else 0.0

    def hardware_values(name: str, system: bool = False) -> list[float]:
        availability_field = "system_available" if system else "available"
        return [
            float(sample[name]) for sample in hardware
            if sample.get(availability_field) and name in sample
        ]

    ordered_wall = sorted(wall_times)
    p95_wall = ordered_wall[max(0, int(0.95 * len(ordered_wall) + 0.999999) - 1)] if ordered_wall else 0.0
    temperatures = hardware_values("temperature_c_average")
    power_averages = hardware_values("power_watts_average")
    power_peaks = hardware_values("power_watts_peak")
    vram_averages = hardware_values("vram_used_mib_average")
    temperature_peaks = hardware_values("temperature_c_peak")
    cpu_averages = hardware_values("cpu_utilization_percent_average", system=True)
    cpu_peaks = hardware_values("cpu_utilization_percent_peak", system=True)
    system_memory_averages = hardware_values("memory_used_mib_average", system=True)
    system_memory_peaks = hardware_values("memory_used_mib_peak", system=True)
    system_memory_percent_averages = hardware_values("memory_used_percent_average", system=True)
    system_memory_percent_peaks = hardware_values("memory_used_percent_peak", system=True)
    return {
        "model": model,
        "benchmark_schema_version": 3,
        "warmup_wall_seconds": (warmup or {}).get("wall_seconds"),
        "warmup_load_seconds": (warmup or {}).get("load_seconds"),
        "runs": len(records),
        "successes": successful,
        "success_rate": round(successful / len(records), 4) if records else 0.0,
        "mean_wall_seconds": round(statistics.mean(wall_times), 4) if wall_times else 0.0,
        "median_wall_seconds": round(statistics.median(wall_times), 4) if wall_times else 0.0,
        "min_wall_seconds": round(min(wall_times), 4) if wall_times else 0.0,
        "max_wall_seconds": round(max(wall_times), 4) if wall_times else 0.0,
        "p95_wall_seconds": round(p95_wall, 4),
        "mean_model_api_seconds": mean_metric("model_api_seconds", 4),
        "mean_load_seconds": mean_metric("load_seconds", 4),
        "mean_prompt_eval_seconds": mean_metric("prompt_eval_seconds", 4),
        "mean_generation_seconds": mean_metric("generation_seconds", 4),
        "mean_tool_seconds": mean_metric("tool_seconds", 4),
        "mean_other_seconds": mean_metric("other_seconds", 4),
        "mean_model_calls": mean_metric("model_calls"),
        "mean_agent_steps": mean_metric("agent_steps"),
        "mean_tool_calls": mean_metric("tool_calls"),
        "mean_tool_errors": mean_metric("tool_errors"),
        "mean_test_runs": mean_metric("test_runs"),
        "mean_failed_test_runs": mean_metric("failed_test_runs"),
        "mean_test_cases_run": mean_metric("test_cases_run"),
        "mean_test_cases_passed": mean_metric("test_cases_passed"),
        "mean_test_failures": mean_metric("test_failures"),
        "mean_test_errors": mean_metric("test_errors"),
        "mean_files_changed": mean_metric("files_changed"),
        "mean_file_mutations": mean_metric("file_mutations"),
        "mean_prompt_tokens": mean_metric("prompt_tokens", 2),
        "mean_generated_tokens": mean_metric("generated_tokens", 2),
        "mean_total_tokens": mean_metric("total_tokens", 2),
        "aggregate_tokens_per_second": round(output_tokens / generation_seconds, 3) if generation_seconds else 0.0,
        "mean_prompt_tokens_per_second": mean_metric("prompt_tokens_per_second"),
        "mean_generation_tokens_per_second": mean_metric("tokens_per_second"),
        "mean_gpu_utilization_percent": round(statistics.mean(gpu_averages), 2) if gpu_averages else None,
        "mean_vram_mib": round(statistics.mean(vram_averages), 2) if vram_averages else None,
        "peak_vram_mib": round(max(vram_peaks), 2) if vram_peaks else None,
        "mean_cpu_utilization_percent": round(statistics.mean(cpu_averages), 2) if cpu_averages else None,
        "peak_cpu_utilization_percent": round(max(cpu_peaks), 2) if cpu_peaks else None,
        "mean_system_memory_used_mib": round(statistics.mean(system_memory_averages), 2) if system_memory_averages else None,
        "peak_system_memory_used_mib": round(max(system_memory_peaks), 2) if system_memory_peaks else None,
        "mean_system_memory_used_percent": round(statistics.mean(system_memory_percent_averages), 2) if system_memory_percent_averages else None,
        "peak_system_memory_used_percent": round(max(system_memory_percent_peaks), 2) if system_memory_percent_peaks else None,
        "mean_temperature_c": round(statistics.mean(temperatures), 2) if temperatures else None,
        "peak_temperature_c": round(max(temperature_peaks), 2) if temperature_peaks else None,
        "mean_power_watts": round(statistics.mean(power_averages), 2) if power_averages else None,
        "peak_power_watts": round(max(power_peaks), 2) if power_peaks else None,
    }


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _append_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "recorded_at",
        "model",
        "benchmark_schema_version",
        "warmup_wall_seconds",
        "warmup_load_seconds",
        "runs",
        "successes",
        "success_rate",
        "mean_wall_seconds",
        "median_wall_seconds",
        "min_wall_seconds",
        "max_wall_seconds",
        "p95_wall_seconds",
        "mean_model_api_seconds",
        "mean_load_seconds",
        "mean_prompt_eval_seconds",
        "mean_generation_seconds",
        "mean_tool_seconds",
        "mean_other_seconds",
        "mean_model_calls",
        "mean_agent_steps",
        "mean_tool_calls",
        "mean_tool_errors",
        "mean_test_runs",
        "mean_failed_test_runs",
        "mean_test_cases_run",
        "mean_test_cases_passed",
        "mean_test_failures",
        "mean_test_errors",
        "mean_files_changed",
        "mean_file_mutations",
        "mean_prompt_tokens",
        "mean_generated_tokens",
        "mean_total_tokens",
        "aggregate_tokens_per_second",
        "mean_prompt_tokens_per_second",
        "mean_generation_tokens_per_second",
        "mean_gpu_utilization_percent",
        "mean_vram_mib",
        "peak_vram_mib",
        "mean_cpu_utilization_percent",
        "peak_cpu_utilization_percent",
        "mean_system_memory_used_mib",
        "peak_system_memory_used_mib",
        "mean_system_memory_used_percent",
        "peak_system_memory_used_percent",
        "mean_temperature_c",
        "peak_temperature_c",
        "mean_power_watts",
        "peak_power_watts",
    ]
    existing_rows: list[dict[str, Any]] = []
    existing_fields: list[str] = []
    if path.exists() and path.stat().st_size:
        with path.open("r", encoding="utf-8", newline="") as existing:
            reader = csv.DictReader(existing)
            existing_fields = list(reader.fieldnames or [])
            existing_rows = list(reader)
    all_fields = list(dict.fromkeys(existing_fields + fieldnames))
    needs_header = not path.exists() or path.stat().st_size == 0
    if not needs_header and existing_fields != all_fields:
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8", newline="") as rewritten:
            writer = csv.DictWriter(rewritten, fieldnames=all_fields)
            writer.writeheader()
            writer.writerows(existing_rows)
        os.replace(temporary_path, path)
        needs_header = False
    with path.open("a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=all_fields)
        if needs_header:
            writer.writeheader()
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for record in records:
            writer.writerow({"recorded_at": recorded_at, **record})


def _read_task(task_file: str) -> str:
    path = Path(task_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.read_text(encoding="utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the same coding task repeatedly against local Ollama models.")
    parser.add_argument("--models", nargs="+", default=[DEFAULT_MODEL], help="Installed Ollama model names.")
    parser.add_argument("--runs", type=int, default=5, help="Independent clean-workspace repetitions per model.")
    parser.add_argument("--task-file", default=str(DEFAULT_TASK_FILE))
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--base-url", help="Ollama URL; defaults to OLLAMA_HOST or loopback.")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    args = parser.parse_args(argv)
    if args.runs < 1 or args.runs > 100:
        parser.error("--runs must be between 1 and 100")
    if args.max_steps < 1 or args.max_steps > 100:
        parser.error("--max-steps must be between 1 and 100")

    try:
        task = _read_task(args.task_file)
        available = {item.get("name") for item in list_models(host=args.base_url)}
    except (OSError, OllamaError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2
    missing = [model for model in args.models if model not in available]
    if missing:
        print("Models not installed in Ollama: {}".format(", ".join(missing)), file=sys.stderr)
        print("Install only the models you want to compare, then rerun the benchmark.", file=sys.stderr)
        return 2

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = PROJECT_ROOT / results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    workspace_base = DEFAULT_WORKSPACE / ".benchmark-workspaces"
    workspace_base.mkdir(parents=True, exist_ok=True)
    all_summaries: list[dict[str, Any]] = []
    result_file = results_dir / "benchmark_runs.jsonl"

    for model in args.models:
        records: list[dict[str, Any]] = []
        print("\n=== {} warm-up ===".format(model), flush=True)
        try:
            warmup = warm_up_model(model, host=args.base_url)
        except OllamaError as exc:
            print("Warm-up failed for {}: {}".format(model, exc), file=sys.stderr)
            return 2
        print("  load={:.2f}s warm-up={:.2f}s".format(warmup["load_seconds"], warmup["wall_seconds"]), flush=True)
        print("=== {} ({}/{} measured runs) ===".format(model, args.runs, args.runs), flush=True)
        for run_number in range(1, args.runs + 1):
            print("Run {}/{} ...".format(run_number, args.runs), flush=True)
            monitor = NvidiaSmiMonitor()
            with tempfile.TemporaryDirectory(
                prefix="run-{}-{}-".format(run_number, len(records) + 1),
                dir=str(workspace_base),
            ) as temporary_workspace:
                monitor.start()
                try:
                    record = run_agent(
                        task,
                        model=model,
                        workspace=Path(temporary_workspace),
                        max_steps=args.max_steps,
                        host=args.base_url,
                        validate_final=True,
                    )
                finally:
                    hardware = monitor.stop()
                record["run_number"] = run_number
                record["benchmark_schema_version"] = 3
                record["warmup"] = warmup
                record["hardware"] = hardware
                records.append(record)
                _append_jsonl(result_file, record)
            print("  success={} wall={:.2f}s steps={} tokens={}".format(
                record["success"],
                record["metrics"]["wall_seconds"],
                record["metrics"]["agent_steps"],
                record["metrics"]["generated_tokens"],
            ), flush=True)

        summary = summarize(model, records, warmup)
        all_summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    _append_csv(results_dir / "summary.csv", all_summaries)
    print("\nDetailed runs: {}".format(result_file))
    print("Summary CSV: {}".format(results_dir / "summary.csv"))
    return 0 if all(summary["successes"] == summary["runs"] for summary in all_summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
