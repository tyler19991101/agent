import json
import re
import unicodedata
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


def infer_requested_outputs(text: str) -> List[str]:
    lowered = text.lower()
    outputs: List[str] = []
    mapping = (
        ("docx", ("word", "docx", "文件檔", "word檔")),
        ("pdf", ("pdf",)),
        ("txt", ("txt", "文字檔", "純文字")),
    )
    for fmt, keywords in mapping:
        if any(keyword in lowered for keyword in keywords):
            outputs.append(fmt)
    return outputs


def prefers_file_only_response(text: str) -> bool:
    lowered = text.lower()
    file_request_keywords = (
        "輸出成",
        "匯出成",
        "轉成",
        "做成",
        "產出",
        "生成",
        "給我word",
        "給我pdf",
        "給我txt",
        "只要檔案",
        "只要文件",
        "下載連結",
        "word",
        "docx",
        "pdf",
        "txt",
    )
    preview_keywords = (
        "先看",
        "先給我內容",
        "先顯示",
        "順便貼",
        "摘要也要",
        "內容也要",
        "同時顯示",
    )
    requested_file = any(keyword in lowered for keyword in file_request_keywords)
    wants_preview = any(keyword in lowered for keyword in preview_keywords)
    return requested_file and not wants_preview


def sanitize_filename(value: str, default: str = "artifact") -> str:
    normalized = unicodedata.normalize("NFKD", value).strip()
    ascii_safe = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "_" for ch in normalized)
    ascii_safe = re.sub(r"\s+", "_", ascii_safe).strip("._")
    return ascii_safe[:80] or default
