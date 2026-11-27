"""Run regression tests while collecting destroyed Tk fixtures on the main thread.

Usage: python -m tests.run_suite [tests.test_module ...]
Windows multiprocessing requires this importable entry point (not stdin).
"""
from __future__ import annotations
import gc
from pathlib import Path
import sys
import unittest


class MainThreadCollection(unittest.TextTestResult):
    """Keep Tcl interpreter destruction out of later tests' worker threads."""
    def startTest(self, test: unittest.TestCase) -> None:
        gc.collect()
        super().startTest(test)


def main() -> int:
    """Discover all tests, or run explicitly named modules, with unchanged assertions."""
    enabled=gc.isenabled()
    gc.disable()
    try:
        loader=unittest.defaultTestLoader
        suite=(loader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv)>1 else
               loader.discover(str(Path(__file__).parent),top_level_dir=str(Path(__file__).parent.parent)))
        result=unittest.TextTestRunner(verbosity=2,resultclass=MainThreadCollection).run(suite)
        gc.collect()
        return 0 if result.wasSuccessful() else 1
    finally:
        if enabled:gc.enable()


if __name__=='__main__':
    raise SystemExit(main())
