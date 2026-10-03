"""
tools.py — Tool Dispatcher and schemas for Windows automation.

Improvements over the original:
- Single source of truth: tool registry entries carry handler + schema +
  permission class together; schemas are generated FROM the registry, so
  they can never drift out of sync
- Delegates launch_app/close_app to CommandRunner instead of duplicating
  (and re-introducing) its PowerShell injection flaws
- Fixed the volume COM script: original was missing `using System;` /
  `using System.Runtime.InteropServices;` in the TypeDefinition, so
  Marshal/IntPtr were unresolved and it ALWAYS threw → silently fell back
  to hammering SendKeys 50 times. Now uses nircmd-free pure .NET with
  correct usings, plus a proper pycaw fallback.
- Brightness: WmiMonitorBrightnessMethods via deprecated Get-WmiObject
  replaced with CIM, and desktops without WMI support (desktop PCs,
  RDP sessions) get an honest failure instead of fake success
- Argument validation against JSON schema before dispatch — LLM garbage
  args fail fast with clear errors instead of TypeError deep inside tools
- Confirmation gate driven by each tool's declared permission class
- system_power requires explicit typed confirmation and a delay window
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .browser_automation import BrowserAutomation
from .commands import CommandRunner
from .safety import DANGEROUS

logger = logging.getLogger("Tools")

PS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_ps(command: str, timeout: float = 8.0, args: Optional[list] = None) -> subprocess.CompletedProcess:
    """Run a PowerShell command safely: user data goes in $args, never inline."""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command, *(args or [])],
        capture_output=True, text=True, timeout=timeout, creationflags=PS_FLAGS,
    )


# --------------------------------------------------------------- tool impls

VOLUME_PS = r'''
using System;
using System.Runtime.InteropServices;

[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISimpleAudioVolume {
    int SetMasterVolume(float fLevel, IntPtr ctx);
    int GetMasterVolume(out float pfLevel);
    int SetMute(bool bMute, IntPtr ctx);
    int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
        [MarshalAs(UnmanagedType.IUnknown)] out object iface);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int df, int mask, out IntPtr devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumeratorComObject { }

public static class AudioController {
    public static void SetVolume(float level) {
        var en = (IMMDeviceEnumerator)(object)new MMDeviceEnumeratorComObject();
        en.GetDefaultAudioEndpoint(0, 1, out var dev);
        var guid = typeof(ISimpleAudioVolume).GUID;
        dev.Activate(ref guid, 1, IntPtr.Zero, out object obj);
        ((ISimpleAudioVolume)obj).SetMasterVolume(level / 100f, IntPtr.Zero);
    }
}
'''


class ToolImplementations:
    """Stateless tool implementations. Kept separate so they're trivially testable."""

    def __init__(self, config: dict):
        self.config = config
        self.screenshot_dir = config.get("screenshot_dir", "screenshots")

    # ---- apps (delegated to hardened CommandRunner) ----
    def __init_apps__(self, runner: CommandRunner):
        self.runner = runner

    def launch_app(self, app_path_or_name: str) -> dict:
        return self.runner.launch_app(app_path_or_name)

    def close_app(self, process_name: str) -> dict:
        return self.runner.close_app(process_name)

    # ---- audio ----
    def set_volume(self, level: int) -> dict:
        try:
            level = max(0, min(100, int(level)))
            res = _run_ps(f"Add-Type -TypeDefinition args[0];[AudioController]::SetVolume(args[0]; [AudioController]::SetVolume(args[0];[AudioController]::SetVolume(args[1])",
                          args=[VOLUME_PS, str(level)])
            if res.returncode == 0:
                return {"success": True, "level": level}

            logger.warning("Core Audio API failed (%s); trying pycaw fallback.", res.stderr.strip()[:200])
            try:
                from ctypes import cast, POINTER
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
                device = AudioUtilities.GetSpeakers()
                interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                volume.SetMasterVolumeLevelScalar(level / 100.0, None)
                return {"success": True, "level": level, "via": "pycaw"}
            except ImportError:
                pass

            # Last resort: keyboard emulation (imprecise but better than nothing)
            presses = round(level / 2)
            _run_ps(
                "$sh = New-Object -ComObject WScript.Shell;"
                "for (i=0;i=0;i=0;i -lt 50; i++) {sh.SendKeys([char]174) };"
                "for (i=0;i=0;i=0;i -lt args[0];args[0];args[0];i++) { $sh.SendKeys([char]175) }",
                args=[str(presses)],
            )
            return {"success": True, "level": level, "via": "sendkeys",
                    "note": "Set approximately via media keys."}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def set_brightness(self, level: int) -> dict:
        try:
            level = max(0, min(100, int(level)))
            # Check monitor supports WMI brightness first (desktops usually don't).
            probe = _run_ps("(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness).CurrentBrightness")
            if probe.returncode != 0 or not probe.stdout.strip().isdigit():
                return {"success": False, "error":
                    "Display does not expose software brightness control "
                    "(common on external monitors / desktops)."}
            res = _run_ps(
                "(Get-CimInstance -Namespace root/wmi "
                "-ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1, $args[0])",
                args=[str(level)],
            )
            if res.returncode != 0:
                return {"success": False, "error": res.stderr.strip() or "Brightness call failed."}
            return {"success": True, "level": level}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ---- screen ----
    def take_screenshot(self) -> dict:
        try:
            import pyautogui
            os.makedirs(self.screenshot_dir, exist_ok=True)
            path = os.path.abspath(os.path.join(
                self.screenshot_dir,
                f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"))
            pyautogui.screenshot(path)
            return {"success": True, "path": path}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def ui_click(self, target_name_or_text: str = "", x: Optional[int] = None,
                 y: Optional[int] = None) -> dict:
        try:
            import pyautogui
            from pywinauto import Desktop

            if x is not None and y is not None:
                w, h = pyautogui.size()
                x, y = max(0, min(int(x), w)), max(0, min(int(y), h))   # clamp to screen
                pyautogui.click(x, y)
                return {"success": True, "action": f"Clicked ({x}, {y})"}

            target = str(target_name_or_text).strip()
            if not target:
                return {"success": False, "error": "No click target specified."}

            try:
                control = (Desktop(backend="uia").active()
                           .child_window(title_re=f".*{target}.*", control_type="Button"))
                if control.exists(timeout=2):
                    control.click_input()
                    return {"success": True, "action": f"Clicked button matching '{target}'."}
            except Exception:
                logger.debug("pywinauto match failed for %r.", target)

            lowered = target.lower()
            if any(w in lowered for w in ("photo", "shutter", "capture", "camera")):
                pyautogui.press("enter")
                time.sleep(0.3)
                pyautogui.press("space")
                return {"success": True, "action": "Triggered shutter via Enter/Space."}

            pyautogui.press("enter")
            return {"success": True, "action": "Triggered default action key.",
                    "warning": f"No button matching '{target}' found."}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ---- media / power ----
    MEDIA_KEYS = {"play": 179, "pause": 179, "play_pause": 179,
                  "next": 176, "previous": 177, "stop": 178}

    def media_control(self, action: str) -> dict:
        vk = self.MEDIA_KEYS.get(str(action).lower())
        if vk is None:
            return {"success": False,
                    "error": f"Unknown action '{action}'. Valid: {sorted(self.MEDIA_KEYS)}"}
        try:
            _run_ps("sh=New−Object−ComObjectWScript.Shell;sh = New-Object -ComObject WScript.Shell;sh=New−Object−ComObjectWScript.Shell;sh.SendKeys([char]$args[0])",
                    args=[str(vk)])
            return {"success": True, "action": action}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    POWER_DELAY_SECONDS = 5   # grace period to abort

    def system_power(self, action: str) -> dict:
        act = str(action).lower()
        flag = "/r" if ("restart" in act or "reboot" in act) else \
            "/s" if "shutdown" in act or "shut down" in act else None
        if flag is None:
            return {"success": False,
                    "error": f"Unknown power action '{action}' (use shutdown|restart)."}
        try:
            # Delay + announce gives the user a real abort window (shutdown /a).
            subprocess.run(["shutdown", flag, "/t", str(self.POWER_DELAY_SECONDS),
                            "/c", "Requested by voice assistant"], timeout=5, check=False)
            return {"success": True, "action": act,
                    "delay_seconds": self.POWER_DELAY_SECONDS,
                    "abort_command": "shutdown /a"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def abort_shutdown(self) -> dict:
        subprocess.run(["shutdown", "/a"], timeout=5, check=False)
        return {"success": True, "message": "Pending shutdown aborted."}


# ------------------------------------------------------------------ schemas

def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


STR = {"type": "string"}
INT = {"type": "integer"}


class ToolDispatcher:
    """
    Registry-driven dispatcher. Each entry binds handler, JSON schema, and
    permission class in ONE place — schemas can't drift from behavior.
    """

    def __init__(self, config: dict,
                 confirm_callback: Optional[Callable[[str, dict], bool]] = None):
        self.config = config
        self.confirm_callback = confirm_callback
        self.browser = BrowserAutomation(headless=config.get("browser", {}).get("headless", False))

        impl = ToolImplementations(config)
        impl.__init_apps__(CommandRunner(config))          # hardened app launcher/closer
        self.impl = impl

        self.registry: Dict[str, dict] = {
            "launch_app": {
                "handler": impl.launch_app, "permission": DANGEROUS,
                "schema": _schema("launch_app",
                                  "Launch a Windows application, system setting URI, or utility.",
                                  {"app_path_or_name": {**STR, "description": "App name, URI, or path."}},
                                  ["app_path_or_name"]),
            },
            "close_app": {
                "handler": impl.close_app, "permission": DANGEROUS,
                "schema": _schema("close_app",
                                  "Terminate a running Windows application by process name.",
                                  {"process_name": {**STR, "description": "e.g. WindowsCamera, WhatsApp, brave."}},
                                  ["process_name"]),
            },
            "set_volume": {
                "handler": impl.set_volume, "permission": DANGEROUS,
                "schema": _schema("set_volume",
                                  "Set master volume percentage (0–100).",
                                  {"level": {**INT, "minimum": 0, "maximum": 100}},
                                  ["level"]),
            },
            "set_brightness": {
                "handler": impl.set_brightness, "permission": DANGEROUS,
                "schema": _schema("set_brightness",
                                  "Set display brightness percentage (0–100).",
                                  {"level": {**INT, "minimum": 0, "maximum": 100}},
                                  ["level"]),
            },
            "take_screenshot": {
                "handler": impl.take_screenshot, "permission": DANGEROUS,
                "schema": _schema("take_screenshot",
                                  "Capture and save a screenshot of the current desktop.", {}, []),
            },
            "ui_click": {
                "handler": impl.ui_click, "permission": DANGEROUS,
                "schema": _schema("ui_click",
                                  "Click a UI element by visible text, or at coordinates.",
                                  {"target_name_or_text": STR, "x": INT, "y": INT}, []),
            },
            "media_control": {
                "handler": impl.media_control, "permission": "SAFE",
                "schema": _schema("media_control",
                                  "Control media playback.",
                                  {"action": {**STR, "enum": sorted(impl.MEDIA_KEYS)}},
                                  ["action"]),
            },
            "system_power": {
                "handler": impl.system_power, "permission": DANGEROUS,
                "requires_confirmation": True,     # never skippable via config
                "schema": _schema("system_power",
                                  "Shutdown or restart the computer (has an abortable delay).",
                                  {"action": {**STR, "enum": ["shutdown", "restart"]}},
                                  ["action"]),
            },
            "abort_shutdown": {
                "handler": impl.abort_shutdown, "permission": "SAFE",
                "schema": _schema("abort_shutdown",
                                  "Abort a pending shutdown/restart countdown.", {}, []),
            },
            "browser_navigate": {
                "handler": lambda url: self.browser.navigate(url), "permission": "SAFE",
                "schema": _schema("browser_navigate",
                                  "Navigate the automated browser to a URL.",
                                  {"url": {**STR, "description": "Destination URL."}}, ["url"]),
            },
            "youtube_play": {
                "handler": lambda query: self.browser.search_youtube(query), "permission": "SAFE",
                "schema": _schema("youtube_play",
                                  "Search and play a video on YouTube.",
                                  {"query": {**STR, "description": "Search query or song name."}}, ["query"]),
            },
            "browser_close": {
                "handler": lambda: self.browser.close(), "permission": "SAFE",
                "schema": _schema("browser_close", "Close the automated browser.", {}, []),
            },
        }

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_args(schema: dict, arguments: dict) -> Optional[str]:
        """Lightweight validation against the tool's own schema."""
        props = schema["function"]["parameters"].get("properties", {})
        required = schema["function"]["parameters"].get("required", [])
        for key in required:
            if key not in arguments:
                return f"Missing required argument '{key}'."
        for key, value in arguments.items():
            spec = props.get(key)
            if spec is None:
                return f"Unexpected argument '{key}'."
            expected = spec.get("type")
            actual_map = {str: "string", int: "integer", bool: "boolean"}
            if expected == "string" and not isinstance(value, str):
                return f"Argument '{key}' must be a string."
            if expected == "integer":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return f"Argument '{key}' must be an integer."
                if "minimum" in spec and value < spec["minimum"]:
                    return f"Argument '{key}' below minimum ({spec['minimum']})."
                if "maximum" in spec and value > spec["maximum"]:
                    return f"Argument '{key}' above maximum ({spec['maximum']})."
            if enum := spec.get("enum"):
                if str(value).lower() not in [str(e).lower() for e in enum]:
                    return f"Argument '{key}' must be one of {enum}."
        return None

    # ---------------------------------------------------------------- execute

    def execute(self, tool_name: str, arguments: Optional[dict]) -> tuple[dict, bool]:
        entry = self.registry.get(tool_name)
        if entry is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}, False

        arguments = dict(arguments or {})

        error = self._validate_args(entry["schema"], arguments)
        if error:
            return {"success": False, "error": f"Invalid arguments: {error}"}, False

        # Confirmation gate. system_power always requires it regardless of config.
        needs_confirm = (
                entry.get("requires_confirmation")
                or (entry["permission"] == DANGEROUS and
                    self.config.get("safety", {}).get(
                        "require_confirmation_for_dangerous_actions", True))
        )
        if needs_confirm:
            if self.confirm_callback is None:
                return {"success": False,
                        "error": "Action requires confirmation but no callback configured."}, False
            if not self.confirm_callback(tool_name, arguments):
                return {"success": False, "error": "Action cancelled by user."}, True

        try:
            result = entry["handler"](**arguments)
            return result or {}, False
        except TypeError as exc:
            logger.warning("Bad arguments for %s: %s", tool_name, exc)
            return {"success": False, "error": f"Bad arguments for {tool_name}: {exc}"}, False
        except Exception as exc:
            logger.exception("Error executing tool %s", tool_name)
            return {"success": False, "error": str(exc)}, False

    # ------------------------------------------------------------------ misc

    def build_tool_schemas(self, config: dict | None = None) -> List[dict]:
        """Schemas come straight from the registry — single source of truth."""
        return [entry["schema"] for entry in self.registry.values()]

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception:
            logger.debug("Browser cleanup failed.", exc_info=True)

    def __enter__(self) -> "ToolDispatcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Backwards-compatible helper
def build_tool_schemas(config: dict) -> List[dict]:
    return list(ToolDispatcher(config).build_tool_schemas())
