"""Run portable regression tests without installed model-provider CLIs."""

import sys
import unittest
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
tests = root / 'nix/_research'
sys.path.insert(0, str(tests))
suite = unittest.TestSuite()
for pattern in ('test_factory.py', 'test_proxy.py', 'test_workflow_v2.py', 'test_experiment_runner.py'):
    suite.addTests(unittest.defaultTestLoader.discover(str(tests), pattern=pattern))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
