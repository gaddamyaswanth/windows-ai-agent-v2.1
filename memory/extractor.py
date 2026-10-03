"""Conservative memory extraction helpers.

Only explicit user-approved facts/preferences are automatically persisted in
V2. Task outcomes are stored separately by the agent. This avoids treating
arbitrary text from websites/files as trusted instructions.
"""
import re

PREFERENCE_PATTERNS = [
    r"\bremember that (.+)",
    r"\bmy preference is (.+)",
    r"\bi prefer (.+)",
    r"\balways (.+)",
    r"\bnever (.+)",
]

def explicit_memory_from_goal(goal):
    for pattern in PREFERENCE_PATTERNS:
        m=re.search(pattern, goal, re.I)
        if m:
            return m.group(1).strip()
    return None
