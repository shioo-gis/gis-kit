# /// script
# requires-python = ">=3.11"
# ///
"""Run bounded GIS benchmark cases and report whether a gated case needs a faster stack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

from _command import split_command


def parse_case(value: str) -> tuple[str, list[str]]:
    name, separator, command = value.partition("=")
    if not separator or not name or not command:
        raise argparse.ArgumentTypeError("--case must be NAME=COMMAND")
    return name, split_command(command)


def rss_mb(pid: int) -> float | None:
    if sys.platform == "win32":
        return None
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return None
    try:
        value = int(result.stdout.strip())
        return value / 1024 if value > 0 else None
    except ValueError:
        return None


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_once(name: str, command: list[str], max_seconds: float, max_rss_mb: float | None, poll_seconds: float) -> dict:
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    peak_rss_mb = 0.0
    rss_observed = False
    status = "passed"
    while True:
        current_rss_mb = rss_mb(process.pid)
        if current_rss_mb is not None:
            peak_rss_mb = max(peak_rss_mb, current_rss_mb)
            rss_observed = True
        if process.poll() is not None:
            break
        elapsed = time.perf_counter() - started
        if elapsed > max_seconds:
            status = "timeout"
            stop(process)
            break
        if max_rss_mb is not None and current_rss_mb is not None and current_rss_mb > max_rss_mb:
            status = "rss_limit"
            stop(process)
            break
        time.sleep(poll_seconds)

    elapsed = time.perf_counter() - started
    if status == "passed" and process.returncode != 0:
        status = "failed"
    if status == "passed" and max_rss_mb is not None and not rss_observed:
        status = "rss_unavailable"
    return {
        "name": name,
        "status": status,
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_mb": round(peak_rss_mb, 1) if rss_observed else None,
        "rss_observed": rss_observed,
        "returncode": process.returncode,
    }


def run_case(
    name: str,
    command: list[str],
    max_seconds: float,
    max_rss_mb: float | None,
    poll_seconds: float,
    repeat: int,
) -> dict:
    runs = [run_once(name, command, max_seconds, max_rss_mb, poll_seconds) for _ in range(repeat)]
    elapsed = [run["elapsed_seconds"] for run in runs]
    statuses = {run["status"] for run in runs}
    if "rss_limit" in statuses:
        status = "rss_limit"
    elif "timeout" in statuses:
        status = "timeout"
    elif "failed" in statuses:
        status = "failed"
    elif "rss_unavailable" in statuses:
        status = "rss_unavailable"
    else:
        status = "passed"
    rss_values = [run["peak_rss_mb"] for run in runs if run["peak_rss_mb"] is not None]
    return {
        "name": name,
        "status": status,
        "elapsed_seconds": round(statistics.median(elapsed), 3),
        "median_elapsed_seconds": round(statistics.median(elapsed), 3),
        "range_seconds": [round(min(elapsed), 3), round(max(elapsed), 3)],
        "peak_rss_mb": max(rss_values) if rss_values else None,
        "rss_observed": any(run["rss_observed"] for run in runs),
        "returncode": runs[-1]["returncode"],
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded benchmark commands without extra dependencies.")
    parser.add_argument("--case", action="append", type=parse_case, required=True, metavar="NAME=COMMAND")
    parser.add_argument("--gate", required=True, help="Case whose result decides whether Rust is a candidate")
    parser.add_argument("--max-seconds", type=float, default=900, help="Per-case wall-clock limit (default: 900)")
    parser.add_argument("--max-rss-mb", type=float, help="Per-case RSS limit on macOS/Linux")
    parser.add_argument("--poll-seconds", type=float, default=0.2, help="RSS polling interval (default: 0.2)")
    parser.add_argument("--repeat", type=int, default=3, help="Number of independent runs per case (default: 3)")
    parser.add_argument("--report", type=Path, help="Write JSON summary without commands or child logs")
    args = parser.parse_args()

    names = [name for name, _ in args.case]
    if args.gate not in names:
        sys.exit(f"--gate must name one --case: {', '.join(names)}")
    if args.max_seconds <= 0 or args.poll_seconds <= 0 or args.repeat <= 0 or args.max_rss_mb is not None and args.max_rss_mb <= 0:
        sys.exit("limits must be positive")

    results = [run_case(name, command, args.max_seconds, args.max_rss_mb, args.poll_seconds, args.repeat) for name, command in args.case]
    gate = next(result for result in results if result["name"] == args.gate)
    if gate["status"] == "passed":
        decision = "TIER0_SUFFICIENT"
    elif gate["status"] in {"timeout", "rss_limit"}:
        decision = "RUST_CANDIDATE"
    else:
        decision = "INCONCLUSIVE"
    report = {"decision": decision, "gate": args.gate, "repeat": args.repeat, "rss_scope": "sampled child process RSS; null means unavailable", "results": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if decision == "RUST_CANDIDATE":
        sys.exit(2)
    if decision == "INCONCLUSIVE" or any(result["status"] != "passed" for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
