import sys
from pathlib import Path

# curve.py and grouping.py live inside the custom_components package
# alongside __init__.py, which imports homeassistant (not installed
# here) - so tests import them as bare top-level modules (curve.py,
# grouping.py directly), never through the package name, which would
# execute __init__.py and pull in homeassistant.
INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "adaptive_lighting_helpers"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))
