import json
import re
from typing import Any, Dict, List


RESET_COMMANDS = {"忘記", "clear", "reset", "重置"}
APPROVAL_KEYWORDS = {"確認", "同意", "可以", "ok", "yes", "approved", "approve", "好"}
REJECTION_KEYWORDS = {"不要", "取消", "no", "reject", "否", "不行"}


def split_text(text: str, chunk_size: int = 4300) -> List[str]:
    if len(text) <= chunk_size:
        return [text]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found")
    return json.loads(candidate[start : end + 1])


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def looks_like_short_followup(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) <= 120 and "\n" not in stripped
