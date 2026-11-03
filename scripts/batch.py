# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
# ]
# ///
"""Batch processing dispatcher using multiprocessing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from multiprocessing import Pool
from pathlib import Path

from _command import show_command, split_command
from _safe_io import validate_vector_file


def build_legacy_command(
    file: Path,
    output_dir: Path,
    script: str,
    extra_args: list[str],
    runner: str,
    overwrite: bool = False,
) -> list[str]:
    out_path = output_dir / file.name
    command = split_command(runner) + [script, str(file)] + extra_args + ["--output", str(out_path)]
    if overwrite:
        command.append("--overwrite")
    return command


def build_template_command(file: Path, output_dir: Path, command_template: str) -> list[str]:
    out_path = output_dir / file.name
    replacements = {
        "input": str(file),
        "input_name": file.name,
        "input_stem": file.stem,
        "input_suffix": file.suffix,
        "input_dir": str(file.parent),
        "output": str(out_path),
        "output_dir": str(output_dir),
    }
    return [part.format(**replacements) for part in split_command(command_template)]


def build_command(
    file: Path,
    output_dir: Path,
    script: str | None,
    extra_args: list[str],
    command_template: str | None,
    runner: str,
    overwrite: bool = False,
) -> list[str]:
    if command_template:
        return build_template_command(file, output_dir, command_template)
    if script:
        return build_legacy_command(file, output_dir, script, extra_args, runner, overwrite=overwrite)
    raise ValueError("Either script or command_template must be provided")


def verify_artifact(path: Path) -> tuple[bool, str, int | None]:
    if not path.exists():
        return False, "output artifact is missing", None
    if path.is_dir():
        if path.suffix.lower() == ".gdb":
            try:
                evidence = validate_vector_file(path)
                return True, "verified", evidence["features"]
            except Exception:
                return False, "output artifact is not readable as a complete vector dataset", None
        return False, "output artifact is an unexpected directory", None
    size = path.stat().st_size
    if size <= 0:
        return False, "output artifact is empty", size
    if path.suffix.lower() in {".gpkg", ".geojson", ".json", ".fgb", ".shp"}:
        try:
            evidence = validate_vector_file(path)
            return True, "verified", evidence["features"]
        except Exception:
            return False, "output artifact is not readable as a complete vector dataset", None
    return True, "present", None


def artifact_fingerprint(path: Path) -> tuple | None:
    if path.is_dir():
        # Directory metadata alone misses in-place updates to FileGDB members.
        members = []
        for child in sorted(path.rglob("*")):
            if child.is_file():
                stat = child.stat()
                members.append((str(child.relative_to(path)), stat.st_ino,
                                stat.st_size, stat.st_mtime_ns))
        return tuple(members)
    if not path.is_file():
        return None
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def run_one(task: tuple[int, int, Path, Path, str | None, list[str], str | None, str, bool]) -> dict:
    idx, total, file, output_dir, script, extra_args, command_template, runner, overwrite = task
    cmd = build_command(file, output_dir, script, extra_args, command_template, runner, overwrite=overwrite)
    output = output_dir / file.name
    started = time.perf_counter()
    process_returncode = None
    artifact_status = "not_checked"
    artifact_features = None
    before = artifact_fingerprint(output)
    try:
        result = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace', timeout=3600,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        process_returncode = result.returncode
        ok = result.returncode == 0
        message = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip() or "process failed")
        artifact_ok, artifact_status, artifact_features = verify_artifact(output)
        if artifact_ok and before is not None and artifact_fingerprint(output) == before:
            artifact_ok = False
            artifact_status = "output artifact was not updated"
        ok = ok and artifact_ok
        if not artifact_ok and process_returncode == 0:
            message = artifact_status
        status = "OK" if ok else "FAIL"
        report_status = "succeeded" if ok else ("process_failed" if process_returncode != 0 else "artifact_failed")
        print(f"  [{idx}/{total}] {status}: {file.name}")
        return {
            "ok": ok,
            "message": message,
            "report": {
                "input": str(file),
                "output": str(output),
                "status": report_status,
                "process_returncode": process_returncode,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "artifact_status": artifact_status,
                "artifact_features": artifact_features,
                "artifact_size_bytes": output.stat().st_size if output.is_file() else None,
            },
        }
    except subprocess.TimeoutExpired:
        print(f"  [{idx}/{total}] TIMEOUT: {file.name}")
        return {"ok": False, "message": "timeout", "report": {
            "input": str(file), "output": str(output), "status": "timeout",
            "process_returncode": None, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "artifact_status": "not_verified_after_timeout", "artifact_features": None,
            "artifact_size_bytes": output.stat().st_size if output.is_file() else None,
        }}
    except Exception as e:
        print(f"  [{idx}/{total}] ERROR: {file.name}")
        return {"ok": False, "message": str(e), "report": {
            "input": str(file), "output": str(output), "status": "execution_error",
            "process_returncode": process_returncode, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "artifact_status": artifact_status, "artifact_features": artifact_features,
            "artifact_size_bytes": output.stat().st_size if output.is_file() else None,
        }}


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Batch processing dispatcher. Use --script for simple single-input GIS scripts, "
            "or --command with placeholders like {input} and {output} for arbitrary commands."
        )
    )
    parser.add_argument("--input-dir", required=True, help="Directory containing input files")
    parser.add_argument("--output-dir", help="Output directory (default: input-dir/output)")
    parser.add_argument("--pattern", default="*.gpkg", help="Glob pattern to match files")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--script", help="Legacy mode: run python3 <script> <input> ... --output <output>")
    mode.add_argument(
        "--command",
        help=(
            "Command template with placeholders. Available: {input}, {output}, {input_name}, "
            "{input_stem}, {input_suffix}, {input_dir}, {output_dir}"
        ),
    )
    parser.add_argument("--runner", default="python3", help="Command prefix for --script mode (default: 'python3')")
    parser.add_argument("--args", default="", help="Additional arguments for --script mode only (quoted string)")
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() - 1), help="Parallel workers")
    parser.add_argument("--dry-run", action="store_true", help="Show commands without running")
    parser.add_argument("--overwrite", action="store_true", help="Pass --overwrite to scripts in legacy --script mode")
    parser.add_argument("--report", type=Path, help="Write a structured JSON report for every input")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        sys.exit(f"Input directory not found: {input_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else input_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    script = str(Path(args.script).resolve()) if args.script else None
    extra_args = split_command(args.args) if args.args else []

    files = sorted(input_dir.glob(args.pattern))
    if not files:
        sys.exit(f"No files matching '{args.pattern}' in {input_dir}")

    total = len(files)
    print(f"Found {total} files, using {args.workers} workers")

    if args.workers <= 0:
        sys.exit("--workers must be positive")

    if args.dry_run:
        for f in files:
            cmd = build_command(f, output_dir, script, extra_args, args.command, args.runner, overwrite=args.overwrite)
            print(f"  {show_command(cmd)}")
        print(f"\nDry run: {total} files would be processed")
        return

    tasks = [
        (i + 1, total, f, output_dir, script, extra_args, args.command, args.runner, args.overwrite)
        for i, f in enumerate(files)
    ]

    with Pool(processes=args.workers) as pool:
        results = pool.map(run_one, tasks)

    succeeded = sum(1 for result in results if result["ok"])
    failed = total - succeeded
    print(f"\nTotal: {total} | Succeeded: {succeeded} | Failed: {failed}")

    if args.report:
        report = {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "results": [result["report"] for result in results],
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if failed:
        print("\nFailed files:")
        for result in results:
            if not result["ok"]:
                print(f"  {Path(result['report']['input']).name}: {result['message'][:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
