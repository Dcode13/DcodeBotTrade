"""Pytest bootstrap: pastikan root project ada di sys.path agar `import core`."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
