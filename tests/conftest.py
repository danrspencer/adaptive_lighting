import sys
from pathlib import Path

# curve.py and grouping.py live inside the custom_components package
# alongside __init__.py, which imports homeassistant - so tests import
# them as bare top-level modules (curve.py, grouping.py directly),
# never through the package name, which would execute __init__.py and
# pull in homeassistant. Kept this way even though homeassistant is
# installed (for tests/integration/, see its own conftest.py) so these
# stay what they've always been: fast, dependency-light tests of pure
# logic, independent of HA's event loop and everything that comes with
# it.
INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "flare"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_DIR))
