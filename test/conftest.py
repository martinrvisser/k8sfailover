import importlib.util
import sys
from pathlib import Path

# The script lives at failover-manager/failover-manager.py, which isn't an
# importable module name. Load it as `failover_manager` so the tests can
# `import failover_manager` and patch("failover_manager....").
SCRIPT = Path(__file__).resolve().parent.parent / "failover-manager" / "failover-manager.py"

spec = importlib.util.spec_from_file_location("failover_manager", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["failover_manager"] = module
spec.loader.exec_module(module)
