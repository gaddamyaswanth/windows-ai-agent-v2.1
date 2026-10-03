"""
action_logger.py — structured JSONL action log.

Every tool call the dispatcher executes gets one line here: the goal it
was in service of, which iteration of the loop, the tool and arguments,
the resolved permission class, and a summary of the result (and
verification, when present). Console output scrolls away; this file is
the durable debugging/audit trail recommended in review — plain text logs
don't let you grep "every DANGEROUS action from the last session" or
diff two runs of the same goal.

One JSON object per line (JSONL), so it can be tailed live or loaded
wholesale without parsing a single giant array.
"""

import json
import os
from datetime import datetime


class ActionLogger:
    def __init__(self, log_dir: str = "logs", filename: str = "actions.jsonl"):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, filename)

    def log(
        self,
        *,
        goal: str,
        iteration: int,
        tool_name: str,
        tool_input: dict,
        permission_class: str,
        result: dict,
    ):
        # Don't log the verification screenshot's base64 payload — it's
        # large and not useful in a text log. Keep everything else.
        result_summary = dict(result) if isinstance(result, dict) else {"raw": str(result)}
        result_summary.pop("post_action_screenshot", None)
        result_summary.pop("image", None)

        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "goal": goal,
            "iteration": iteration,
            "tool": tool_name,
            "input": tool_input,
            "permission_class": permission_class,
            "result": result_summary,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            # Logging must never take down the agent loop itself.
            print(f"[action_logger] failed to write log entry: {e}")
