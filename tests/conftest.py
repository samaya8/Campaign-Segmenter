import sys
from pathlib import Path

# Make the project root importable (for `src.*` and Day 1's `headlines`).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
