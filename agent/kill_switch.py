"""
agent/kill_switch.py — Emergency stop mechanism (instrumented).

API:
    is_stopped() -> bool        check anywhere in any thread
    trip(reason="")             activate the stop (logs caller stack trace)
    reset()                     clear the stop (new session)
    on_stop(fn) / on_resume(fn) register callbacks (TTS silence etc.)

Thread-safe via lock; callbacks invoked synchronously on trip/reset.

NOTE: trip() must ONLY be called from emergency-stop paths:
  - main.py's SIGINT handler / shutdown finally-block
  - the configured emergency hotkey
If any module calls trip() during init or error handling, the agent
silently dead-ends — this instrumented version logs a full stack trace
of the caller so rogue call sites can be found and removed.
"""

import logging
import threading
import traceback

logger = logging.getLogger("KillSwitch")

_lock = threading.Lock()
_stopped = False
_stop_reason = ""
_stop_callbacks = []
_resume_callbacks = []


def is_stopped() -> bool:
    with _lock:
        return _stopped


def get_reason() -> str:
    with _lock:
        return _stop_reason


def trip(reason: str = "") -> None:
    """Activate emergency stop. Idempotent. Logs caller stack trace."""
    global _stopped, _stop_reason
    with _lock:
        if _stopped:
            return                      # already tripped; don't re-log
        _stopped = True
        _stop_reason = reason or "unspecified"
        callbacks = list(_stop_callbacks)

    logger.warning("KILL SWITCH TRIPPED: %s", _stop_reason)
    logger.warning("TRIP CALLER STACK TRACE:\n%s",
                   "".join(traceback.format_stack()))
    for cb in callbacks:
        try:
            cb()
        except Exception:
            logger.exception("kill_switch stop-callback failed.")


def reset() -> None:
    """Clear the stop state."""
    global _stopped, _stop_reason
    with _lock:
        _stopped = False
        _stop_reason = ""
        callbacks = list(_resume_callbacks)
    logger.info("Kill switch reset.")
    for cb in callbacks:
        try:
            cb()
        except Exception:
            logger.exception("kill_switch resume-callback failed.")


def on_stop(callback) -> None:
    with _lock:
        _stop_callbacks.append(callback)


def on_resume(callback) -> None:
    with _lock:
        _resume_callbacks.append(callback)
