from __future__ import annotations

from typing import Any


def _sanitize_log_value(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"
    return text.replace("\n", "\\n").replace(" ", "_")


def format_log_event(event: str, **fields: Any) -> str:
    parts = [f"event={_sanitize_log_value(event)}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_sanitize_log_value(value)}")
    return " ".join(parts)
