import json
import re
import unicodedata
from typing import Any, Dict, List, Optional


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


def normalize_service_name(value: str) -> str:
    text = value.strip().lower()
    mapping = {
        "google calendar": "google",
        "google tasks": "google",
        "google": "google",
        "agoda": "agoda",
        "booking": "booking",
        "booking.com": "booking",
        "momo": "momo",
        "pchome": "pchome",
        "蝦皮": "shopee",
        "shopee": "shopee",
    }
    return mapping.get(text, text.replace(" ", "_"))


def parse_memory_command(text: str) -> Optional[Dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return None

    email_match = re.search(r"(?:記住|更新)我(?:的)?(?:常用)?\s*email\s*(?:是|為)?\s*([^\s，,]+@[^\s，,]+)", stripped, re.I)
    if email_match:
        email = email_match.group(1).strip()
        return {
            "type": "profile_save",
            "profile_updates": {"contact_email": email},
            "reply_text": f"已記住你的常用 email：{email}",
            "change_type": "save_profile",
        }

    departure_match = re.search(r"(?:記住|更新)我(?:都|通常)?(?:是)?從(.+?)(?:出發|飛)", stripped)
    if departure_match:
        departure = departure_match.group(1).strip()
        return {
            "type": "profile_save",
            "profile_updates": {"preferred_departure_airport": departure},
            "reply_text": f"已記住你常用從 {departure} 出發。",
            "change_type": "save_profile",
        }

    traveler_match = re.search(r"我通常(\d+)個人出遊", stripped)
    if traveler_match:
        count = int(traveler_match.group(1))
        return {
            "type": "profile_save",
            "profile_updates": {"default_traveler_count": count},
            "reply_text": f"已記住你通常是 {count} 人出遊。",
            "change_type": "save_profile",
        }

    address_match = re.search(r"(?:記住|更新)我(?:的)?(?:常用)?地址(?:是|為)?\s*(.+)$", stripped)
    if address_match:
        address = address_match.group(1).strip()
        return {
            "type": "profile_save",
            "profile_updates": {"default_address": address},
            "reply_text": "已記住你的常用地址。",
            "change_type": "save_profile",
        }

    account_match = re.search(
        r"(?:記住|更新)我(?:的)?\s*(.+?)(?:帳號|會員)(?:是|為|用這個帳號|用)\s*([^\s，,]+)",
        stripped,
        re.I,
    )
    if account_match:
        service_name = normalize_service_name(account_match.group(1))
        login_identifier = account_match.group(2).strip()
        return {
            "type": "account_save",
            "account_updates": [
                {
                    "service_name": service_name,
                    "login_identifier": login_identifier,
                    "display_name": login_identifier,
                }
            ],
            "reply_text": f"已記住你的 {service_name} 帳號識別資訊。",
            "change_type": "save_account",
        }

    forget_account_match = re.search(r"忘記我(?:的)?\s*(.+?)(?:帳號|會員)", stripped, re.I)
    if forget_account_match:
        service_name = normalize_service_name(forget_account_match.group(1))
        return {
            "type": "account_forget",
            "service_name": service_name,
            "reply_text": f"已刪除你在 {service_name} 的帳號記錄。",
            "change_type": "forget_account",
        }

    forget_email_match = re.search(r"忘記我(?:的)?(?:常用)?\s*email", stripped, re.I)
    if forget_email_match:
        return {
            "type": "profile_forget",
            "fields": ["contact_email"],
            "reply_text": "已刪除你常用 email 的記錄。",
            "change_type": "forget_profile",
        }

    return None
