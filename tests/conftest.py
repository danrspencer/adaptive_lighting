import sys
from pathlib import Path

MODULES_DIR = Path(__file__).resolve().parent.parent / "pyscript" / "modules"
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))
