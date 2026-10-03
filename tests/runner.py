"""
runner.py — executes a list of TestCases against the real Agent loop and
writes a JSON + Markdown report.

Uses SafetyGate's confirm_callback hook (see safety.py's docstring) so
confirmations don't block on console input() during an unattended run.
This does NOT weaken the safety model for normal use — main.py never
passes a callback, so interactive runs are unaffected.

Importantly, the test callback is SCOPE-AWARE, not a blanket approval.
Approving every confirmation unconditionally would make the test harness
itself a safety bypass: if a test goal were ever worded ambiguously, or
the agent's behavior drifted, an unattended blanket-yes could approve
something well outside what the suite intends (e.g. touching a path
outside the Desktop test fixtures, or launching/closing an app the suite
never asked about). Instead, _scoped_test_confirm only approves actions
that match the specific paths/apps/windows the test suite is actually
exercising — everything else is denied and logged, exactly as it would be
if a real reviewer said "that's not what I expected, no."
"""

import json
import os
import time
from datetime import datetime

from agent.agent import Agent
from tests.cases import DESKTOP

# Apps/processes/windows the test suite legitimately interacts with.
# Anything outside these sets is outside the suite's intended scope.
_ALLOWED_TEST_APPS = {"notepad", "notepad.exe", "calculator", "calc", "calc.exe", "calculatorapp.exe"}
_ALLOWED_TEST_PROCESSES = {"notepad.exe", "calc.exe", "calculatorapp.exe"}
_ALLOWED_TEST_WINDOW_SUBSTRINGS = ("notepad", "calculator")


def _path_in_test_scope(path: str) -> bool:
    """A path is in scope only if it's under the Desktop fixture AND
    follows the agent_test naming convention — not just "anywhere on the
    Desktop", so an unrelated real file that happens to live there can't
    be approved by accident."""
    if not path:
        return False
    expanded = os.path.expandvars(str(path)).lower()
    desktop_expanded = os.path.expandvars(DESKTOP).lower()
    return desktop_expanded in expanded and "agent_test" in expanded


def _scoped_test_confirm(tool_name: str, tool_input: dict, reason: str) -> bool:
    """
    Scope-aware auto-confirm for the test harness. Approves only what the
    test suite is actually meant to do; denies everything else, including
    during an "unattended" run.
    """
    if tool_name in ("write_file", "delete_file"):
        approved = _path_in_test_scope(tool_input.get("path", ""))
    elif tool_name == "move_file":
        approved = _path_in_test_scope(tool_input.get("src", "")) and _path_in_test_scope(tool_input.get("dst", ""))
    elif tool_name == "launch_app":
        requested = str(tool_input.get("requested", tool_input.get("app_path_or_name", ""))).strip().lower()
        approved = requested in _ALLOWED_TEST_APPS
    elif tool_name == "close_app":
        process_name = str(tool_input.get("process_name", "")).strip().lower()
        approved = process_name in _ALLOWED_TEST_PROCESSES
    elif tool_name in ("ui_click_control", "ui_type_into_control", "ui_close_window"):
        window_title = str(tool_input.get("window_title", "")).lower()
        approved = any(sub in window_title for sub in _ALLOWED_TEST_WINDOW_SUBSTRINGS)
    else:
        # Anything not explicitly recognized (including run_command, which
        # the test suite never intentionally uses) is denied by default —
        # fail closed, same philosophy as SafetyGate itself.
        approved = False

    print(f"[test-scope-check] {tool_name} -> {'APPROVED' if approved else 'DENIED'} "
          f"(input={tool_input})")
    return approved


class TestRunner:
    def __init__(self, config: dict, report_dir: str = "logs/test_reports"):
        self.config = config
        self.report_dir = report_dir
        os.makedirs(self.report_dir, exist_ok=True)

    def run(self, cases: list) -> list:
        results = []

        for case in cases:
            print(f"\n{'=' * 70}\nRUNNING: {case.id} — {case.description}\n{'=' * 70}")
            print(f"Goal: {case.goal}")

            agent = Agent(self.config, confirm_callback=_scoped_test_confirm)

            if case.setup:
                try:
                    case.setup(agent.dispatcher)
                except Exception as e:
                    print(f"[setup error] {e}")

            start = time.time()
            try:
                final_text = agent.run(case.goal, max_iterations=case.max_iterations)
            except Exception as e:
                final_text = f"EXCEPTION during agent.run: {e}"
            duration = round(time.time() - start, 1)

            try:
                verify_result = case.verify(agent.dispatcher, final_text)
            except Exception as e:
                from tests.cases import VerifyResult
                verify_result = VerifyResult(False, f"EXCEPTION during verify: {e}")

            if case.cleanup:
                try:
                    case.cleanup(agent.dispatcher)
                except Exception as e:
                    print(f"[cleanup error] {e}")

            status = "PASS" if verify_result.passed else "FAIL"
            print(f"\n[{status}] {case.id}: {verify_result.message}  ({duration}s)")

            results.append({
                "id": case.id,
                "description": case.description,
                "goal": case.goal,
                "passed": verify_result.passed,
                "message": verify_result.message,
                "duration_seconds": duration,
                "agent_final_text": final_text,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            })

        self._write_report(results)
        return results

    def _write_report(self, results: list):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(self.report_dir, f"report_{ts}.json")
        md_path = os.path.join(self.report_dir, f"report_{ts}.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        lines = [
            f"# Test run — {ts}",
            "",
            f"**{passed}/{total} passed**",
            "",
            "| Test | Status | Message | Time (s) |",
            "|---|---|---|---|",
        ]
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            # Escape pipe characters so the markdown table doesn't break
            safe_message = r["message"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['id']} | {status} | {safe_message} | {r['duration_seconds']} |")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        print(f"\n{'=' * 70}\n{passed}/{total} PASSED\nReports written to:\n  {json_path}\n  {md_path}\n{'=' * 70}")
