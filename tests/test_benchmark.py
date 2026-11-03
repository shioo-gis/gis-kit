from pathlib import Path
import json
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "scripts/benchmark.py"


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "report.json"
        passed = subprocess.run(
            [sys.executable, str(BENCHMARK), "--case", f"ok={sys.executable} -c 'pass'", "--gate", "ok", "--report", str(report)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert passed.returncode == 0, passed.stderr
        assert json.loads(report.read_text(encoding="utf-8"))["decision"] == "TIER0_SUFFICIENT"

        timed_out = subprocess.run(
            [sys.executable, str(BENCHMARK), "--case", f"slow={sys.executable} -c 'import time; time.sleep(1)'", "--gate", "slow", "--max-seconds", "0.1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert timed_out.returncode == 2
        assert json.loads(timed_out.stdout)["decision"] == "RUST_CANDIDATE"


if __name__ == "__main__":
    main()
