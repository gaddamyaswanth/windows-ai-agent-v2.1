"""
tts.py — Text-to-Speech synthesis using pyttsx3.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, List, Optional

logger = logging.getLogger("TTS")

MAX_CHUNK_CHARS = 300          # SAPI gets flaky past ~4k chars; keep chunks sane


class TextToSpeech:
    def __init__(self, config: Any):
        # Support both dot-notation Config objects and raw dictionaries
        if hasattr(config, "voice"):
            voice_cfg = config.voice
        elif hasattr(config, "get"):
            voice_cfg = config.get("voice", {})
        else:
            voice_cfg = {}

        self.enabled = bool(getattr(voice_cfg, "tts_enabled", True))
        self.rate = max(80, min(int(getattr(voice_cfg, "rate", 175)), 400))
        self.volume = max(0.0, min(float(getattr(voice_cfg, "volume", 1.0)), 1.0))
        self.voice_hint = str(getattr(voice_cfg, "voice_name", "")).lower()

        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._stop_event = threading.Event()       # abort current + clear queue
        self._worker: Optional[threading.Thread] = None
        self._engine = None                        # lazily created in worker thread
        self._last_spoken = ""
        self._last_time = 0.0

        if self.enabled:
            self._start_worker()

    # ------------------------------------------------------------- lifecycle

    def _start_worker(self) -> None:
        self._worker = threading.Thread(
            target=self._speech_loop, daemon=True, name="tts-worker")
        self._worker.start()

    def _init_engine(self):
        """Create the engine once. MUST run in the worker thread — pyttsx3
        drivers bind to the creating thread's COM apartment."""
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.setProperty("volume", self.volume)

        if self.voice_hint:
            try:
                for v in engine.getProperty("voices"):
                    if self.voice_hint in v.name.lower():
                        engine.setProperty("voice", v.id)
                        logger.info("TTS voice: %s", v.name)
                        break
            except Exception:
                logger.debug("Voice selection failed; using default.", exc_info=True)

        return engine

    def _speech_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:                      # shutdown sentinel
                break

            text, interruptible = item

            if self._stop_event.is_set():
                continue                          # drain remaining stale items

            try:
                if self._engine is None:
                    self._engine = self._init_engine()

                for chunk in self._chunk(text):
                    if self._stop_event.is_set():
                        break
                    self._engine.say(chunk)
                    self._engine.runAndWait()
            except Exception:
                logger.exception("Speech synthesis failed.")
                # Engine may be wedged after a driver error — rebuild it.
                try:
                    self._engine.stop()
                except Exception:
                    pass
                self._engine = None
                time.sleep(0.2)                   # let the audio device settle

    @staticmethod
    def _chunk(text: str, size: int = MAX_CHUNK_CHARS) -> List[str]:
        """Split on sentence boundaries so chunks end naturally."""
        import re
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        chunks: List[str] = []
        buf = ""
        for s in sentences:
            if len(buf) + len(s) + 1 <= size:
                buf = f"{buf} {s}".strip()
            else:
                if buf:
                    chunks.append(buf)
                while len(s) > size:
                    cut = s.rfind(" ", 0, size)
                    cut = cut if cut > 0 else size
                    chunks.append(s[:cut])
                    s = s[cut:].strip()
                buf = s
        if buf:
            chunks.append(buf)
        return chunks or [text]

    # ------------------------------------------------------------------ API

    def speak(self, text: str, *, priority: bool = False, interruptible: bool = True) -> None:
        text = str(text).strip()
        if not self.enabled or not text:
            return

        now = time.monotonic()
        if text == self._last_spoken and now - self._last_time < 10.0:
            logger.debug("Suppressed duplicate TTS: %r", text[:60])
            return
        self._last_spoken, self._last_time = text, now

        item = (text, interruptible)
        if priority:
            pending: list = []
            while not self._queue.empty():
                try:
                    pending.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._queue.put(item)
            for p in pending:
                self._queue.put(p)
            self._interrupt_current()
        else:
            self._queue.put(item)

    def say_error(self, text: str) -> None:
        self.speak(f"Error. {text}", priority=True)

    def stop(self) -> None:
        self._stop_event.set()
        self._clear_queue()
        self._interrupt_current()

    def resume(self) -> None:
        self._stop_event.clear()

    def wait_until_done(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self._queue.empty():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    # -------------------------------------------------------------- internals

    def _clear_queue(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _interrupt_current(self) -> None:
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                logger.debug("Engine stop failed during interrupt.", exc_info=True)

    def shutdown(self) -> None:
        self._clear_queue()
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=3.0)
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass