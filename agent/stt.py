"""
stt.py — High-accuracy local speech-to-text transcription using Faster-Whisper.

Improvements over the original:
- FIXED config section mismatch: read config["stt"] while main.py/YAML
  define config["voice"] — all settings silently ignored. Reads "voice"
  with "stt" fallback.
- Lazy model loading: original loaded the model synchronously in
  __init__, blocking app startup for several seconds before the UI/voice
  loop appeared responsive. Model now loads on first transcribe call,
  with a lock so concurrent calls don't double-load.
- Confidence filtering + hallucination rejection: Whisper emits confident-
  looking garbage on silence/noise — classic artifacts are standalone
  "Thank you.", "Thanks for watching!", "Mm-hmm", or bare punctuation.
  Original passed ALL of it downstream, where hallucinations could reach
  the agent as commands. Now: segments below avg logprob threshold are
  dropped; known hallucination phrases rejected outright.
- Device auto-selection: hardcodes were device="cpu", compute_type="int8"
  even when a CUDA GPU is available. Now auto-detects GPU (much faster),
  with explicit override via config.
- Audio sanity: rejects clips that are too short to be speech (saves a
  full inference pass) and normalizes amplitude (mic levels vary wildly).
- initial_prompt kept SHORT: long priming prompts bias decoding toward
  prompt-like output (Whisper can parrot the prompt itself when input is
  ambiguous). Trimmed to vocabulary hints only.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger("LocalWhisperSTT")

# Phrases faster-whisper famously hallucinates on silence / background
# hum. Case-insensitive exact matches after stripping punctuation.
_HALLUCINATIONS = {
    "thank you", "thanks", "thank you for watching", "thanks for watching",
    "mm-hmm", "mm hmm", "uh-huh", "uh huh", "okay", "ok",
    "you", ".", "", "-", "bye", "bye bye",
}

_MIN_AUDIO_SECONDS = 0.4      # shorter than this can't be a command
_MIN_AVG_LOGPROB = -0.75      # segment confidence floor (-1.0 worst)


class LocalWhisperSTT:
    def __init__(self, config: dict):
        # Accept either section name; "voice" is what config.yaml defines.
        cfg = config.get("voice") or config.get("stt") or {}
        self.model_size = cfg.get("whisper_model", "small")
        self.language = cfg.get("language", "en")
        self.device = cfg.get("device") or self._auto_device()
        self.compute_type = cfg.get("compute_type") or (
            "float16" if self.device == "cuda" else "int8")
        self.min_confidence = float(cfg.get("min_transcription_confidence",
                                            _MIN_AVG_LOGPROB))
        self.beam_size = int(cfg.get("beam_size", 5))

        self._model = None
        self._model_lock = threading.Lock()

        logger.info("Whisper %r configured (device=%s, will lazy-load on "
                    "first use).", self.model_size, self.device)

    # ------------------------------------------------------------- setup

    @staticmethod
    def _auto_device() -> str:
        """Prefer CUDA if available; fall back to CPU silently."""
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def _ensure_model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:          # double-checked locking
                    t0 = time.monotonic()
                    logger.info("Loading Faster-Whisper '%s' on %s (%s)...",
                                self.model_size, self.device, self.compute_type)
                    from faster_whisper import WhisperModel
                    try:
                        self._model = WhisperModel(
                            self.model_size,
                            device=self.device,
                            compute_type=self.compute_type)
                    except Exception:
                        if self.device != "cpu":
                            logger.warning("GPU load failed — falling back to CPU.",
                                           exc_info=True)
                            self.device = "cpu"
                            self.compute_type = "int8"
                            self._model = WhisperModel(
                                self.model_size, device="cpu", compute_type="int8")
                        else:
                            raise
                    logger.info("Whisper ready in %.1fs.",
                                time.monotonic() - t0)
        return self._model

    # -------------------------------------------------------- preprocessing

    @staticmethod
    def _normalize_audio(audio_data: np.ndarray) -> np.ndarray:
        """
        Convert to float32 and peak-normalize. Mic gain varies hugely
        between devices; Whisper's VAD behaves more consistently on
        normalized input. Guards against division by zero on digital
        silence.
        """
        audio = np.asarray(audio_data, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = audio / peak * 0.95
        return audio

    # ------------------------------------------------------------ filtering

    @staticmethod
    def _is_hallucination(text: str) -> bool:
        cleaned = re.sub(r"[^\w\s']", "", text.lower()).strip()
        return cleaned in _HALLUCINATIONS

    # ----------------------------------------------------------- transcribe

    def transcribe(self, audio_data: Optional[np.ndarray]) -> str:
        """
        Transcribe one utterance. Returns "" for anything that isn't
        confident speech — callers treat "" exactly like 'nothing heard'.
        """
        if audio_data is None or len(audio_data) == 0:
            return ""

        sample_rate = 16000   # matches voice capture pipeline
        duration = len(audio_data) / sample_rate
        if duration < _MIN_AUDIO_SECONDS:
            logger.debug("Clip too short (%.2fs) — skipping inference.", duration)
            return ""

        try:
            audio = self._normalize_audio(audio_data)
            model = self._ensure_model()

            # Short vocab hint only — long prompts cause Whisper to PARROT
            # the prompt when input is ambiguous, producing phantom commands.
            initial_prompt = (
                "open close youtube volume brightness screenshot browser tab."
            )

            segments, _info = model.transcribe(
                audio,
                beam_size=self.beam_size,
                language=self.language,
                initial_prompt=initial_prompt,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )

            kept_parts = []
            dropped = 0
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                # seg.avg_logprob ~ -1.0..0 ; below threshold = guessing
                if getattr(seg, "avg_logprob", 0.0) < self.min_confidence:
                    dropped += 1
                    logger.debug("Dropped low-confidence segment (%.2f): %r",
                                 seg.avg_logprob, text[:50])
                    continue
                if self._is_hallucination(text):
                    dropped += 1
                    continue
                kept_parts.append(text)

            final_text = " ".join(kept_parts).strip()

            if dropped and not final_text:
                logger.debug("All %d segments filtered out (noise/hallucination).",
                             dropped)
                return ""

            if final_text:
                logger.info("Transcription (%.2fs clip): %r", duration, final_text)
            return final_text

        except Exception:
            logger.exception("Transcription failed.")
            return ""      # callers already handle empty string gracefully


# -------------------------------------------------------------- quick test

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    stt = LocalWhisperSTT({"voice": {"whisper_model": "tiny"}})

    # Synthetic test: 2s of quiet noise should yield "" (filtered).
    noise = (np.random.randn(32000 * 2) * 0.001).astype(np.float32)
    print("Noise result:", repr(stt.transcribe(noise)))
