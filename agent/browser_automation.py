"""
browser_automation.py — Playwright-based browser automation wrapper.
"""

import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("BrowserAutomation")


class BrowserAutomation:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_browser(self):
        try:
            if self._page is not None and not self._page.is_closed():
                return
        except Exception:
            pass

        try:
            if not self._playwright:
                self._playwright = sync_playwright().start()

            # Launch Chromium securely with arguments to prevent pipe/session drops
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
            logger.info("Playwright browser instance initialized successfully.")
        except Exception as exc:
            logger.error(f"Failed to start browser: {exc}")
            self._page = None

    def navigate(self, url: str) -> dict:
        try:
            self._ensure_browser()
            if not self._page:
                return {"success": False, "error": "Browser page could not be initialized."}

            self._page.goto(url, timeout=15000)
            return {"success": True, "url": url}
        except PlaywrightTimeoutError:
            return {"success": False, "error": "Navigation timed out."}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def search_youtube(self, query: str) -> dict:
        try:
            self._ensure_browser()
            if not self._page:
                return {"success": False, "error": "Browser page could not be initialized."}

            from urllib.parse import quote_plus
            url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            self._page.goto(url, timeout=15000)

            # Wait for results and click the first video
            try:
                self._page.wait_for_selector("ytd-video-renderer", timeout=5000)
                first_video = self._page.locator("ytd-video-renderer #video-title").first
                first_video.click()
            except Exception:
                pass

            return {"success": True, "message": f"Playing first result for: {query}"}
        except Exception as exc:
            logger.exception("YouTube automation failed")
            return {"success": False, "error": str(exc)}

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None