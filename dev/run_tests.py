"""Regression tests; use the managed Python for optional media tests."""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugin/scripts"), str(ROOT / "dev/tests"), str(ROOT / "dev")]

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "dev/tests"), pattern=sys.argv[1] if len(sys.argv) > 1 else "test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
