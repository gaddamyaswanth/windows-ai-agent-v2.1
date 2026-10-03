"""
browser.py — Playwright browser controller.

Supports installed Windows browsers:

    - Brave
    - Chrome
    - Edge
    - Firefox

The browser is selected with:

    browser.set_browser("brave")

or through config:

    browser:
        name: brave

The controller launches the installed browser executable rather than
automatically launching Playwright's bundled Chromium.

No persistent browser profile is used by default.
"""

import os
import shutil
from pathlib import Path


class BrowserController:

    SUPPORTED_BROWSERS = {
        "brave",
        "chrome",
        "edge",
        "firefox",
    }

    def __init__(
            self,
            config,
            safety_gate=None,
    ):

        self.config = config
        self.safety = safety_gate

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

        browser_config = config.get(
            "browser",
            {},
        )

        self.browser_name = str(
            browser_config.get(
                "name",
                "chrome",
            )
        ).lower().strip()

        self.explicit_executable = (
            browser_config.get(
                "executable_path"
            )
        )

        self.headless = bool(
            browser_config.get(
                "headless",
                False,
            )
        )

        self.download_dir = os.path.expandvars(
            browser_config.get(
                "download_dir",
                "Downloads",
            )
        )

        os.makedirs(
            self.download_dir,
            exist_ok=True,
        )

    # ================================================================
    # BROWSER SELECTION
    # ================================================================

    def set_browser(
            self,
            browser_name,
    ):

        name = str(
            browser_name
        ).lower().strip()

        aliases = {
            "brave-browser": "brave",
            "brave browser": "brave",
            "google-chrome": "chrome",
            "google chrome": "chrome",
            "microsoft-edge": "edge",
            "microsoft edge": "edge",
        }

        name = aliases.get(
            name,
            name,
        )

        if name not in self.SUPPORTED_BROWSERS:

            raise ValueError(
                "Unsupported browser: "
                + name
            )

        # If a browser is already running through Playwright,
        # close that session before changing browsers.
        if self._browser:

            self.close()

        self.browser_name = name

        print(
            "[browser] Selected browser: "
            + self.browser_name
        )

    # ================================================================
    # WINDOWS EXECUTABLE DETECTION
    # ================================================================

    @staticmethod
    def _existing(paths):

        for path in paths:

            if not path:
                continue

            expanded = os.path.expandvars(
                os.path.expanduser(
                    str(path)
                )
            )

            if os.path.isfile(
                    expanded
            ):

                return os.path.abspath(
                    expanded
                )

        return None

    def _find_brave(self):

        paths = [
            os.path.join(
                os.environ.get(
                    "PROGRAMFILES",
                    ""),
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),

            os.path.join(
                os.environ.get(
                    "PROGRAMFILES(X86)",
                    ""),
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),

            os.path.join(
                os.environ.get(
                    "LOCALAPPDATA",
                    ""),
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),
        ]

        found = self._existing(
            paths
        )

        if found:
            return found

        return shutil.which(
            "brave.exe"
        ) or shutil.which(
            "brave"
        )

    def _find_chrome(self):

        paths = [
            os.path.join(
                os.environ.get(
                    "PROGRAMFILES",
                    ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),

            os.path.join(
                os.environ.get(
                    "PROGRAMFILES(X86)",
                    ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),

            os.path.join(
                os.environ.get(
                    "LOCALAPPDATA",
                    ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
        ]

        found = self._existing(
            paths
        )

        if found:
            return found

        return shutil.which(
            "chrome.exe"
        ) or shutil.which(
            "chrome"
        )

    def _find_edge(self):

        paths = [
            os.path.join(
                os.environ.get(
                    "PROGRAMFILES(X86)",
                    ""),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),

            os.path.join(
                os.environ.get(
                    "PROGRAMFILES",
                    ""),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),

            os.path.join(
                os.environ.get(
                    "LOCALAPPDATA",
                    ""),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),
        ]

        found = self._existing(
            paths
        )

        if found:
            return found

        return shutil.which(
            "msedge.exe"
        ) or shutil.which(
            "msedge"
        )

    def _find_firefox(self):

        paths = [
            os.path.join(
                os.environ.get(
                    "PROGRAMFILES",
                    ""),
                "Mozilla Firefox",
                "firefox.exe",
            ),

            os.path.join(
                os.environ.get(
                    "PROGRAMFILES(X86)",
                    ""),
                "Mozilla Firefox",
                "firefox.exe",
            ),

            os.path.join(
                os.environ.get(
                    "LOCALAPPDATA",
                    ""),
                "Mozilla Firefox",
                "firefox.exe",
            ),
        ]

        found = self._existing(
            paths
        )

        if found:
            return found

        return shutil.which(
            "firefox.exe"
        ) or shutil.which(
            "firefox"
        )

    def find_executable(self):

        if self.explicit_executable:

            path = self._existing(
                [
                    self.explicit_executable
                ]
            )

            if path:
                return path

            raise RuntimeError(
                "Configured browser executable does not exist: "
                + str(
                    self.explicit_executable
                )
            )

        if self.browser_name == "brave":
            path = self._find_brave()

        elif self.browser_name == "chrome":
            path = self._find_chrome()

        elif self.browser_name == "edge":
            path = self._find_edge()

        elif self.browser_name == "firefox":
            path = self._find_firefox()

        else:
            raise RuntimeError(
                "Unsupported browser: "
                + self.browser_name
            )

        if not path:

            raise RuntimeError(
                "Could not find installed "
                + self.browser_name
                + " browser executable. "
                  "Install the browser or set "
                  "browser.executable_path in config."
            )

        return path

    # ================================================================
    # PLAYWRIGHT
    # ================================================================

    def _ensure(self):

        if self._page:
            return

        try:

            from playwright.sync_api import (
                sync_playwright
            )

        except ImportError as exc:

            raise RuntimeError(
                "Playwright is not installed. "
                "Run: pip install playwright"
            ) from exc

        executable = self.find_executable()

        print(
            "[browser] Browser: "
            + self.browser_name
        )

        print(
            "[browser] Executable: "
            + executable
        )

        self._playwright = (
            sync_playwright().start()
        )

        launch_kwargs = {
            "headless": self.headless,
        }

        # Brave, Chrome and Edge are Chromium-family
        # browsers and can be launched through Playwright's
        # chromium launcher with executable_path.
        if self.browser_name in {
            "brave",
            "chrome",
            "edge",
        }:

            self._browser = (
                self._playwright.chromium.launch(
                    executable_path=executable,
                    **launch_kwargs,
                )
            )

        elif self.browser_name == "firefox":

            self._browser = (
                self._playwright.firefox.launch(
                    executable_path=executable,
                    **launch_kwargs,
                )
            )

        else:

            raise RuntimeError(
                "Unsupported browser: "
                + self.browser_name
            )

        self._context = (
            self._browser.new_context(
                accept_downloads=True
            )
        )

        self._page = (
            self._context.new_page()
        )

    # ================================================================
    # NAVIGATION
    # ================================================================

    def navigate(
            self,
            url,
    ):

        if self.safety:

            self.safety.validate_browser_url(
                url
            )

        self._ensure()

        print(
            "[browser] Navigating "
            + self.browser_name
            + " -> "
            + url
        )

        self._page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        return {
            "success": True,
            "requested_url": url,
            "url": self._page.url,
            "title": self._page.title(),
            "browser": self.browser_name,
            "executable": self.find_executable(),
        }

    # ================================================================
    # PAGE
    # ================================================================

    def get_page(self):

        self._ensure()

        return {
            "success": True,
            "url": self._page.url,
            "title": self._page.title(),
            "browser": self.browser_name,
            "text": self._page.locator(
                "body"
            ).inner_text(
                timeout=10000
            )[:20000],
        }

    # ================================================================
    # CLICK
    # ================================================================

    def click(
            self,
            selector,
    ):

        self._ensure()

        self._page.locator(
            selector
        ).first.click(
            timeout=10000
        )

        return {
            "success": True,
            "url": self._page.url,
            "title": self._page.title(),
            "browser": self.browser_name,
        }

    # ================================================================
    # FILL
    # ================================================================

    def fill(
            self,
            selector,
            text,
    ):

        self._ensure()

        self._page.locator(
            selector
        ).first.fill(
            text,
            timeout=10000,
        )

        return {
            "success": True,
            "browser": self.browser_name,
        }

    # ================================================================
    # PRESS
    # ================================================================

    def press(
            self,
            selector,
            key,
    ):

        self._ensure()

        self._page.locator(
            selector
        ).first.press(
            key,
            timeout=10000,
        )

        return {
            "success": True,
            "browser": self.browser_name,
        }

    # ================================================================
    # SELECT
    # ================================================================

    def select(
            self,
            selector,
            value,
    ):

        self._ensure()

        self._page.locator(
            selector
        ).first.select_option(
            value,
            timeout=10000,
        )

        return {
            "success": True,
            "browser": self.browser_name,
        }

    # ================================================================
    # SCREENSHOT
    # ================================================================

    def screenshot(
            self,
            path=None,
    ):

        self._ensure()

        if path is None:

            path = self.config.get(
                "browser",
                {},
            ).get(
                "screenshot_path",
                os.path.join(
                    "logs",
                    "browser_screenshots",
                    "browser.png",
                ),
            )

        if self.safety and not self.safety.output_path_is_allowed(
                path
        ):

            raise RuntimeError(
                "Blocked browser screenshot path: "
                "outside configured output/user paths."
            )

        parent = os.path.dirname(
            os.path.abspath(
                path
            )
        )

        os.makedirs(
            parent,
            exist_ok=True,
        )

        self._page.screenshot(
            path=path,
            full_page=True,
        )

        return {
            "success": True,
            "path": path,
            "url": self._page.url,
            "browser": self.browser_name,
        }

    # ================================================================
    # LINKS
    # ================================================================

    def links(
            self,
            limit=50,
    ):

        self._ensure()

        links = self._page.locator(
            "a"
        ).all()

        out = []

        for a in links[:limit]:

            try:

                out.append(
                    {
                        "text": a.inner_text()[:200],
                        "href": a.get_attribute(
                            "href"
                        ),
                    }
                )

            except Exception:
                pass

        return {
            "success": True,
            "browser": self.browser_name,
            "links": out,
        }

    # ================================================================
    # CLOSE
    # ================================================================

    def close(self):

        errors = []

        if self._context:

            try:
                self._context.close()
            except Exception as exc:
                errors.append(
                    str(exc)
                )

        if self._browser:

            try:
                self._browser.close()
            except Exception as exc:
                errors.append(
                    str(exc)
                )

        if self._playwright:

            try:
                self._playwright.stop()
            except Exception as exc:
                errors.append(
                    str(exc)
                )

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

        if errors:

            return {
                "success": False,
                "error": "; ".join(
                    errors
                ),
            }

        return {
            "success": True
        }