"""
run_tests.py — entry point for the PC-control test suite.

Usage:
    python run_tests.py            # basic suite (files, folders, apps)
    python run_tests.py --full     # basic + Calculator, window switching, recovery
    python run_tests.py --quick    # first 3 basic cases only, for a fast smoke test

This runs REAL actions on the machine it's executed on: opening/closing
apps, creating/renaming/moving/deleting files under your Desktop, and
clicking through Calculator. It uses the confirm_callback test hook so it
runs unattended rather than pausing for a y/N prompt at every step —
review tests/cases.py before running so you know exactly what it does.
"""

import sys
import yaml

from agent import kill_switch
from tests.runner import TestRunner
from tests.cases import BASIC_SUITE, FULL_SUITE


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    kill_switch.start_listener()

    if "--full" in sys.argv:
        cases = FULL_SUITE
    elif "--quick" in sys.argv:
        cases = BASIC_SUITE[:3]
    else:
        cases = BASIC_SUITE

    print(f"Running {len(cases)} test case(s). Press Ctrl+Alt+Shift+Q at any time to abort.\n")

    runner = TestRunner(config)
    runner.run(cases)


if __name__ == "__main__":
    main()
