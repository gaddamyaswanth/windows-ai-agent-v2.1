"""
computer.py — screen capture and mouse/keyboard control.

This backs the "computer_use" tool. Prefer commands.py for anything a
script/API can do directly; use this only for GUI apps with no other hook.
"""

import base64
import io
import os
import time
from datetime import datetime

import pyautogui
from PIL import Image

pyautogui.FAILSAFE = True  # slam mouse to a screen corner to abort
pyautogui.PAUSE = 0.15     # small delay between actions so UI can catch up


class ComputerController:
    def __init__(self, config: dict):
        screen_cfg = config.get("screen", {})
        self.width = screen_cfg.get("display_width_px", 1920)
        self.height = screen_cfg.get("display_height_px", 1080)
        self.screenshot_dir = screen_cfg.get("screenshot_dir", "logs/screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)

    # ---------- Screen ----------

    def screenshot(self, save: bool = True) -> dict:
        """Capture the screen. Returns base64 PNG data for the model."""
        img = pyautogui.screenshot()
        img = img.resize((self.width, self.height))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")

        if save:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.screenshot_dir, f"{ts}.png")
            img.save(path)

        return {"type": "base64", "media_type": "image/png", "data": b64_data}

    # ---------- Mouse ----------

    def move_mouse(self, x: int, y: int, duration: float = 0.2):
        pyautogui.moveTo(x, y, duration=duration)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1):
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)

    def double_click(self, x: int, y: int):
        pyautogui.doubleClick(x, y)

    def right_click(self, x: int, y: int):
        pyautogui.rightClick(x, y)

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.4):
        pyautogui.moveTo(start_x, start_y)
        pyautogui.dragTo(end_x, end_y, duration=duration, button="left")

    def scroll(self, amount: int):
        """Positive scrolls up, negative scrolls down."""
        pyautogui.scroll(amount)

    # ---------- Keyboard ----------

    def type_text(self, text: str, interval: float = 0.02):
        pyautogui.typewrite(text, interval=interval)

    def press_key(self, key: str):
        """Single key or combo string like 'ctrl+c'."""
        keys = [k.strip() for k in key.split("+")]
        if len(keys) > 1:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])

    def wait(self, seconds: float):
        time.sleep(seconds)
