"""
agent/agent.py — Orchestration core for the Windows AI Agent (UI Settings, Bluetooth Toggle, Phonetic ASR Fixes, Automated UI & Camera Sub-Features).
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from agent.voice_normalizer import VoiceCommandNormalizer
from agent.browser_workflows import BrowserWorkflows
from agent.ui_automation import UIAutomationController
from agent import kill_switch

logger = logging.getLogger("Agent")

BROWSER_NAMES = {"chrome", "brave", "edge", "firefox", "opera", "vivaldi", "brav", "bravo"}
_BROWSER_ASR_FIXES = {"bravo": "brave", "brav": "brave"}


# --------------------------------------------------------------------------
# Hardware abstraction — swap for a mock in tests
# --------------------------------------------------------------------------

class SystemBackend:
    """Thin wrapper over OS/keyboard so the agent is testable."""

    def hotkey(self, *keys: str) -> None:
        import pyautogui
        pyautogui.hotkey(*keys)

    def press(self, key: str) -> None:
        import pyautogui
        pyautogui.press(key)

    def type_text(self, text: str) -> None:
        import pyperclip
        pyperclip.copy(text)
        self.hotkey("ctrl", "v")

    def set_volume_absolute(self, value: int) -> None:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(value / 100.0, None)

    def screenshot(self) -> str:
        from PIL import ImageGrab
        import datetime
        os.makedirs("screenshots", exist_ok=True)
        path = os.path.join("screenshots", datetime.datetime.now().strftime("%Y%m%d_%H%M%S.png"))
        ImageGrab.grab().save(path)
        return path


# --------------------------------------------------------------------------
# Safety gate
# --------------------------------------------------------------------------

class SafetyGate:
    def __init__(self, config):
        if hasattr(config, "safety"):
            safety = config.safety
        elif hasattr(config, "get"):
            safety = config.get("safety", {})
        else:
            safety = {}

        self.confirmations_enabled = bool(getattr(safety, "require_confirmation_for_dangerous_actions", True))
        self.max_actions_per_minute = int(getattr(safety, "max_actions_per_minute", 30))

        blocked_raw = getattr(safety, "blocked_processes", ["csrss", "wininit", "winlogon", "services", "lsass", "smss", "svchost", "explorer", "dwm"])
        self.blocked_processes = {str(p).lower().removesuffix(".exe") for p in blocked_raw}
        self._timestamps: list[float] = []

    def check(self, action_type: str) -> tuple[bool, str]:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        if len(self._timestamps) >= self.max_actions_per_minute:
            return False, "Rate limit exceeded."
        return True, ""

    def record_attempt(self):
        self._timestamps.append(time.monotonic())


# --------------------------------------------------------------------------
# LLM tool router (rules first, LLM fallback)
# --------------------------------------------------------------------------

class OllamaToolRouter:
    TOOLS: Dict[str, Optional[str]] = {
        "open_app": "name",
        "close_app": "name",
        "set_volume": "value",
        "set_brightness": "value",
        "take_screenshot": None,
        "web_search": "query",
        "unknown": None,
    }

    SYSTEM_PROMPT = (
            "You control a Windows PC via tools. Choose exactly one tool.\n"
            'Respond ONLY with minified JSON:\n'
            '{"tool":"<name>","args":{<arg>:<value>}}\n'
            "Tools:\n"
            + "\n".join(f"- {k}: {d}" for k, d in {
        "open_app": "launch an app by name",
        "close_app": "close an app by name",
        "set_volume": "system volume 0-100",
        "set_brightness": "brightness 0-100",
        "take_screenshot": "take a screenshot",
        "web_search": "search the web",
        "unknown": "use when nothing fits",
    }.items())
            + '\nIf unsure, use "unknown".'
    )

    RULES = [
        (re.compile(r"\b(?:open|start|launch)\s+(?:the\s+)?(.+)", re.I), "open_app"),
        (re.compile(r"\b(?:close|quit|exit|kill)\s+(?:the\s+)?(.+)", re.I), "close_app"),
        (re.compile(r"\b(?:set\s+)?volume\s*(?:to|at)?\s*(\d{1,3})\b", re.I), "set_volume"),
        (re.compile(r"\bbrightness\s*(?:to|at)?\s*(\d{1,3})\b", re.I), "set_brightness"),
        (re.compile(r"\b(?:take\s+a?\s*)?(?:screen\s*shot|screenshot)\b", re.I), "take_screenshot"),
        (re.compile(r"\b(?:search(?:\s+the)?\s+web(?:\s+for)?|google)\s+(.+)", re.I), "web_search"),
    ]

    def __init__(self, config: Any):
        if hasattr(config, "model"):
            model_cfg = config.model
        elif hasattr(config, "get"):
            model_cfg = config.get("model", {})
        else:
            model_cfg = {}

        self.model = getattr(model_cfg, "ollama_model", "qwen2.5:1.5b")
        self.temperature = float(getattr(model_cfg, "temperature", 0.1))
        self.timeout = float(getattr(model_cfg, "request_timeout_seconds", 120))
        host = getattr(model_cfg, "ollama_host", "http://localhost:11434")
        self.api_url = f"{host}/api/generate"

    @staticmethod
    def _clean_name(raw: Any) -> str:
        name = str(raw or "").strip().rstrip(".!?").strip()
        name = re.sub(r"""['"{}\[\]]""", "", name).strip()
        return re.sub(r"^(?:app|program|application)\s+", "", name, flags=re.I).lower()

    @staticmethod
    def _clamp_value(raw: Any) -> Optional[int]:
        try:
            return max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return None

    def route(self, utterance: Any) -> dict:
        text = utterance.get("text", "") if isinstance(utterance, dict) else str(utterance or "")
        text = text.strip()

        for pattern, tool in self.RULES:
            match = pattern.search(text)
            if not match:
                continue
            arg = self.TOOLS[tool]
            args: Dict[str, Any] = {}
            if arg == "name":
                cleaned = self._clean_name(match.group(1))
                if not cleaned:
                    continue
                args["name"] = cleaned
            elif arg == "query":
                args["query"] = match.group(1).strip().rstrip(".!?")
            elif arg == "value":
                value = self._clamp_value(match.group(1))
                if value is None:
                    continue
                args["value"] = value
            return {"tool": tool, "args": args}

        parsed = self._parse_json(self._call_ollama(text))
        if parsed is None:
            return {"tool": "unknown", "args": {}}

        tool = str(parsed.get("tool", "unknown")).strip()
        llm_args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        if tool not in self.TOOLS:
            return {"tool": "unknown", "args": {}}

        expected_arg = self.TOOLS[tool]
        if expected_arg is None:
            return {"tool": tool, "args": {}}

        value = llm_args.get(expected_arg)
        if value is None:
            return {"tool": "unknown", "args": {}}
        if expected_arg == "value":
            clamped = self._clamp_value(value)
            if clamped is None:
                return {"tool": "unknown", "args": {}}
            value = clamped
        else:
            value = self._clean_name(value)
            if not value:
                return {"tool": "unknown", "args": {}}
        return {"tool": tool, "args": {expected_arg: value}}

    def _call_ollama(self, utterance: str) -> str:
        payload = {
            "model": self.model,
            "prompt": f"{self.SYSTEM_PROMPT}\n\nUser: {utterance}\nJSON:",
            "stream": False,
            "format": "json",
            "options": {"temperature": self.temperature, "num_predict": 128},
        }
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return str(json.loads(response.read().decode()).get("response", ""))
        except Exception as exc:
            logger.warning("Ollama call failed: %s", exc)
            return ""

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        raw = raw.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                obj = json.loads(match.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
        return None


# --------------------------------------------------------------------------
# App commands
# --------------------------------------------------------------------------

class AppCommands:
    APP_ALIASES = {
        "notepad": ["notepad.exe"],
        "calculator": ["calculator:", "calc.exe"],
        "calc": ["calculator:", "calc.exe"],
        "paint": ["mspaint.exe"],
        "chrome": ["chrome.exe"],
        "brave": [
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ],
        "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"],
        "spotify": ["spotify.exe"],
        "discord": ["discord.exe"],
        "whatsapp": ["whatsapp:"],
        "whats app": ["whatsapp:"],
        "what's up": ["whatsapp:"],
        "watch up": ["whatsapp:"],
        "word": ["winword.exe"],
        "excel": ["excel.exe"],
        "powerpoint": ["powerpnt.exe"],
        "outlook": ["outlook.exe"],
        "explorer": ["explorer.exe"],
        "task manager": ["taskmgr.exe"],
        "cmd": ["cmd.exe"],
        "terminal": ["wt.exe", "powershell.exe"],
        "settings": ["ms-settings:"],
        "camera": ["microsoft.windows.camera:"],
        "photos": ["microsoft.windows.photos:"],
        "snipping tool": ["snippingtool.exe", "ms-screenclip:"],
        "windows update": ["ms-settings:windowsupdate"],
        "windows updates": ["ms-settings:windowsupdate"],
        "update": ["ms-settings:windowsupdate"],
        "wifi": ["ms-settings:network-wifi"],
        "wi-fi": ["ms-settings:network-wifi"],
        "bluetooth": ["ms-settings:bluetooth"],
        "mouse": ["ms-settings:mousetouchpad"],
        "mouse settings": ["ms-settings:mousetouchpad"],
        "touchpad": ["ms-settings:mousetouchpad"],
        "display": ["ms-settings:display"],
        "sound": ["ms-settings:sound"],
        "storage": ["ms-settings:storagesense"],
        "battery": ["ms-settings:batterysaver"],
        "privacy": ["ms-settings:privacy"],
        "about": ["ms-settings:about"],
        "device manager": ["devmgmt.msc"],
        "control panel": ["control.exe"],
    }

    CLOSE_PROCESS_MAP = {
        "whatsapp": ["WhatsApp.exe", "WhatsApp.Root.exe", "WhatsAppHost.exe"],
        "notepad": ["notepad.exe"],
        "chrome": ["chrome.exe"],
        "brave": ["brave.exe"],
        "edge": ["msedge.exe"],
        "camera": ["WindowsCamera.exe", "Camera.exe"],
        "settings": ["SystemSettings.exe"],
        "calculator": ["CalculatorApp.exe"],
    }

    CLOSE_DENYLIST = {"explorer", "cmd", "terminal", "task manager", "control panel"}

    BROWSER_PATHS = {
        "brave": [
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ],
        "chrome": [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ],
        "edge": [
            os.path.expandvars(r"%PROGRAMFILES(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        ],
    }

    def __init__(self, config: Any):
        if hasattr(config, "safety"):
            safety = config.safety
        elif hasattr(config, "get"):
            safety = config.get("safety", {})
        else:
            safety = {}
        blocked_list = getattr(safety, "blocked_processes", [])
        self.blocked = {str(item).lower().removesuffix(".exe") for item in blocked_list}

    def resolve_name(self, spoken: str) -> Optional[str]:
        name = str(spoken or "").strip().lower().rstrip(".!?")

        if name in {"watch up", "whats up", "what's up", "what's app", "whats app"}:
            name = "whatsapp"

        name = re.sub(r"""['"{}\[\]]""", "", name).strip()
        name = re.sub(r"^(?:the|app|application|program)\s+", "", name).strip()
        if not name:
            return None
        if name in self.APP_ALIASES:
            return name
        matches = difflib.get_close_matches(name, list(self.APP_ALIASES.keys()), n=1, cutoff=0.60)
        if matches:
            logger.info("Fuzzy app match: %r -> %r", name, matches[0])
            return matches[0]
        return None

    def find_browser_exe(self, browser: str) -> Optional[str]:
        import shutil
        browser_key = browser.lower()
        for candidate in self.BROWSER_PATHS.get(browser_key, []):
            if candidate and os.path.exists(candidate):
                return candidate
        return shutil.which(browser_key)

    def open(self, name: str) -> dict:
        import shutil
        raw = str(name or "").strip().lower()

        if raw in {"watch up", "whats up", "what's up", "what's app", "whats app"}:
            raw = "whatsapp"

        cleaned = re.sub(r"\b(?:and|,|then)\s+(?:click|take|snap|capture|press)\b.*$", "", raw, flags=re.I).strip()
        resolved = self.resolve_name(cleaned or raw)
        display = resolved or cleaned.lower()

        if resolved and resolved.lower() in {b.lower() for b in self.blocked} | {b.lower() for b in ("shutdown", "regedit")}:
            logger.warning("Blocked attempt to open %r", display)

        candidates = self.APP_ALIASES.get(resolved, [cleaned.lower(), display])
        for candidate in candidates:
            try:
                if candidate.endswith(":") or candidate.startswith("ms-settings:"):
                    os.startfile(candidate)
                    return {"success": True, "message": f"Opening {display}."}
                if os.path.exists(candidate):
                    subprocess.Popen([candidate], shell=False, close_fds=True)
                    return {"success": True, "message": f"Opening {display}."}
                exe = shutil.which(candidate) or shutil.which(candidate.removesuffix(".exe"))
                if exe:
                    subprocess.Popen([exe], shell=False, close_fds=True)
                    return {"success": True, "message": f"Opening {display}."}
            except Exception as exc:
                logger.debug("Open candidate %s failed: %s", candidate, exc)

        # Smart Web Fallback
        if "." in display or display in {"linkedin", "github", "reddit", "twitter", "facebook", "instagram", "netflix", "chatgpt", "youtube", "google"}:
            url = display if display.startswith(("http://", "https://")) else f"https://www.{display}.com"
            try:
                os.startfile(url)
                return {"success": True, "message": f"Opening {display}."}
            except Exception:
                pass

        try:
            os.startfile(display)
            return {"success": True, "message": f"Opening {display}."}
        except Exception:
            try:
                subprocess.Popen(display, shell=True)
                return {"success": True, "message": f"Opening {display}."}
            except Exception as exc:
                return {"success": False, "message": f"I couldn't find or open '{display}'."}

    def close(self, name: str) -> dict:
        cleaned = str(name or "").strip().lower().removesuffix(".exe")
        resolved = self.resolve_name(cleaned)
        app_name = (resolved or cleaned).removesuffix(".exe")

        if app_name == "youtube":
            return {"success": False, "message": "YouTube runs in your web browser. Say 'close tab' or 'close Brave'."}

        if app_name in self.CLOSE_DENYLIST or app_name in self.blocked:
            return {"success": False, "message": f"Closing {app_name} isn't allowed."}

        if app_name in {"brave", "chrome", "edge"}:
            try:
                import pyautogui
                subprocess.run(["powershell", "-Command", f"(New-Object -ComObject WScript.Shell).AppActivate('{app_name.capitalize()}')"], capture_output=True, timeout=3)
                time.sleep(0.3)
                pyautogui.hotkey('alt', 'f4')
            except Exception:
                pass
            process_map = {"brave": "brave.exe", "chrome": "chrome.exe", "edge": "msedge.exe"}
            try:
                subprocess.run(["taskkill", "/IM", process_map[app_name], "/T", "/F"], capture_output=True, text=True, timeout=10)
                return {"success": True, "message": f"Closed {app_name}."}
            except Exception:
                pass

        if app_name == "settings":
            try:
                import pyautogui
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WScript.Shell).AppActivate('Settings')"], capture_output=True, timeout=3)
                time.sleep(0.3)
                pyautogui.hotkey('alt', 'f4')
                return {"success": True, "message": "Closed settings."}
            except Exception:
                pass

        targets = self.CLOSE_PROCESS_MAP.get(app_name, [f"{app_name}.exe"])
        success = False
        for process_name in targets:
            try:
                res = subprocess.run(
                    ["taskkill", "/IM", process_name, "/T", "/F"],
                    capture_output=True, text=True, timeout=10,
                )
                success |= res.returncode == 0
            except Exception as exc:
                logger.debug("taskkill %s failed: %s", process_name, exc)

        if success:
            return {"success": True, "message": f"Closed {app_name}."}
        return {"success": False, "message": f"Couldn't find {app_name} running."}


# --------------------------------------------------------------------------
# System controls
# --------------------------------------------------------------------------

class SystemControls:
    FOLDERS = {
        "download": "Downloads", "downloads": "Downloads",
        "document": "Documents", "documents": "Documents",
        "picture": "Pictures", "pictures": "Pictures",
        "video": "Videos", "videos": "Videos",
        "desktop": "Desktop", "music": "Music",
    }

    def __init__(self, config: Optional[Any] = None, backend: Optional[SystemBackend] = None):
        self.backend = backend or SystemBackend()

    def media_key(self, which: str) -> dict:
        mapping = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack"}
        return self._wrap(lambda: self.backend.press(mapping.get(which, "playpause")), "Playback updated.")

    def volume_step(self, direction: int) -> dict:
        import ctypes
        ctypes.windll.user32.keybd_event(0xAF if direction > 0 else 0xAE, 0, 0, 0)
        return {"success": True, "message": "Volume adjusted."}

    def mute_toggle(self) -> dict:
        import ctypes
        ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)
        return {"success": True, "message": "Toggled mute."}

    def set_volume(self, value) -> dict:
        if value is None:
            return {"success": False, "message": "What volume level?"}
        try:
            val = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return {"success": False, "message": "I need a number between 0 and 100."}
        try:
            self.backend.set_volume_absolute(val)
            return {"success": True, "message": f"Volume set to {val}."}
        except Exception as exc:
            logger.warning("Volume set failed: %s", exc)
            return {"success": False, "message": "Couldn't change system volume on this machine."}

    def set_brightness(self, value) -> dict:
        if value is None:
            return {"success": False, "message": "What brightness level?"}
        try:
            val = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return {"success": False, "message": "I need a number between 0 and 100."}

        ps_script = f"""
        $success = $false
        try {{
            $monitors = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop
            foreach ($monitor in $monitors) {{
                $monitor.WmiSetBrightness(1, {val})
                $success = $true
            }}
        }} catch {{}}
        if (-not $success) {{
            try {{
                (Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {val})
                $success = $true
            }} catch {{}}
        }}
        if (-not $success) {{
            exit 1
        }}
        """
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                return {"success": True, "message": f"Brightness set to {val}."}
        except Exception as exc:
            logger.warning("Brightness failed: %s", exc)

        return {"success": False, "message": "Brightness adjustment not supported on this display (external monitor or desktop PC?)."}

    def bluetooth_toggle(self, action: str) -> dict:
        try:
            import pyautogui
            os.startfile("ms-settings:bluetooth")
            time.sleep(1.0)
            pyautogui.press('tab')
            pyautogui.press('space')
            time.sleep(0.4)
            pyautogui.hotkey('alt', 'f4')
            return {"success": True, "message": f"Turned Bluetooth {action}."}
        except Exception as exc:
            return {"success": False, "message": f"Couldn't toggle Bluetooth: {exc}"}

    def window_action(self, action: str) -> dict:
        combos = {
            "minimize": [("alt", "escape")], "maximize": [("win", "up")],
            "switch": [("alt", "tab")], "show_desktop": [("win", "d")],
        }
        keys = combos.get(action, [("alt", "tab")])
        return self._wrap(lambda: [self.backend.hotkey(*k) for k in keys], "Window action executed.")

    def power(self, action: str) -> dict:
        if action == "lock":
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], capture_output=True, timeout=5)
            return {"success": True, "message": "Locking PC."}
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "10"])
            return {"success": True, "message": "Shutting down in 10 seconds. Say 'abort shutdown' to cancel."}
        if action == "restart":
            subprocess.Popen(["shutdown", "/r", "/t", "10"])
            return {"success": True, "message": "Restarting in 10 seconds. Say 'abort shutdown' to cancel."}
        if action == "sleep":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"], capture_output=True, timeout=5)
            return {"success": True, "message": "Putting PC to sleep."}
        return {"success": False, "message": "Unknown power action."}

    def type_text(self, text: str) -> dict:
        try:
            self.backend.type_text(str(text))
            return {"success": True, "message": "Typed it."}
        except Exception as exc:
            return {"success": False, "message": f"Typing failed: {exc}"}

    def open_folder(self, spoken_name: str) -> dict:
        folder = self.FOLDERS.get(spoken_name.lower().strip())
        if not folder:
            return {"success": False, "message": f"I don't know the folder '{spoken_name}'."}
        path = os.path.join(os.path.expanduser("~"), folder)
        if not os.path.isdir(path):
            return {"success": False, "message": f"The {folder} folder doesn't exist."}
        os.startfile(path)
        return {"success": True, "message": f"Opening {folder}."}

    def screenshot(self) -> dict:
        try:
            path = self.backend.screenshot()
            return {"success": True, "message": "Screenshot saved.", "path": path}
        except Exception as exc:
            return {"success": False, "message": f"Screenshot failed: {exc}"}

    def _wrap(self, fn: Callable[[], None], ok_msg: str) -> dict:
        try:
            fn()
            return {"success": True, "message": ok_msg}
        except Exception as exc:
            return {"success": False, "message": str(exc)}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _extract_browser(text: str) -> tuple[str, Optional[str]]:
    text = str(text or "").strip()
    match = re.search(r"\bin\s+(?:the\s+)?([a-z]+)(?:\s+browser)?\b\.?\s*$", text, re.I)
    if not match:
        return text, None
    candidate = match.group(1).lower()
    if candidate in BROWSER_NAMES:
        return text[:match.start()].strip(), _BROWSER_ASR_FIXES.get(candidate, candidate)
    return text, None


def _build_browser_fallbacks() -> dict:
    def browser_navigate(url: str) -> dict:
        import threading
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "Refusing to open non-http URL."}
        try:
            threading.Thread(target=os.startfile, args=(url,), daemon=True).start()
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def browser_press(key: str) -> dict:
        try:
            import pyautogui
            parts = [p.strip().lower() for p in str(key).split("+") if p.strip()]
            parts = [{"left": "left"}.get(p, p) for p in parts]
            if len(parts) > 1:
                pyautogui.hotkey(*parts)
            elif parts:
                pyautogui.press(parts[0])
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    return {"browser_navigate": browser_navigate, "browser_press": browser_press}


# --------------------------------------------------------------------------
# Agent core
# --------------------------------------------------------------------------

class Agent:
    def __init__(self, config: Any, backend: Optional[SystemBackend] = None):
        self.config = config
        self.normalizer = VoiceCommandNormalizer()
        self.browser = BrowserWorkflows(_build_browser_fallbacks())
        self.ui = UIAutomationController(config)
        self.safety = SafetyGate(config)
        self.router = OllamaToolRouter(config)
        self._pending_confirmation: Optional[dict] = None
        self._apps = AppCommands(config)
        self._system = SystemControls(config, backend=backend)

    def run(self, text: str) -> dict:
        text = str(text or "").strip()
        if not text:
            return {"success": False, "message": "I didn't hear anything."}

        if self._pending_confirmation is not None:
            return self._resolve_confirmation(text)

        intent = self.normalizer.normalize(text)
        allowed, reason = self.safety.check(intent["type"])
        if reason == "NEEDS_CONFIRMATION":
            self._pending_confirmation = intent
            return {"success": False, "message": f"You said: {text}. Should I do that? Say yes or no."}
        if not allowed:
            return {"success": False, "message": reason}

        result = self._dispatch(intent)
        self.safety.record_attempt()
        return result

    def _dispatch(self, intent: dict) -> dict:
        handler_name = f"_handle_{intent['type']}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            return self._handle_normal(intent)
        result = handler(intent)
        if intent["type"] in getattr(self.safety, "CONFIRM_ACTIONS", ()) and not getattr(result, "_confirmed", False):
            self._pending_confirmation = intent
            return {"success": False,
                    "message": f"That's a destructive action ({intent['type']}). Should I do it? Say yes or no."}
        return result

    _YES_WORDS = ("yes", "yeah", "yep", "sure", "go ahead", "do it", "confirm")
    _NO_WORDS = ("no", "nope", "cancel", "stop", "don't", "never mind", "abort")

    def _resolve_confirmation(self, reply: str) -> dict:
        pending = self._pending_confirmation
        self._pending_confirmation = None
        lowered = reply.lower().strip()

        said_no = any(w in lowered for w in self._NO_WORDS)
        said_yes = any(w in lowered for w in self._YES_WORDS)

        if said_no or not said_yes:
            return {"success": True, "cancelled": True, "message": "Cancelled."}
        if pending is None:
            return {"success": False, "message": "Nothing pending to confirm."}

        allowed, reason = self.safety.check(pending.get("type", ""))
        if not allowed:
            return {"success": False, "message": reason}

        handler = getattr(self, f"_handle_{pending.get('type', '')}", None)
        if handler is None:
            return {"success": False, "message": "That action is unavailable."}
        result = handler(pending)
        result["_confirmed"] = True
        return result

    def _handle_camera_action(self, action: str) -> dict:
        """Handles camera actions like taking photos or recording video."""
        open_res = self._apps.open("camera")
        if not open_res.get("success"):
            return open_res

        time.sleep(1.5)
        try:
            import pyautogui
            low_action = action.lower()

            if "video" in low_action:
                if "start" in low_action or "record" in low_action:
                    pyautogui.press('tab')
                    time.sleep(0.3)
                    pyautogui.press('space')
                    return {"success": True, "message": "Started video recording."}
                elif "stop" in low_action:
                    pyautogui.press('space')
                    return {"success": True, "message": "Stopped video recording."}

            pyautogui.press('space')
            return {"success": True, "message": "Photo captured!"}
        except Exception as exc:
            return {"success": False, "message": f"Camera action failed: {exc}"}

    def _handle_normal(self, intent) -> dict:
        text = (intent.get("text") if isinstance(intent, dict) else str(intent)) or ""
        low = text.lower().strip()

        # Camera Sub-Features
        if re.search(r"\b(?:take\s+a?\s*)?photo\b|\bclick\s+(?:a\s+)?photo\b|\bsnap\b", low):
            return self._handle_camera_action("photo")
        if re.search(r"\bstart\s+(?:recording\s+)?video\b|\brecord\s+video\b", low):
            return self._handle_camera_action("start video")
        if re.search(r"\bstop\s+(?:recording\s+)?video\b|\bstop\s+video\b", low):
            return self._handle_camera_action("stop video")

        # UI Automation Handlers (Clicking on screen elements)
        click_match = re.search(r"\bclick\s+(?:on\s+)?(.+)", low)
        if click_match and "window" not in low:
            return self.ui.click_text(click_match.group(1).strip())

        if re.search(r"\bshut\s*down(\s+pc|\s+computer)?\b", low):
            return self._system.power("shutdown")
        if re.search(r"\brestart(\s+pc|\s+computer)?\b", low):
            return self._system.power("restart")
        if re.search(r"\bsleep(\s+pc|\s+computer)?\b", low):
            return self._system.power("sleep")

        bt_match = re.search(r"\bturn\s+(on|off)\s+bluetooth\b|\bbluetooth\s+(on|off)\b", low)
        if bt_match:
            action = "on" if "on" in bt_match.group(0) else "off"
            return self._system.bluetooth_toggle(action)

        compound = re.match(r"\b(open|launch|start)\s+(.+?)\s+(?:and|,|then)\s+(.+)$", low)
        if compound:
            return self._handle_compound(compound.group(2).strip(), compound.group(3).strip(), text)

        if re.search(r"\b(play|resume)(\s+(music|video|song))?\b", low) and "youtube" not in low:
            return self._system.media_key("play_pause")
        if re.search(r"\bpause\b|\bstop\s*(the)?\s*(music|video|song|playback)\b", low):
            return self._system.media_key("play_pause")
        if re.search(r"\bnext\s*(track|song|video)\b|\bskip\b", low):
            return self._system.media_key("next")
        if re.search(r"\bprevious\s*(track|song|video)\b", low):
            return self._system.media_key("previous")

        if re.search(r"\b(volume|sound)\s*up\b|\bturn up\b", low):
            return self._system.volume_step(+1)
        if re.search(r"\b(volume|sound)\s*down\b|\bturn down\b", low):
            return self._system.volume_step(-1)
        if re.search(r"\b(unmute)\b", low):
            return self._system.mute_toggle()

        if re.fullmatch(r"(lock( (the )?(pc|computer|screen))?)", low):
            return self._system.power("lock")

        if re.search(r"\bminimi[sz]e\b", low):
            return self._system.window_action("minimize")
        if re.search(r"\bmaximi[sz]e\b|\bfull ?screen\b", low):
            return self._system.window_action("maximize")
        if re.search(r"\bswitch window\b", low):
            return self._system.window_action("switch")
        if re.search(r"\bshow desktop\b", low):
            return self._system.window_action("show_desktop")

        match = re.match(r"\btype\s+(.+)$", low)
        if match:
            return self._system.type_text(match.group(1))

        match = re.search(r"\bopen\s+(?:my\s+)?(downloads?|documents?|pictures?|videos?|desktop|music)\b", low)
        if match:
            return self._system.open_folder(match.group(1))

        decision = self.router.route(text)
        tool, args = decision["tool"], decision["args"]
        logger.info("Routed to %s %s", tool, args)

        dispatch_map = {
            "open_app": lambda: self._apps.open(args.get("name", "")),
            "close_app": lambda: self._apps.close(args.get("name", "")),
            "set_volume": lambda: self._system.set_volume(args.get("value")),
            "set_brightness": lambda: self._system.set_brightness(args.get("value")),
            "take_screenshot": self._system.screenshot,
            "web_search": lambda: self._web_search(args.get("query", ""), text),
        }
        handler_fn = dispatch_map.get(tool)
        return handler_fn() if handler_fn else {"success": False, "message": "I'm not sure how to do that yet."}

    def _handle_compound(self, app_name: str, action_text: str, original: str) -> dict:
        if "youtube" in action_text or "search" in action_text:
            if "youtube" in action_text:
                query = re.sub(r"\b(?:play|search|on|youtube)\b", "", action_text).strip()
                return self._youtube_common(query or None, original)
            return self._web_search(action_text, original)

        open_result = self._apps.open(app_name)
        if not open_result.get("success"):
            return open_result

        time.sleep(2.5)
        safe_keys = {"enter", "space", "escape", "tab"}
        key = re.sub(r"\b(?:press|click|hit|tap)\b\s*", "", action_text).strip()
        if key in safe_keys:
            try:
                import pyautogui
                pyautogui.press(key)
                return {"success": True, "message": f"Opened {app_name} and pressed {key}."}
            except Exception as exc:
                return {"success": False, "message": f"Opened {app_name}, but follow-up failed: {exc}"}
        return {"success": True, "message": f"Opened {app_name}. I don't know how to '{action_text}' yet."}

    def _navigate_in_browser(self, browser: str, url: str) -> dict:
        exe = self._apps.find_browser_exe(browser)
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen(f'"{exe}" --new-window "{url}"', shell=True)
                return {"success": True}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        try:
            os.startfile(url)
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _handle_browser_close_all_tabs(self, intent: dict) -> dict:
        result = self.browser.close_all_tabs()
        return ({"success": True, "message": "Closed all browser windows.", **result}
                if result.get("success") else {"success": False, "message": "Couldn't close tabs."})

    def _handle_browser_close_tab(self, intent: dict) -> dict:
        return self._to_message(self.browser.close_current_tab(), "Closed this tab.", "Couldn't close tab.")

    def _handle_youtube_open(self, intent: dict) -> dict:
        return self._youtube_common(None, intent.get("text", ""))

    def _handle_youtube(self, intent: dict) -> dict:
        return self._youtube_common(intent.get("query"), intent.get("text", ""))

    def _youtube_common(self, query: Optional[str], text: str = "") -> dict:
        source = query or text
        source, browser = _extract_browser(source)
        effective_query = (source.strip() or None) if query is not None else None

        url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(effective_query)
               if effective_query else "https://www.youtube.com/")
        if browser:
            result = self._navigate_in_browser(browser, url)
            return ({"success": True, "message": f"Opening YouTube in {browser}."}
                    if result["success"] else {"success": False, "message": result["error"]})

        result = self.browser.navigate(url)
        if not result.get("success"):
            return {"success": False, "message": "YouTube didn't load."}
        return {"success": True,
                "message": "Opening YouTube." if not effective_query else f"Playing {effective_query} on YouTube."}

    def _handle_browser_navigation(self, intent: dict) -> dict:
        target = intent.get("target", "")
        _, browser = _extract_browser(intent.get("text", ""))
        url = target if target.startswith(("http://", "https://")) else f"https://www.{target}.com"
        if browser:
            return self._to_message(self._navigate_in_browser(browser, url), f"Opening {target} in {browser}.", "Navigation failed.")
        return self._to_message(self.browser.navigate(url), f"Opening {target}.", "Navigation failed.")

    def _web_search(self, query: str, text: str = "") -> dict:
        source, browser = _extract_browser(query or text)
        q = (source.strip() or query or "").strip()
        url = f"https://www.google.com/search?q={urllib.parse.quote_plus(q)}"
        if browser:
            return self._to_message(self._navigate_in_browser(browser, url), f"Searching for {q} in {browser}.", "Search failed.")
        return self._to_message(self.browser.navigate(url), f"Searching for {q}.", "Search failed.")

    @staticmethod
    def _to_message(result: dict, ok_msg: str, fail_msg: str) -> dict:
        success = bool(result.get("success"))
        return {"success": success, "message": ok_msg if success else result.get("error", fail_msg), **result}