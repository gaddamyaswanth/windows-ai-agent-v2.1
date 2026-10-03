"""
voice_normalizer.py — Voice Command Normalization and Intent Classification.

Improvements over the original:
- Fixed double-correction bug: normalize() corrected text, then classify()
  corrected it AGAIN. Corrections are now applied exactly once, in classify().
- Verb-flexible matching: original required EXACT anchors like
  "^close all tabs$" so "please close all my tabs" or "could you close
  every tab for me" fell through to generic handling
- Fuzzy navigation targets: "open git hub", "open g mail", "open face book"
  now resolve via difflib instead of requiring dictionary-exact matches
- Wake-word stripping: "hey jarvis open youtube" no longer fails the ^anchor
- Data-driven pattern tables: verbs/intents declared as lists and compiled
  once — adding an intent is a data edit, not new regex plumbing
- Number-word normalization: "open five tabs" → digits (useful downstream)
- Result includes confidence + which rule fired, for auditability/debugging
- Punctuation-tolerant: STT often emits trailing periods/commas that broke
  strict $ anchors ("close this tab." matched NOTHING)
"""

from __future__ import annotations

import logging
import re
from difflib import get_close_matches
from typing import Any, Dict, List, Optional

logger = logging.getLogger("VoiceCommandNormalizer")

# ------------------------------------------------------------------ lexicons

WORD_CORRECTIONS: Dict[str, str] = {
    # STT mishearings of app/tab vocabulary
    "caps lock": "tabs",
    "caps": "tabs",
    "cap": "tab",
    "taps": "tabs",
    "tab s": "tabs",
    "you tube": "youtube",
    "youtub": "youtube",
    "you-tube": "youtube",
    "spottyfy": "spotify",
    "spot ify": "spotify",
    "discorded": "discord",
    "fire fox": "firefox",
    "google chrome browser": "chrome",
    "chrome browser": "chrome",
}

NAV_TARGETS = {
    "youtube", "google", "gmail", "github", "facebook",
    "instagram", "spotify", "twitter", "reddit", "netflix",
    "amazon", "maps", "drive", "calendar", "chatgpt",
}

# Common STT filler phrases to strip BEFORE matching.
FILLERS = [
    r"^hey\s+(jarvis|assistant|computer|alexa)\b[,\s]*",
    r"^(?:hey |ok |okay )?(?:jarvis|assistant)\b[,\s]+",
    r"\b(?:please|kindly|can you|could you|would you|i want you to|"
    r"go ahead and|just)\b",
]

VERB_OPEN = r"(?:open|go\s+to|goto|launch|start|bring\s+up|navigate\s+to)"
VERB_CLOSE = r"(?:close|kill|shut|terminate)"

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


class VoiceCommandNormalizer:
    """Cleans speech-to-text transcripts and classifies intent into workflows."""

    def __init__(self):
        self._correction_patterns = [
            (re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE), right)
            for wrong, right in sorted(WORD_CORRECTIONS.items(),
                                       key=lambda kv: len(kv[0]), reverse=True)
        ]
        self._filler_patterns = [re.compile(p, re.IGNORECASE) for p in FILLERS]
        self._number_pattern = re.compile(
            r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.IGNORECASE)

        # Compiled intent matchers: (intent_name, compiled_regex, extractor)
        self._intents = self._build_intents()

        self._nav_targets_lower = {t.lower(): t for t in NAV_TARGETS}

    # ------------------------------------------------------------- cleaning

    @staticmethod
    def _clean(text: Any) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"[.,!?;]+$", "", str(text))     # trailing punctuation
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def correct_speech(self, text: str) -> str:
        """Correct obvious STT errors conservatively."""
        cleaned = self._clean(text)
        if not cleaned:
            return ""
        for pattern, correct in self._correction_patterns:
            cleaned = pattern.sub(correct, cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _strip_fillers(self, text: str) -> str:
        for pattern in self._filler_patterns:
            text = pattern.sub("", text)
        return re.sub(r"\s+", " ", text).strip()

    def _numbers_to_digits(self, text: str) -> str:
        return self._number_pattern.sub(
            lambda m: str(NUMBER_WORDS[m.group(1).lower()]), text)

    def _resolve_target(self, raw: str) -> Optional[str]:
        """Exact match first, then fuzzy fallback for STT-mangled names."""
        target = raw.lower().replace(".", "").replace("-", "").strip()
        target = re.sub(r"\s+(dot|period)\s+", ".", target)   # "git hub dot com"
        if target.startswith(("http://", "https://")):
            return raw.strip()

        if target.endswith(".com"):
            candidate = target[:-4].replace(".", "")
        else:
            candidate = target.replace(" ", "")

        if candidate in self._nav_targets_lower:
            return self._nav_targets_lower[candidate]
        if target in self._nav_targets_lower:
            return self._nav_targets_lower[target]

        # Fuzzy: "git hub", "g mail", "face book" → github, gmail, facebook
        collapsed = target.replace(" ", "")
        matches = get_close_matches(collapsed, list(self._nav_targets_lower),
                                    n=1, cutoff=0.75)
        if matches:
            resolved = self._nav_targets_lower[matches[0]]
            logger.debug("Fuzzy-resolved nav target %r -> %r", raw, resolved)
            return resolved
        return None

    # -------------------------------------------------------------- intents

    def _build_intents(self) -> List[tuple]:
        """
        Ordered matcher table. First match wins, so MORE SPECIFIC patterns
        must come before less specific ones.
        """
        Q = r"(?P<query>.+?)"

        specs = [
            # --- tabs: count-aware, verb-flexible ---
            (rf"^{VERB_CLOSE}\s+(?:all\s+)?(?:the\s+|my\s+|every\s+)?(?:browser\s+)?tabs?"
             rf"(?:\s+(?:on|in)\s+\w+)?$",
             "browser_close_all_tabs"),
            (rf"^{VERB_CLOSE}\s+(?P<count>\d+)\s+tabs?\b",
             "browser_close_n_tabs"),
            (rf"^{VERB_CLOSE}\s+(?:this|the|current|that)\s+(?:browser\s+)?tab\b.*$",
             "browser_close_tab"),
            (r"^(?:new|open\s+a?)\s+tab\b",
             "browser_new_tab"),

            # --- youtube: query extraction with genre-suffix trimming ---
            (rf"^(?:{VERB_OPEN})?\s*youtube(?:\s+and\s+play|\s*,?\s*play)?\s+{Q}$",
             "youtube"),
            (rf"^play\s+{Q}\s+on\s+youtube$",
             "youtube"),
            (rf"^{VERB_OPEN}\s+youtube$",
             "youtube_open"),

            # --- music apps ---
            (rf"^play\s+{Q}\s+on\s+(spotify|discord)$",
             "app_media_play"),

            # --- search ---
            (rf"^(?:search|look\s+up|find)(?:\s+for)?\s+{Q}$",
             "web_search"),

            # --- generic navigation (LAST among web intents) ---
            (rf"^(?:{VERB_OPEN})\s+(?P<target>.+)$",
             "browser_navigation"),
        ]

        intents = []
        for pattern_str, name in specs:
            try:
                intents.append((name, re.compile(pattern_str, re.IGNORECASE)))
            except re.error:
                logger.exception("Bad intent regex for %s — skipped.", name)
        return intents

    # -------------------------------------------------------------- classify

    def classify(self, raw_text: str) -> Dict[str, Any]:
        """
        Clean ONCE (fixes the original's double-correction), strip fillers,
        then run ordered intent matching.
        """
        text = self._numbers_to_digits(self._strip_fillers(
            self.correct_speech(raw_text)))
        lowered = text.lower().strip()

        result_base: Dict[str, Any] = {"text": text}

        if not lowered:
            return {"type": "empty", "text": ""}

        # YouTube query needs special trimming (trailing "song"/"music").
        for name, pattern in self._intents:
            if match := pattern.match(lowered):
                payload: Dict[str, Any] = {}
                groups = match.groupdict()

                if name == "youtube":
                    query = (groups.get("query") or "").strip()
                    query = re.sub(r"\s+(?:songs?|music|video)s?$", "",
                                   query, flags=re.IGNORECASE).strip()
                    payload["query"] = query or None

                elif name == "browser_navigation":
                    target = self._resolve_target(groups.get("target", ""))
                    if target is None:
                        continue          # unknown site — fall through
                    payload["target"] = target

                elif name == "browser_close_n_tabs":
                    payload["count"] = int(groups["count"])

                elif name == "app_media_play":
                    payload["query"] = groups.get("query", "").strip()
                    payload["app"] = groups.get("app", "").lower()

                logger.debug("Intent %s matched (%s)", name, text[:60])
                return {"type": name, **result_base, **payload,
                        "confidence": "rule"}

        logger.debug("Classified as normal command: %r", text[:60])
        return {"type": "normal", **result_base}

    def normalize(self, text: str) -> Dict[str, Any]:
        """Main entry point. Cleaning happens exactly once inside classify()."""
        return self.classify(text)


# --------------------------------------------------------------------- demo

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    n = VoiceCommandNormalizer()

    samples = [
        "close all tabs",
        "please close all my tabs.",              # filler + punctuation — was missed
        "hey jarvis, could you close every tab",  # wake word + filler — was missed
        "open five tabs",                          # number word → digit
        "play never gonna give you up on youtube",
        "youtube and play some lofi songs",
        "open git hub",                            # fuzzy → github
        "go to g mail",                            # fuzzy → gmail
        "open https://example.com/page",
        "what time is it",                         # falls through to normal
    ]
    for s in samples:
        print(f"{s!r:50} -> {n.normalize(s)}")
