import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple


@dataclass(frozen=True)
class SavedImage:
    sha256: str
    mime_type: str
    filename: str
    path: str
    size_bytes: int
    created_at: str
    expires_at: str


class ImageStorageManager:
    def __init__(self, *, base_dir: str, retention_days: int):
        self.base_dir = base_dir
        self.retention_days = max(retention_days, 1)
        os.makedirs(self.base_dir, exist_ok=True)

    def save_image(self, *, message_id: str, image_bytes: bytes, mime_type: Optional[str] = None) -> SavedImage:
        detected_mime, extension = self._detect_image_type(image_bytes, mime_type)
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(days=self.retention_days)
        day_dir = os.path.join(self.base_dir, created_at.strftime("%Y%m%d"))
        os.makedirs(day_dir, exist_ok=True)
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        filename = f"{message_id}_{sha256[:12]}{extension}"
        path = os.path.join(day_dir, filename)
        with open(path, "wb") as file_obj:
            file_obj.write(image_bytes)
        return SavedImage(
            sha256=sha256,
            mime_type=detected_mime,
            filename=filename,
            path=path,
            size_bytes=len(image_bytes),
            created_at=created_at.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def delete_file(self, path: str) -> bool:
        if not path or not os.path.exists(path):
            return False
        os.remove(path)
        return True

    @staticmethod
    def _detect_image_type(image_bytes: bytes, mime_type: Optional[str]) -> Tuple[str, str]:
        if mime_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            mapping = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }
            return mime_type, mapping[mime_type]
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "image/webp", ".webp"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", ".gif"
        raise ValueError("Unsupported image format")
