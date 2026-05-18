"""Thin python wrapper around refresh_snapshot.sh so bot.py's safety_net_loop
(which spawns `python <script>.py`) can rerun the daily snapshot pipeline."""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SH = HERE / "refresh_snapshot.sh"

if not SH.exists():
    print(f"refresh_snapshot.sh missing at {SH}", file=sys.stderr)
    sys.exit(1)

result = subprocess.run(["/bin/bash", str(SH)], cwd=str(HERE.parent))
sys.exit(result.returncode)
