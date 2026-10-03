"""
agent/ui_automation.py — Automated UI Controller for Windows desktop interaction.
"""

from __future__ import annotations

import logging
import time
import subprocess
import pyautogui
import pywinauto

logger = logging.getLogger("UIAutomation")

# Safety failsafe: moving mouse to any corner aborts pyautogui actions instantly
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


class UIAutomationController:
    def __init__(self, config=None):
        self.config = config or {}

    def click_text(self, text: str) -> dict:
        """Finds text/control on screen using pywinauto and clicks it."""
        try:
            logger.info("Attempting to find and click UI element: %r", text)
            desktop = pywinauto.Desktop(backend="uia")
            # Search active windows for the control text
            windows = desktop.windows()
            for win in windows:
                try:
                    if win.is_visible() and win.exists():
                        control = win.child_window(title_re=f".*{text}.*", control_type="Button")
                        if control.exists():
                            control.click_input()
                            return {"success": True, "message": f"Clicked button '{text}'."}
                except Exception:
                    continue
            return {"success": False, "message": f"Could not find a clickable UI element matching '{text}'."}
        except Exception as exc:
            logger.warning("UI click failed: %s", exc)
            return {"success": False, "message": f"UI interaction failed: {exc}"}

    def type_in_field(self, field_name: str, text_to_type: str) -> dict:
        """Finds a text input field and types into it."""
        try:
            logger.info("Typing %r into field %r", text_to_type, field_name)
            desktop = pywinauto.Desktop(backend="uia")
            for win in desktop.windows():
                try:
                    if win.is_visible() and win.exists():
                        edit = win.child_window(title_re=f".*{field_name}.*", control_type="Edit")
                        if edit.exists():
                            edit.set_focus()
                            edit.type_keys(text_to_type, with_spaces=True)
                            return {"success": True, "message": f"Typed text into {field_name}."}
                except Exception:
                    continue

            # Fallback to general hotkey typing if specific edit field isn't matched via UIA
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.2)
            pyautogui.write(text_to_type, interval=0.05)
            return {"success": True, "message": f"Typed '{text_to_type}' via fallback search."}
        except Exception as exc:
            return {"success": False, "message": f"Failed to type into field: {exc}"}

    def scroll(self, direction: str, clicks: int = 3) -> dict:
        try:
            amount = -clicks * 100 if direction.lower() == "down" else clicks * 100
            pyautogui.scroll(amount)
            return {"success": True, "message": f"Scrolled {direction}."}
        except Exception as exc:
            return {"success": False, "message": f"Scroll failed: {exc}"}