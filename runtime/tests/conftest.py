import sys
from pathlib import Path

# Ensure tests run against the current source tree in src/, not any installed
# copy of the sharp package that may exist in the venv.
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
