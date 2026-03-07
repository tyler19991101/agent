import json
import subprocess
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from secretary_agent.config import Settings
from secretary_agent.models import SpeakerUtterance


class AudioTranscriptionError(RuntimeError):
    """Raised when speech-to-text or speaker diarization fails."""


class AssemblyAIAudioTranscriber:
    def __init__(
        self,
        *,
        api_key: str,
        language_code: str = "zh",
        speech_models: Optional[List[str]] = None,
        poll_seconds: float = 2.5,
        timeout_seconds: float = 120.0,
        upload_timeout_seconds: float = 600.0,
        speakers_expected: int = 0,
    ):
        self.api_key = api_key
        self.language_code = language_code
        self.speech_models = speech_models or ["universal-3-pro", "universal-2"]
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.upload_timeout_seconds = upload_timeout_seconds
        self.speakers_expected = speakers_expected
        self.base_url = "https://api.assemblyai.com/v2"

    @classmethod
    def from_settings(cls, settings: Settings) -> "AssemblyAIAudioTranscriber":
        if not settings.assemblyai_api_key:
            raise AudioTranscriptionError(
                "缺少 ASSEMBLYAI_API_KEY，無法啟用語音逐字稿與語者分離。"
            )
        return cls(
            api_key=settings.assemblyai_api_key,
            language_code=settings.stt_language_code,
            speech_models=list(settings.stt_speech_models),
            poll_seconds=settings.stt_poll_seconds,
            timeout_seconds=settings.stt_timeout_seconds,
            upload_timeout_seconds=settings.stt_upload_timeout_seconds,
            speakers_expected=settings.diarization_speakers_expected,
        )

    def transcribe_to_prompt(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        _ = filename, mime_type
        upload_url = self._upload_audio(audio_bytes)
        transcript_id = self._create_transcript(upload_url)
        result = self._wait_for_completion(transcript_id)
        utterances = self._parse_utterances(result)
        return format_diarized_transcript(utterances, fallback_text=str(result.get("text", "")).strip())

    def _upload_audio(self, audio_bytes: bytes) -> str:
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-X",
                    "POST",
                    f"{self.base_url}/upload",
                    "-H",
                    f"Authorization: {self.api_key}",
                    "-H",
                    "Content-Type: application/octet-stream",
                    "-H",
                    "Accept: application/json",
                    "-H",
                    "User-Agent: curl/8.7.1",
                    "--data-binary",
                    "@-",
                ],
                input=audio_bytes,
                capture_output=True,
                timeout=self.upload_timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired as err:
            raise AudioTranscriptionError("AssemblyAI upload timed out") from err
        except subprocess.CalledProcessError as err:
            detail = err.stderr.decode("utf-8", errors="ignore") or err.stdout.decode(
                "utf-8", errors="ignore"
            )
            raise AudioTranscriptionError(f"AssemblyAI upload failed: {detail}") from err

        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as err:
            raise AudioTranscriptionError("AssemblyAI upload returned invalid JSON") from err

        upload_url = str(payload.get("upload_url", "")).strip()
        if not upload_url:
            raise AudioTranscriptionError("AssemblyAI upload failed: missing upload_url")
        return upload_url

    def _create_transcript(self, audio_url: str) -> str:
        body: Dict[str, Any] = {
            "audio_url": audio_url,
            "speech_models": self.speech_models,
            "speaker_labels": True,
        }
        if self.language_code:
            body["language_code"] = self.language_code
        if self.speakers_expected > 0:
            body["speakers_expected"] = self.speakers_expected
        req = Request(
            f"{self.base_url}/transcript",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "curl/8.7.1",
            },
            method="POST",
        )
        payload = self._json_request(req)
        transcript_id = str(payload.get("id", "")).strip()
        if not transcript_id:
            raise AudioTranscriptionError("AssemblyAI transcript creation failed: missing id")
        return transcript_id

    def _wait_for_completion(self, transcript_id: str) -> Dict[str, Any]:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            req = Request(
                f"{self.base_url}/transcript/{transcript_id}",
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "curl/8.7.1",
                },
                method="GET",
            )
            payload = self._json_request(req)
            status = str(payload.get("status", "")).lower()
            if status == "completed":
                return payload
            if status == "error":
                raise AudioTranscriptionError(
                    f"AssemblyAI transcription failed: {payload.get('error', 'unknown error')}"
                )
            time.sleep(self.poll_seconds)
        raise AudioTranscriptionError("AssemblyAI transcription timed out")

    def _json_request(self, req: Request) -> Dict[str, Any]:
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="ignore")
            raise AudioTranscriptionError(f"AssemblyAI HTTP error {err.code}: {detail}") from err
        except URLError as err:
            raise AudioTranscriptionError(f"AssemblyAI connection error: {err}") from err

    @staticmethod
    def _parse_utterances(payload: Dict[str, Any]) -> List[SpeakerUtterance]:
        utterances: List[SpeakerUtterance] = []
        for item in payload.get("utterances", []) or []:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            utterances.append(
                SpeakerUtterance(
                    speaker=str(item.get("speaker", "A")).strip() or "A",
                    text=text,
                    start_ms=int(item.get("start", 0) or 0),
                    end_ms=int(item.get("end", 0) or 0),
                )
            )
        return utterances


def format_diarized_transcript(
    utterances: List[SpeakerUtterance],
    *,
    fallback_text: str = "",
) -> str:
    intro = (
        "以下是 LINE 語音訊息的逐字稿，已先做語者分離。"
        "請根據這份帶角色標籤的逐字稿整理需求、承諾、待辦與決策，必要時主動追問缺漏資訊。"
    )
    if not utterances:
        if fallback_text:
            return f"{intro}\n\n逐字稿：\n{fallback_text}"
        raise AudioTranscriptionError("沒有可用的逐字稿內容")

    speaker_map: Dict[str, str] = {}
    next_label = ord("A")
    lines = [intro, "", "逐字稿："]
    for item in utterances:
        if item.speaker not in speaker_map:
            speaker_map[item.speaker] = f"Speaker {chr(next_label)}"
            next_label += 1
        label = speaker_map[item.speaker]
        lines.append(
            f"{label} [{format_timestamp(item.start_ms)}-{format_timestamp(item.end_ms)}]: {item.text}"
        )
    return "\n".join(lines)


def format_timestamp(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds // 1000))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
