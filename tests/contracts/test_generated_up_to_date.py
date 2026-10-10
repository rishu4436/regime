import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_schema_and_ts_types_are_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_contracts.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
