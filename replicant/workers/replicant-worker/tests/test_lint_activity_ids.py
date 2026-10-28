"""tools/lint_activity_ids.py (XD-00-01) as a test: every side-effecting activity call passes an explicit ID."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_lint_activity_ids() -> None:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "lint_activity_ids.py"), "replicant_worker"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
