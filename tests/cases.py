"""
cases.py — the PC-control test suite.

Each TestCase sends a natural-language goal through the real agent loop,
then verifies the outcome DETERMINISTICALLY — checking actual file
contents, process lists, and window titles via the dispatcher's tools
directly — rather than trusting the agent's own summary of what it did.
This is the "deterministic check" layer discussed in review: screenshots
and AI narration are useful signals but not proof; a file that exists
with the right content is proof.

Cases build on each other in sequence (basic file suite creates
"agent_test.txt", later cases rename/move/delete it) mirroring the task
list from the review. Run BASIC_SUITE for a quick pass, FULL_SUITE to
also exercise Calculator, window switching, and failure recovery.
"""

import math
import os
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class VerifyResult:
    passed: bool
    message: str


@dataclass
class TestCase:
    id: str
    description: str
    goal: str
    verify: Callable  # (dispatcher, agent_final_text) -> VerifyResult
    setup: Optional[Callable] = None      # (dispatcher) -> None, runs before the agent call
    cleanup: Optional[Callable] = None    # (dispatcher) -> None, runs after verify, best-effort
    max_iterations: Optional[int] = None  # override Agent's default iteration cap for hard tasks


# ---------- Shared paths ----------

DESKTOP = r"%USERPROFILE%\Desktop"
TEST_FILE = DESKTOP + r"\agent_test.txt"
TEST_FILE_RENAMED = DESKTOP + r"\agent_test_renamed.txt"
TEST_FOLDER = DESKTOP + r"\agent_test_folder"
TEST_FILE_IN_FOLDER = TEST_FOLDER + r"\agent_test_renamed.txt"
TEST_CONTENT_MARKER = "Hello from my AI agent."


def _silent_delete(dispatcher, path):
    """Best-effort cleanup helper — ignores failures (e.g. file doesn't exist)."""
    try:
        dispatcher.commands.delete_file(path)
    except Exception:
        pass


# ---------- Basic suite: files, folders, apps ----------

def _verify_notepad_running(dispatcher, final_text):
    result = dispatcher.commands.list_processes()
    processes = [p.lower() for p in result.get("processes", [])]
    if "notepad.exe" in processes:
        return VerifyResult(True, "notepad.exe found in process list.")
    return VerifyResult(False, f"notepad.exe not found. Processes seen: {processes[:10]}...")


def _verify_notepad_closed(dispatcher, final_text):
    result = dispatcher.commands.list_processes()
    processes = [p.lower() for p in result.get("processes", [])]
    if "notepad.exe" not in processes:
        return VerifyResult(True, "notepad.exe no longer in process list.")
    return VerifyResult(False, "notepad.exe is still running.")


def _verify_file_content(dispatcher, final_text):
    result = dispatcher.commands.read_file(TEST_FILE)
    if not result.get("success"):
        return VerifyResult(False, f"Could not read {TEST_FILE}: {result.get('error')}")
    content = result.get("content", "")
    if TEST_CONTENT_MARKER in content:
        return VerifyResult(True, f"File contains expected text. Content: {content[:80]!r}")
    return VerifyResult(False, f"File exists but content doesn't match. Got: {content[:80]!r}")


def _verify_renamed(dispatcher, final_text):
    listing = dispatcher.commands.list_dir(DESKTOP)
    entries = listing.get("entries", [])
    has_new = "agent_test_renamed.txt" in entries
    has_old = "agent_test.txt" in entries
    if has_new and not has_old:
        return VerifyResult(True, "Old name gone, new name present on Desktop.")
    return VerifyResult(False, f"Expected renamed file only. Desktop entries: {entries}")


def _verify_folder_created(dispatcher, final_text):
    listing = dispatcher.commands.list_dir(DESKTOP)
    entries = listing.get("entries", [])
    if "agent_test_folder" in entries:
        return VerifyResult(True, "agent_test_folder found on Desktop.")
    return VerifyResult(False, f"Folder not found. Desktop entries: {entries}")


def _verify_moved_into_folder(dispatcher, final_text):
    folder_listing = dispatcher.commands.list_dir(TEST_FOLDER)
    desktop_listing = dispatcher.commands.list_dir(DESKTOP)
    in_folder = "agent_test_renamed.txt" in folder_listing.get("entries", [])
    still_at_root = "agent_test_renamed.txt" in desktop_listing.get("entries", [])
    if in_folder and not still_at_root:
        return VerifyResult(True, "File is in the folder and no longer at Desktop root.")
    return VerifyResult(
        False,
        f"in_folder={in_folder}, still_at_desktop_root={still_at_root}. "
        f"Folder entries: {folder_listing.get('entries')}"
    )


def _verify_read_reported_correctly(dispatcher, final_text):
    # Deterministic-ish: check the agent's own final answer contains the
    # marker text, since "read a file and tell me what it says" is
    # inherently a text-output task. We still verify the underlying file
    # independently rather than trusting the agent alone.
    file_check = dispatcher.commands.read_file(TEST_FILE_IN_FOLDER)
    file_ok = file_check.get("success") and TEST_CONTENT_MARKER in file_check.get("content", "")
    reported_ok = TEST_CONTENT_MARKER.lower() in final_text.lower()
    if file_ok and reported_ok:
        return VerifyResult(True, "File content matches and agent reported it correctly.")
    return VerifyResult(
        False,
        f"file_ok={file_ok}, reported_in_answer={reported_ok}. Agent said: {final_text[:150]!r}"
    )


def _verify_cleanup(dispatcher, final_text):
    listing = dispatcher.commands.list_dir(DESKTOP)
    entries = listing.get("entries", [])
    if "agent_test_folder" not in entries:
        return VerifyResult(True, "agent_test_folder no longer present — cleanup succeeded.")
    return VerifyResult(False, f"agent_test_folder still present. Desktop entries: {entries}")


BASIC_SUITE = [
    TestCase(
        id="01_open_notepad",
        description="Open Notepad",
        goal="Open Notepad.",
        verify=_verify_notepad_running,
    ),
    TestCase(
        id="02_type_and_save",
        description="Type text in Notepad and save to Desktop",
        goal=(
            f'In Notepad, type exactly: "{TEST_CONTENT_MARKER}" '
            f'and save the file as {TEST_FILE}'
        ),
        setup=lambda d: _silent_delete(d, TEST_FILE),
        verify=_verify_file_content,
    ),
    TestCase(
        id="03_close_notepad",
        description="Close Notepad",
        goal="Close Notepad.",
        verify=_verify_notepad_closed,
    ),
    TestCase(
        id="04_rename_file",
        description="Rename the test file",
        goal=f"Rename {TEST_FILE} to {TEST_FILE_RENAMED}",
        verify=_verify_renamed,
    ),
    TestCase(
        id="05_create_folder",
        description="Create a folder on the Desktop",
        goal=f'Create a folder named "agent_test_folder" on the Desktop.',
        setup=lambda d: _silent_delete(d, TEST_FOLDER),
        verify=_verify_folder_created,
    ),
    TestCase(
        id="06_move_file",
        description="Move the renamed file into the new folder",
        goal=f"Move {TEST_FILE_RENAMED} into {TEST_FOLDER}",
        verify=_verify_moved_into_folder,
    ),
    TestCase(
        id="07_read_file_contents",
        description="Read the file back and report its contents",
        goal=f"Read the contents of {TEST_FILE_IN_FOLDER} and tell me exactly what it says.",
        verify=_verify_read_reported_correctly,
    ),
    TestCase(
        id="08_cleanup",
        description="Delete the test folder and its contents",
        goal=f"Delete the folder {TEST_FOLDER} and everything inside it.",
        verify=_verify_cleanup,
    ),
]


# ---------- Full suite additions: apps, window switching, resilience ----------

def _verify_calculator_result(dispatcher, final_text):
    expected = 1234 * 5678
    if str(expected) in final_text.replace(",", ""):
        return VerifyResult(True, f"Agent reported the correct result ({expected}).")
    return VerifyResult(False, f"Expected {expected} in agent's answer. Got: {final_text[:150]!r}")


def _verify_active_window_contains(expected_substr):
    def _verify(dispatcher, final_text):
        result = dispatcher.ui.get_active_window()
        title = result.get("active_window", "")
        if expected_substr.lower() in title.lower():
            return VerifyResult(True, f"Active window is '{title}', matches expected substring.")
        return VerifyResult(False, f"Active window is '{title}', expected to contain '{expected_substr}'.")
    return _verify


def _verify_did_not_hang(dispatcher, final_text):
    # Resilience check: the agent should recognize a missing UI element
    # and report failure gracefully, rather than exhausting all iterations
    # or crashing. We check it finished with actual output, not the
    # "Stopped: reached max iterations" fallback message.
    if final_text.startswith("Stopped: reached max iterations"):
        return VerifyResult(False, "Agent exhausted its iteration budget instead of recovering.")
    if final_text.startswith("Halted:"):
        return VerifyResult(False, "Agent was halted (kill switch) — not a recovery scenario.")
    return VerifyResult(True, f"Agent finished with a response instead of hanging: {final_text[:150]!r}")


FULL_SUITE = BASIC_SUITE + [
    TestCase(
        id="09_calculator",
        description="Use Calculator to compute a product",
        goal="Open Calculator, compute 1234 * 5678, and tell me the result.",
        verify=_verify_calculator_result,
    ),
    TestCase(
        id="10_window_switching",
        description="Open two apps and switch back to the first",
        goal="Open Notepad, then open Calculator, then switch focus back to Notepad.",
        verify=_verify_active_window_contains("Notepad"),
        cleanup=lambda d: (d.commands.close_app("notepad.exe"), d.commands.close_app("CalculatorApp.exe")),
    ),
    TestCase(
        id="11_recover_missing_control",
        description="Gracefully handle a UI control that doesn't exist",
        goal=(
            'Open Notepad, then try to click a button labeled '
            '"ThisButtonDoesNotExist12345". If it is not found, '
            "don't get stuck retrying forever — tell me it doesn't exist."
        ),
        verify=_verify_did_not_hang,
        max_iterations=10,
        cleanup=lambda d: d.commands.close_app("notepad.exe"),
    ),
]
