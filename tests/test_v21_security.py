"""V2.1 security regression tests."""

from pathlib import Path
import tempfile

import yaml

from agent.safety import SafetyGate, SafetyViolation
from agent.tools import build_tool_schemas


def load_config():
    with open("config/config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_browser_url_policy():
    cfg = load_config()
    gate = SafetyGate(cfg)

    blocked_urls = [
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://localhost:8080/",
        "http://127.0.0.1:8000/",
        "http://192.168.1.1/",
        "https://user:password@example.com/",
    ]

    for bad_url in blocked_urls:
        try:
            gate.validate_browser_url(bad_url)
        except SafetyViolation:
            pass
        else:
            raise AssertionError(
                f"URL should be blocked: {bad_url}"
            )

    gate.validate_browser_url("https://example.com")


def test_browser_screenshot_path_policy():
    cfg = load_config()
    gate = SafetyGate(cfg)

    assert gate.output_path_is_allowed(
        "logs/browser_screenshots/test.png"
    )

    assert not gate.output_path_is_allowed(
        "C:/Windows/test.png"
    )


def test_memory_source_policy():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "memory.db")

        from memory.store import MemoryStore

        store = MemoryStore(db)

        try:
            result = store.save(
                "fact",
                "from a webpage",
                source="webpage",
            )

            assert result["success"] is False

            result = store.save(
                "preference",
                "Use Chrome",
                source="explicit_user_request",
            )

            assert result["success"] is True

            result = store.save(
                "fact",
                "api_key=SECRET123",
                source="agent_experience",
            )

            assert result["success"] is False

        finally:
            store.close()


def test_memory_save_not_model_exposed():
    tool_schemas = build_tool_schemas(load_config())

    tool_names = {
        tool.get("name")
        for tool in tool_schemas
        if tool.get("name")
    }

    assert "memory_save" not in tool_names

    assert "memory_search" in tool_names
    assert "memory_list" in tool_names