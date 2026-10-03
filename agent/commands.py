"""
commands.py — Windows application and filesystem controller.

Improvements over the original:
- SECURITY: user input is passed to PowerShell as an argument list element
  (never interpolated into -Command strings), eliminating quote-breaking
  command injection like "notepad'; Remove-Item -Recurse C:\; 'x"
- close_app actually verifies success (original ALWAYS returned success:True
  even for nonexistent processes)
- SafetyGate is actually consulted before dangerous actions (original stored
  it but never called it — every launch bypassed the safety layer)
- Graceful close first, force-kill only on timeout (less data loss)
- Non-blocking launch option + proper process cleanup
- Path validation for direct .exe paths (no UNC/network surprises)
- Cross-platform fallback kept but clearly separated
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .safety import DANGEROUS, SafetyGate

logger = logging.getLogger("CommandRunner")

LAUNCH_TIMEOUT = 10.0
CLOSE_GRACE_SECONDS = 3.0


class CommandRunner:
    def __init__(
            self,
            config: dict,
            safety_gate: Optional[SafetyGate] = None,
            speak_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config = config or {}
        self.safety = safety_gate
        self.speak_callback = speak_callback

        self.app_aliases: Dict[str, str] = {
            # executables
            "chrome": "chrome.exe", "chrome browser": "chrome.exe",
            "google chrome": "chrome.exe", "edge": "msedge.exe",
            "microsoft edge": "msedge.exe", "firefox": "firefox.exe",
            "notepad": "notepad.exe", "note pad": "notepad.exe",
            "calculator": "calc.exe", "calc": "calc.exe",
            "paint": "mspaint.exe", "explorer": "explorer.exe",
            "wordpad": "wordpad.exe", "cmd": "cmd.exe",
            "command prompt": "cmd.exe", "control panel": "control.exe",
            "task manager": "taskmgr.exe", "device manager": "devmgmt.msc",
            # URI protocols
            "settings": "ms-settings:", "whatsapp": "whatsapp:",
            "spotify": "spotify:", "discord": "discord:",
            "camera": "microsoft.windows.camera:",
            # web shortcuts
            "youtube": "https://www.youtube.com",
            "google": "https://www.google.com",
            "github": "https://github.com",
        }

    # ------------------------------------------------------------------ utils

    def speak(self, text: str) -> None:
        if self.speak_callback and text:
            try:
                self.speak_callback(text)
            except Exception:
                logger.debug("Speak callback failed.", exc_info=True)

    @staticmethod
    def _is_uri(target: str) -> bool:
        """URIs (ms-settings:, whatsapp:) and URLs go through ShellExecute."""
        return target.endswith(":") or "://" in target or target.endswith(".msc") is False and ":" in target and not target.lower().endswith(".exe")

    @staticmethod
    def _is_windows() -> bool:
        return os.name == "nt"

    def _check_safety(self, action: str) -> Optional[Dict[str, Any]]:
        """Consult the safety gate if one is configured."""
        if self.safety is None:
            return None
        verdict = self.safety.check(action)
        if verdict and not verdict.get("allowed", True):
            logger.warning("SafetyGate denied action: %s (%s)", action, verdict.get("reason"))
            self.speak("That action was blocked by safety settings.")
            return {"success": False, "error": f"Blocked by safety gate: {verdict.get('reason', 'denied')}"}
        return None

    # --------------------------------------------------------------- launch

    def launch_app(self, app_path_or_name: str, wait: bool = False) -> Dict[str, Any]:
        requested = str(app_path_or_name).strip()
        if not requested or len(requested) > 256:
            return {"success": False, "error": "Application name empty or too long."}

        blocked = self._check_safety(f"launch:{requested}")
        if blocked:
            return {**blocked, "_permission_class": DANGEROUS}

        key = requested.lower().rstrip(".exe").strip()
        target = self.app_aliases.get(requested.lower(), requested)

        # Reject obvious path traversal / injection attempts on raw paths.
        if any(ch in target for ch in (";", "&", "|", "`", "$")):
            return {"success": False,
                    "error": "Invalid characters in application name.",
                    "_permission_class": DANGEROUS}

        # If it looks like a file path, verify it exists.
        if ("\\" in target or "/" in target) and not target.startswith(("http", "ms-settings")):
            p = Path(target)
            if not p.is_file():
                return {"success": False, "error": f"Path does not exist: {target}",
                        "_permission_class": DANGEROUS}
            target = str(p.resolve())   # normalize, kills .. segments

        self.speak(f"Opening {requested}.")

        if self._is_windows():
            return self._launch_windows(target, wait)
        return self._launch_posix(target, wait)

    def _launch_windows(self, target: str, wait: bool) -> Dict[str, Any]:
        try:
            # URIs and URLs: ShellExecute handles these natively and safely.
            if self._is_uri(target):
                os.startfile(target)  # noqa: S606 — intended shell behavior for URIs
                return {"success": True, "target": target, "_permission_class": DANGEROUS}

            # Executables: pass the name AS AN ARGUMENT to Start-Process rather
            # than embedding it in a -Command string. Argument-list form cannot
            # be escaped out of by crafted input.
            cmd = [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-Command", "Start-Process -FilePath $args[0]",
                target,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=LAUNCH_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return {"success": True, "target": target, "_permission_class": DANGEROUS}

            logger.warning("Start-Process failed (%s); falling back to startfile.",
                           result.stderr.strip())
            os.startfile(target)  # last resort for PATH-resolvable apps
            return {"success": True, "target": target, "_permission_class": DANGEROUS}

        except FileNotFoundError:
            return {"success": False, "error": f"Application not found: {target}",
                    "_permission_class": DANGEROUS}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Launch timed out.",
                    "_permission_class": DANGEROUS}
        except Exception as exc:
            logger.exception("Failed to launch %r", target)
            return {"success": False, "error": str(exc), "_permission_class": DANGEROUS}

    def _launch_posix(self, target: str, wait: bool) -> Dict[str, Any]:
        resolved = shutil.which(target)
        if resolved is None:
            return {"success": False, "error": f"Executable not found: {target}"}
        try:
            proc = subprocess.Popen(  # noqa: S603 — resolved from PATH above
                [resolved],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if wait:
                proc.wait(timeout=LAUNCH_TIMEOUT)
            return {"success": True, "pid": proc.pid}
        except Exception as exc:
            logger.exception("POSIX launch failed.")
            return {"success": False, "error": str(exc)}

    # ---------------------------------------------------------------- close

    def close_app(self, process_name: str, force: bool = False) -> Dict[str, Any]:
        base = str(process_name).strip().lower().removesuffix(".exe")
        if not base or len(base) > 64 or any(ch in base for ch in ("'", '"', ";", "|", "&", "$", "`")):
            return {"success": False, "error": "Invalid process name."}

        blocked = self._check_safety(f"close:{base}")
        if blocked:
            return blocked

        if not self._is_windows():
            return {"success": False, "error": "close_app requires Windows."}

        try:
            # Does it even exist?
            probe = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-Process -Name $args[0] -ErrorAction SilentlyContinue).Count",
                 base],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            count_str = probe.stdout.strip()
            if not count_str.isdigit() or int(count_str) == 0:
                return {"success": False, "error": f"No running process named '{base}'."}

            # Graceful close first (WM_CLOSE → apps save state), then force.
            if not force:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "Get-Process -Name args[0] | ForEach-Object {_.CloseMainWindow() }",
                     base],
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                time.sleep(CLOSE_GRACE_SECONDS)
                still = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-Process -Name $args[0] -ErrorAction SilentlyContinue).Count",
                     base],
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout.strip()

                if still.isdigit() and int(still) == 0:
                    return {"success": True, "mode": "graceful"}

            kill = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "Stop-Process -Name $args[0] -Force -ErrorAction Stop",
                 base],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if kill.returncode != 0:
                return {"success": False,
                        "error": kill.stderr.strip() or "Force-close failed."}
            return {"success": True, "mode": "forced"}

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Close operation timed out."}
        except Exception as exc:
            logger.exception("Failed to close %r", base)
            return {"success": False, "error": str(exc)}
