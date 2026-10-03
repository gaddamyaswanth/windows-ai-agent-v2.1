"""
main.py — Entry point for the Windows AI Agent (Continuous Voice with Listening Notifications).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from agent.agent import Agent
from agent.voice import VoiceController
from agent.config import load_config
from agent import kill_switch

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("WindowsAgent")


def run_voice_mode(agent: Agent, config):
    voice = VoiceController(config)

    if hasattr(kill_switch, "register"):
        kill_switch.register(voice.shutdown, lambda: logger.info("User interrupt received."))

    logger.info("Voice pipeline initialized. Listening for commands...")

    try:
        while True:
            logger.info("🎤 Ready and listening for your command...")

            audio = voice.listen()
            if audio is None:
                continue

            text = voice.transcribe(audio)
            if not text:
                continue

            logger.info("\n" + "=" * 60)
            logger.info("VOICE COMMAND:\n%s.", text)
            logger.info("=" * 60)

            result = agent.run(text)
            msg = result.get("message")
            if msg:
                logger.info("Result: %s", msg)
                voice.speak(msg)

    except KeyboardInterrupt:
        logger.info("Stopping voice mode...")
    finally:
        voice.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Windows AI Agent")
    parser.add_argument("--voice", action="store_true", default=True, help="Enable voice control mode")
    parser.add_argument("--text", type=str, help="Run a single command via text")
    args = parser.parse_args()

    config = load_config()
    agent = Agent(config)

    if args.text:
        logger.info("Running text command: %s", args.text)
        res = agent.run(args.text)
        print(res.get("message", res))
        return 0

    logger.info("Starting Windows AI Agent...")
    run_voice_mode(agent, config)
    logger.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())