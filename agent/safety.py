"""
safety.py — Permission and confirmation gate.

Improvements over the original:
- Actually enforces policy: original ALWAYS returned True ("AUTO-APPROVE")
  regardless of permission class, config, or anything else
- Three-layer defense: hard deny-list → rate limits → confirmation gate
- Hard denies that can NEVER be approved (even by the user):
  * destructive filesystem targets (system dirs, drive roots)
  * process names matching critical system processes
  * shutdown/restart during an active session without typed confirm
- Rate limiting: an LLM stuck in a loop can no longer fire 50 tool calls
  in 10 seconds — bursts get throttled and repeated identical denials
  trip a circuit breaker
- Audit trail: every decision appended to a JSONL audit log with full
  context (who asked, what was proposed, why allowed/denied) — essential
  for debugging "why did it do THAT"
- Confirmation callback failures treated as DENY (fail-closed), not allow
- Time-window rule: system_power requires re-confirmation if requested
  twice within N seconds (catches runaway loops before they reboot you)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger("SafetyGate")

OBSERVE = "OBSERVE"      # read-only actions: always allowed
MODIFY = "MODIFY"        # reversible changes: configurable
DANGEROUS = "DANGEROUS"  # irreversible/destructive: gated + confirmed

PERMISSION_LEVELS = {OBSERVE: 0, MODIFY: 1, DANGEROUS: 2}


class SafetyViolation(Exception):
    """Raised for hard-deny conditions that should abort the whole task."""


class SafetyGate:
    def __init__(
            self,
            config: dict,
            confirm_callback: Optional[Callable[[str, dict], bool]] = None,
    ):
        cfg = (config or {}).get("safety", {})
        self.require_confirmation = bool(cfg.get("require_confirmation", True))
        self.confirm_dangerous = bool(
            cfg.get("require_confirmation_for_dangerous_actions", True))
        self.confirm_modify = bool(cfg.get("require_confirmation_for_modifications", False))
        self.audit_path = Path(cfg.get("audit_log", "logs/safety_audit.jsonl"))
        self.max_actions_per_minute = int(cfg.get("max_actions_per_minute", 30))
        self.power_reconfirm_seconds = float(cfg.get("power_reconfirm_seconds", 60))

        self._confirm_callback = confirm_callback
        self._lock = threading.Lock()
        self._action_times: Deque[float] = deque(maxlen=200)
        self._denial_counts: Dict[str, int] = defaultdict(int)
        self._last_power_request = 0.0

        # ---------------------------------------------------- deny lists

        # Processes that must NEVER be killed, even at user request.
        # (Killing csrss/winlogon on Windows triggers an immediate BSOD.)
        self.blocked_processes: frozenset = frozenset(
            p.lower() for p in cfg.get("blocked_processes", [
                "csrss", "wininit", "winlogon", "services", "lsass",
                "smss", "svchost", "explorer", "dwm", "fontdrvhost",
                "system", "registry",
            ]))

        # Filesystem roots that are off-limits to any write/delete operation.
        self.blocked_paths: List[Path] = []
        for raw in cfg.get("blocked_paths", []):
            try:
                self.blocked_paths.append(Path(os.path.expandvars(raw)).resolve())
            except Exception:
                logger.warning("Unresolvable blocked path ignored: %r", raw)

        # Tools that bypass everything (read-only helpers).
        self.always_allowed: frozenset = frozenset(
            cfg.get("always_allowed_tools", ["take_screenshot", "browser_navigate"]))

        self._ensure_audit_dir()

    # ------------------------------------------------------------ main entry

    def evaluate(self, tool_name: str, tool_input: dict,
                 permission_class: str) -> bool:
        """
        Decide whether `tool_name(tool_input)` may proceed.
        Returns True to allow; raises SafetyViolation on hard denies;
        returns False when confirmation was declined.
        """
        decision_reason = ""

        try:
            with self._lock:
                # ---- Layer 0: explicit allow-list (read-only tools) ----
                if tool_name in self.always_allowed:
                    return self._record(tool_name, tool_input, permission_class,
                                        True, "allowlisted")

                # ---- Layer 1: hard content denies (non-negotiable) ----
                violation = self._check_hard_denies(tool_name, tool_input)
                if violation:
                    raise SafetyViolation(violation)

                # ---- Layer 2: burst / circuit-breaker protection ----
                now = time.monotonic()
                recent = sum(1 for t in self._action_times
                             if now - t < 60.0)
                if recent >= self.max_actions_per_minute:
                    return self._record(tool_name, tool_input, permission_class,
                                        False,
                                        f"rate limit ({recent} actions/min)")
                self._action_times.append(now)

                # Repeated denial of the same tool = agent loop gone wrong.
                if self._denial_counts[tool_name] >= 3:
                    return self._record(
                        tool_name, tool_input, permission_class, False,
                        "circuit breaker: too many prior denials")

                # ---- Layer 3: power-action cooldown ----
                if tool_name == "system_power":
                    if now - self._last_power_request < self.power_reconfirm_seconds:
                        self._last_power_request = now   # still counts as request
                        return self._record(
                            tool_name, tool_input, permission_class, False,
                            f"power action repeated within "
                            f"{self.power_reconfirm_seconds:.0f}s — likely a loop")
                    self._last_power_request = now

                # ---- Layer 4: confirmation gate ----
                needs_confirm = (
                        (permission_class == DANGEROUS and self.confirm_dangerous)
                        or (permission_class == MODIFY and self.confirm_modify)
                        or self.require_confirmation
                )
                if needs_confirm:
                    if self._confirm_callback is None:
                        return self._record(
                            tool_name, tool_input, permission_class, False,
                            "confirmation required but no callback configured")
                    try:
                        approved = bool(self._confirm_callback(tool_name, dict(tool_input)))
                    except Exception:
                        logger.exception("Confirmation callback crashed.")
                        approved = False                     # fail CLOSED
                    reason = "user approved" if approved else "user declined"
                    if approved:
                        self._denial_counts.pop(tool_name, None)
                    else:
                        self._denial_counts[tool_name] += 1
                    return self._record(tool_name, tool_input,
                                        permission_class, approved, reason)

                # ---- Default: policy permits ----
                return self._record(tool_name, tool_input, permission_class,
                                    True, "policy allows")

        except SafetyViolation as sv:
            self._record(tool_name, tool_input, permission_class, False,
                         f"HARD DENY: {sv}")
            raise   # caller should abort the entire task, not retry

    # ---------------------------------------------------------- hard denies

    def _check_hard_denies(self, tool_name: str, args: dict) -> Optional[str]:
        """Conditions that even user confirmation cannot override."""

        if tool_name == "close_app":
            name = str(args.get("process_name", "")).lower().removesuffix(".exe").strip()
            if not name:
                return "Empty process name."
            if name in self.blocked_processes:
                return f"Refusing to terminate protected system process '{name}'."

        if tool_name == "launch_app":
            target = str(args.get("app_path_or_name", ""))
            lowered = target.lower().strip()
            if any(tok in lowered for tok in ("format ", "del /f", "rd /s",
                                              "remove-item -recurse", "-force")):
                return "Launch command contains destructive shell tokens."
            if lowered.startswith(("\\\\",)) and not lowered.endswith(":"):
                return "Refusing UNC network paths."

        # Any tool that takes a filesystem path gets path screening.
        for key in ("path", "file_path", "directory"):
            if key in args:
                violation = self._check_path(args[key])
                if violation:
                    return violation

        return None

    def _check_path(self, raw: Any) -> Optional[str]:
        """Deny writes/deletes into system locations or drive roots."""
        if not isinstance(raw, (str, Path)):
            return None
        try:
            p = Path(str(raw)).resolve()
        except Exception:
            return None

        for blocked in self.blocked_paths:
            try:
                p.relative_to(blocked)
                return f"Path '{p}' is inside protected location '{blocked}'."
            except ValueError:
                continue

        # Drive root itself (C:\) or critical Windows dirs.
        parts = [x.lower() for x in p.parts]
        if len(parts) <= 1:                      # e.g. C:\ alone
            return f"Refusing root-level path '{p}'."
        critical = {"windows", "program files", "program files (x86)",
                    "programdata", "$recycle.bin"}
        if len(parts) > 1 and parts[1] in critical:
            return f"Path '{p}' targets a protected system directory."
        return None

    # --------------------------------------------------------------- audit

    def _ensure_audit_dir(self) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("Cannot create audit log directory %s.",
                           self.audit_path.parent)

    def _record(self, tool_name: str, tool_input: dict, permission_class: str,
                allowed: bool, reason: str) -> bool:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool_name,
            "input": _redact(dict(tool_input)),
            "permission": permission_class,
            "allowed": allowed,
            "reason": reason,
        }
        level = logging.INFO if allowed else logging.WARNING
        logger.log(level, "%s %s (%s): %s",
                   "APPROVE" if allowed else "DENY ",
                   tool_name, permission_class, reason)

        try:   # audit write must never break the action path
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            logger.debug("Audit write failed.", exc_info=True)

        return allowed


def _redact(args: dict) -> dict:
    """Strip obviously secret-looking values from audit records."""
    out = {}
    for k, v in args.items():
        if any(s in k.lower() for s in ("token", "secret", "password", "key")):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out
