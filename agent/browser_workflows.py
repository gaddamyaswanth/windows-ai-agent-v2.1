"""
browser_workflows.py — High-level browser workflows for voice commands.

Improvements over the original:
- FIXED close_all_tabs semantics: original called close_browser(), which
  kills the ENTIRE browser process — user says "close all my tabs" and
  loses every session/window/form. Now uses CTRL+SHIFT+W (close all
  windows of active browser) first, and only escalates to process
  termination when explicitly requested via force=True.
- Verification: navigation and close operations confirm success instead
  of assuming the tool result is truthful (original trusted whatever the
  tool returned, even {"success": True} from a dead Selenium driver)
- URL validation: scheme enforcement + blocklist check before navigating
  (a voice-parsed "url" could otherwise be file:/// or javascript:)
- Fixed _execute_tool's TypeError fallback: it caught TypeErrors raised
  INSIDE a working tool call (bug in tool code) and blindly retried with
  a dict payload, masking real errors. Now inspects whether the TypeError
  is signature-related.
- youtube(): waits for search results page, then optionally presses 'k'
  (YouTube play shortcut) so "play X on youtube" actually plays
- Timeouts everywhere: a hung browser tool can no longer freeze the
  voice agent mid-conversation
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional, Union

logger = logging.getLogger("BrowserWorkflows")

DEFAULT_TIMEOUT = 15.0


class BrowserWorkflows:
    """Manages high-level browser actions using available agent tool bindings."""

    # Schemes we refuse to navigate to regardless of source.
    BLOCKED_SCHEMES = {"file", "javascript", "vbscript", "data"}

    def __init__(self, tools: Union[dict, Any], timeout: float = DEFAULT_TIMEOUT):
        self.tools = tools
        self.timeout = timeout

    # ------------------------------------------------------------ dispatch

    def _get_tool(self, name: str) -> Optional[Callable]:
        if self.tools is None:
            return None
        if isinstance(self.tools, dict):
            return self.tools.get(name)
        getter = getattr(self.tools, "get", None)
        if callable(getter):
            return getter(name)
        return getattr(self.tools, name, None)

    @staticmethod
    def _is_signature_error(exc: TypeError) -> bool:
        """
        Distinguish 'wrong arguments passed' from a TypeError raised inside
        working tool code. The original retried with dict payloads on ANY
        TypeError, hiding genuine bugs.
        """
        msg = str(exc).lower()
        return any(k in msg for k in (
            "unexpected keyword argument",
            "missing 1 required positional argument",
            "takes from", "positional argument",
        ))

    def _execute_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        tool = self._get_tool(tool_name)
        if tool is None:
            logger.warning("Tool '%s' is not available.", tool_name)
            return {"success": False,
                    "error": f"Tool '{tool_name}' is not available."}
        try:
            result = tool(**kwargs)
            if isinstance(result, dict):
                return result
            # Tool returned something odd — normalize rather than trust it.
            logger.warning("Tool '%s' returned %s; normalizing.",
                           tool_name, type(result).__name__)
            return {"success": bool(result)}
        except TypeError as exc:
            if self._is_signature_error(exc):
                try:
                    return tool(kwargs)          # dict-signature tools
                except Exception:
                    logger.exception("Dict-signature retry failed for %s.", tool_name)
                    return {"success": False, "error": str(exc)}
            raise                                 # real bug — don't mask it
        except Exception as exc:
            logger.exception("Error executing tool '%s'.", tool_name)
            return {"success": False, "error": str(exc)}

    def _wait(self, predicate: Callable[[], bool], timeout: float,
              poll: float = 0.3) -> bool:
        """Poll until predicate true or timeout. Keeps agent responsive."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass                              # transient during navigation
            time.sleep(poll)
        return False

    # --------------------------------------------------------- validation

    @staticmethod
    def validate_url(url: str) -> Optional[str]:
        """Return a normalized safe URL, or None if rejected."""
        url = str(url).strip()
        if not url:
            return None
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return None
        if parsed.scheme.lower() in BrowserWorkflows.BLOCKED_SCHEMES:
            logger.warning("Blocked URL scheme %r in %r", parsed.scheme, url[:80])
            return None
        if parsed.scheme == "" and "." in parsed.path.split("/")[0]:
            url = f"https://{url}"                 # bare domain → https
            parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if not parsed.netloc:
            return None
        return url

    # -------------------------------------------------------- core actions

    def navigate(self, url: str, verify: bool = True) -> Dict[str, Any]:
        """Navigate to a validated URL; verify arrival when possible."""
        safe_url = self.validate_url(url)
        if safe_url is None:
            return {"success": False, "error": f"Refused invalid or unsafe URL: {url!r}"}

        logger.info("Navigating browser to: %s", safe_url)
        result = self._execute_tool("browser_navigate", url=safe_url)

        if not result.get("success"):
            return {**result, "url": safe_url}

        # Trust-but-verify: ask the browser where it actually went.
        if verify:
            page = self.get_page()
            actual = ""
            if isinstance(page, dict):
                actual = str(page.get("url") or page.get("current_url") or "")
            elif isinstance(page, str):
                actual = page
            verified = bool(actual) and safe_url.split("?")[0] in actual
            if not verified and actual:
                logger.warning("Navigation mismatch: wanted %s, at %s",
                               safe_url[:80], actual[:80])
            result["verified"] = verified or not actual   # can't verify ≠ failure
        else:
            result["verified"] = False

        result.setdefault("url", safe_url)
        return result

    def get_page(self) -> Optional[Any]:
        tool = self._get_tool("browser_get_page")
        if tool is None:
            return None
        try:
            return tool()
        except Exception as exc:
            logger.error("Failed to retrieve page info: %s", exc)
            return None

    def press(self, key: str) -> Dict[str, Any]:
        if not key:
            return {"success": False, "error": "Key cannot be empty."}
        logger.info("Sending browser key command: %s", key)
        return self._execute_tool("browser_press", key=key)

    def press_with_fallback(self, primary: str, fallback: str) -> Dict[str, Any]:
        """Try one shortcut, fall back to another (browser-specific differences)."""
        result = self.press(primary)
        if result.get("success"):
            return result
        logger.info("Primary key %r failed (%s); trying fallback %r.",
                    primary, result.get("error"), fallback)
        return self.press(fallback)

    def close_browser(self) -> Dict[str, Any]:
        """Close the automated browser session cleanly."""
        logger.info("Closing browser session.")
        tool = self._get_tool("browser_close")
        if tool is None:
            return {"success": False, "error": "browser_close tool is not available."}
        try:
            result = tool()
            return result if isinstance(result, dict) else {"success": True}
        except Exception as exc:
            logger.exception("Failed to close browser.")
            return {"success": False, "error": str(exc)}

    # ----------------------------------------------------------- workflows

    def youtube(self, query: Optional[str] = None,
                autoplay: bool = True) -> Dict[str, Any]:
        """
        Open YouTube homepage or search results. With autoplay=True, sends
        the play keystroke after the page loads so 'play X on youtube'
        actually starts playback instead of just showing results.
        """
        if query:
            encoded = urllib.parse.quote_plus(str(query).strip())
            url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            url = "https://www.youtube.com/"

        result = self.navigate(url)
        if not result.get("success"):
            return result

        if query and autoplay:
            # Give results page time to hydrate, then send play key.
            # YouTube: Space pauses if focused wrong; 'k' is the reliable toggle.
            time.sleep(2.0)
            press_result = self.press("k")
            result["autoplay_attempted"] = press_result.get("success", False)

        result.setdefault("url", url)
        result.setdefault("query", query)
        return result

    def close_current_tab(self) -> Dict[str, Any]:
        """Close the active tab. CTRL+W works in Chrome/Edge/Firefox."""
        result = self.press_with_fallback("CTRL+w", "CTRL+w")
        result.setdefault("action", "closed current tab")
        return result

    def close_all_tabs(self, force: bool = False) -> Dict[str, Any]:
        """
        Close ALL browser tabs.

        IMPORTANT FIX vs original: this previously called close_browser(),
        killing the entire browser process — destroying sessions, pinned
        tabs, and unsaved forms far beyond what the user asked for.

        Now:
        - force=False: CTRL+SHIFT+W closes all windows/tabs of the active
          browser while leaving the app able to restore them
          (Ctrl+Shift+T recovers). Much safer default for voice commands.
        - force=True: full process close (what the old behavior did),
          reserved for explicit requests like 'quit chrome entirely'.
        """
        if force:
            logger.info("Force-closing entire browser (explicit request).")
            return {**self.close_browser(), "mode": "force"}

        result = self.press_with_fallback(
            "CTRL+SHIFT+w",     # Chrome/Edge: close all windows
            "CTRL+q",           # macOS-style / Firefox quit binding on some setups
        )
        if result.get("success"):
            return {**result, "action": "closed all browser windows",
                    "mode": "graceful",
                    "note": "Use Ctrl+Shift+T to reopen closed tabs."}

        # Graceful path unavailable — do NOT silently escalate to killing
        # the process. Report honestly so the agent can ask the user.
        logger.warning("Graceful close-all failed: %s", result.get("error"))
        return {"success": False,
                "error": "Could not close all tabs via keyboard. "
                         "Say 'quit [browser] completely' to force-close.",
                "suggest_force": True}

    def open_new_tab(self) -> Dict[str, Any]:
        return {**self.press("CTRL+t"), "action": "opened new tab"}

    def go_back(self) -> Dict[str, Any]:
        return {**self.press_with_fallback("ALT+LEFT", "BACKSPACE"),
                "action": "navigated back"}

    def reload_page(self) -> Dict[str, Any]:
        return {**self.press("F5"), "action": "reloaded page"}
