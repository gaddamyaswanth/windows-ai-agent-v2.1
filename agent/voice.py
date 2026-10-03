"""
agent/voice.py — Microphone capture with non-blocking stream checks and robust fallbacks.
"""

import logging
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

from agent.tts import TextToSpeech

logger = logging.getLogger("VoiceController")


def _extract_voice_section(config):
    try:
        return config.voice
    except AttributeError:
        return config


class VoiceController:

    def __init__(self, config):
        self.cfg = _extract_voice_section(config)
        self._stt = LocalWhisperSTT(self.cfg)
        self._tts = TextToSpeech(config)
        self._muted_until = 0.0

        logger.info(
            "VoiceController ready (sr=%s)",
            getattr(self.cfg, "sample_rate", 16000))

    def listen(self, timeout: float = None):
        timeout = timeout if timeout is not None else self.cfg.duration_seconds
        sample_rate = self.cfg.sample_rate
        min_rms = getattr(self.cfg, "min_audio_level", 0.0005)
        max_rms = getattr(self.cfg, "max_audio_level", 0.95)
        block = getattr(self.cfg, "block_size", 1600)
        pre_buf_frames = int(self.cfg.pre_speech_buffer * sample_rate)

        try:
            # Open stream using default system input to avoid hardware index locks
            stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block,
            )
        except Exception as exc:
            logger.warning("Microphone stream unavailable (%s). Retrying in 2s...", exc)
            time.sleep(2.0)
            return None

        frames_needed_pre = max(pre_buf_frames // block, 1)
        pre_buffer = deque(maxlen=frames_needed_pre)
        recording = []

        logger.info("🎤 Listening... (Speak now)")
        try:
            with stream:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if time.monotonic() < self._muted_until:
                        time.sleep(0.1)
                        continue

                    # Read a small block with safety check
                    block_data, overflowed = stream.read(block)
                    chunk = block_data[:, 0].copy()
                    rms = float(np.sqrt(np.mean(chunk ** 2)))

                    # Live audio level output to confirm it's processing
                    print(f"\r[RMS: {rms:.5f}]   ", end="", flush=True)

                    pre_buffer.append(chunk)
                    if rms >= min_rms:
                        print(f"\n🗣️ Speech detected! (rms={rms:.5f}) Recording...")
                        recording.extend(pre_buffer)
                        break
                else:
                    print()
                    return None

                # Capture speech until silence
                silence_limit = self.cfg.post_speech_silence
                max_frames = int(self.cfg.max_record_seconds * sample_rate)
                silent_chunks = 0
                chunks_per_sec = sample_rate // block
                total_frames = len(recording) * block

                while total_frames < max_frames:
                    block_data, overflowed = stream.read(block)
                    chunk = block_data[:, 0].copy()
                    peak_rms = float(np.sqrt(np.mean(chunk ** 2)))

                    if peak_rms > max_rms:
                        chunk = np.clip(chunk, -max_rms, max_rms)

                    recording.append(chunk)
                    total_frames += block

                    if peak_rms < min_rms:
                        silent_chunks += 1
                        if silent_chunks >= int(silence_limit * chunks_per_sec):
                            logger.info("Speech ended.")
                            break
                    else:
                        silent_chunks = 0
        except Exception as exc:
            logger.debug("Recording loop error: %s", exc)
            time.sleep(0.2)
            return None

        return np.concatenate(recording).astype(np.float32)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) == 0:
            return ""
        try:
            text = self._stt.transcribe(audio)
        except Exception:
            logger.exception("STT transcription failed.")
            return ""
        text = (text or "").strip()
        logger.info("Transcribed: %r", text)
        return text

    def speak(self, text: str):
        if not text:
            return
        est_duration = max(len(text) / 12.0, 1.5)
        cooldown = getattr(self.cfg, "mute_cooldown_seconds", 5.0)
        self._muted_until = time.monotonic() + est_duration + cooldown
        self._tts.speak(text)

    def shutdown(self):
        if self._tts:
            self._tts.shutdown()


class LocalWhisperSTT:

    def __init__(self, config):
        self.cfg = _extract_voice_section(config)
        self._model = None
        self._model_name = self.cfg.whisper_model
        device = self.cfg.stt_device
        if device == "auto":
            self._device = ("cuda" if self._cuda_available() else "cpu")
        else:
            self._device = device
        logger.info("Whisper '%s' configured (device=%s).", self._model_name, self._device)

    @staticmethod
    def _cuda_available():
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:
            return False

    def _ensure_loaded(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            logger.info("Loading Whisper model '%s'...", self._model_name)
            self._model = WhisperModel(self._model_name, device=self._device, compute_type="int8")
            logger.info("Whisper model loaded.")

    def transcribe(self, audio: np.ndarray) -> str:
        self._ensure_loaded()
        segments, info = self._model.transcribe(
            audio,
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
        )
        parts = [s.text for s in segments if s.avg_logprob >= self.cfg.min_transcription_confidence]
        return "".join(parts).strip()