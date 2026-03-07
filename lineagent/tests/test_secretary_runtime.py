import os
import tempfile
import unittest

from secretary_agent.audio_transcriber import format_diarized_transcript, format_timestamp
from secretary_agent.memory import SQLiteStore
from secretary_agent.models import InboundMessage, PlannerResult, SpeakerUtterance
from secretary_agent.runtime import SecretaryRuntime
from secretary_agent.transport_line import (
    extract_sent_message_ids,
    format_location_message,
    infer_media_metadata,
)
from secretary_agent.utils import infer_requested_outputs, prefers_file_only_response, split_text


class FakeSettings:
    dify_api_key = "fake"
    dify_base_url = "https://example.invalid/v1"
    dify_user_prefix = "line"
    worker_poll_seconds = 0.01
    short_context_ttl_days = 14
    public_base_url = "https://example.com"
    artifact_output_dir = ""
    google_client_id = ""
    google_client_secret = ""
    google_redirect_uri = ""
    google_calendar_id = "primary"
    google_tasklist_id = "@default"
    browser_automation_enabled = False


class FakeGoogleClient:
    def __init__(self, *, configured=True, events=None, tasks=None):
        self.is_configured = configured
        self.events = events or []
        self.tasks = tasks or []
        self.state_tokens = []

    def new_state_token(self):
        token = "state-token"
        self.state_tokens.append(token)
        return token


class FakeBrowserAutomation:
    enabled = False

    def __init__(self, store=None):
        self.calls = []
        self.store = store

    def create_checkpoint(self, *, run_id, memory_key, browser_request, profile, accounts):
        self.calls.append(
            {
                "run_id": run_id,
                "memory_key": memory_key,
                "browser_request": browser_request,
                "profile": profile,
                "accounts": accounts,
            }
        )
        if self.store is not None:
            self.store.create_sensitive_checkpoint(
                token="checkpoint-token",
                run_id=run_id,
                memory_key=memory_key,
                checkpoint_type="browser_review",
                prompt_text="請確認",
                payload={"browser_request": browser_request},
            )
        return {
            "automation_id": 1,
            "checkpoint_token": "checkpoint-token",
            "prompt_text": "請確認",
        }


class FakeMessenger:
    def __init__(self):
        self.replies = []
        self.pushes = []
        self.counter = 0

    def split_for_storage(self, text):
        return split_text(text, 4300)

    def reply_text(self, reply_token, text):
        self.counter += 1
        message_id = f"reply-{self.counter}"
        self.replies.append((reply_token, text, message_id))
        return [message_id]

    def push_text(self, target_id, text):
        chunks = split_text(text, 4300)
        ids = []
        for chunk in chunks:
            self.counter += 1
            message_id = f"push-{self.counter}"
            self.pushes.append((target_id, chunk, message_id))
            ids.append(message_id)
        return ids


class FakeAgentClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def plan(self, *, memory_key, user_goal, runtime_context):
        self.calls.append(
            {
                "memory_key": memory_key,
                "user_goal": user_goal,
                "runtime_context": runtime_context,
            }
        )
        if not self.results:
            raise AssertionError("No fake planner result available")
        return self.results.pop(0)


class RaisingAgentClient:
    def __init__(self, error):
        self.error = error

    def plan(self, *, memory_key, user_goal, runtime_context):
        raise self.error


class SecretaryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "bot_memory.sqlite3")
        FakeSettings.artifact_output_dir = os.path.join(self.tempdir.name, "output", "doc")
        self.store = SQLiteStore(self.db_path)
        self.messenger = FakeMessenger()

    def tearDown(self):
        self.tempdir.cleanup()

    def inbound(self, text, quoted_message_id=None, line_event_id="evt-1"):
        from datetime import datetime, timezone

        return InboundMessage(
            source_type="user",
            source_id="U123",
            user_id="U123",
            reply_token="reply-token",
            reply_enabled=True,
            text=text,
            quoted_message_id=quoted_message_id,
            received_at=datetime.now(timezone.utc),
            line_event_id=line_event_id,
        )

    def test_new_message_queues_task_and_replies_ack(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [PlannerResult(task_type="information_request", final_reply="這是結果")]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我整理明天的工作"))
        self.assertEqual(len(self.messenger.replies), 1)
        self.assertIn("任務已收到", self.messenger.replies[0][1])
        processed = runtime.process_next_run()
        self.assertTrue(processed)
        self.assertEqual(len(self.messenger.pushes), 1)
        self.assertIn("這是結果", self.messenger.pushes[0][1])

    def test_final_reply_appends_action_links_when_missing_from_text(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="information_request",
                        final_reply="這是摘要",
                        action_links=[
                            {"label": "Skyscanner", "url": "https://www.skyscanner.com.tw"},
                        ],
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我查機票"))
        runtime.process_next_run()
        self.assertIn("這是摘要", self.messenger.pushes[0][1])
        self.assertIn("相關連結：", self.messenger.pushes[0][1])
        self.assertIn("https://www.skyscanner.com.tw", self.messenger.pushes[0][1])

    def test_runtime_generates_requested_output_files(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="information_request",
                        final_reply="這是整理好的會議摘要",
                        requested_outputs=["txt", "docx"],
                        document_title="會議摘要",
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我整理會議並輸出成 word 和 txt"))
        runtime.process_next_run()
        pushed = self.messenger.pushes[0][1]
        self.assertIn("已完成，請下載檔案：", pushed)
        self.assertIn("DOCX", pushed)
        self.assertIn("TXT", pushed)
        artifacts = self.store.get_artifacts(1, kind="generated_file")
        self.assertEqual(len(artifacts), 2)

    def test_runtime_can_include_summary_and_artifact_links_when_user_wants_both(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="information_request",
                        final_reply="這是整理好的會議摘要",
                        requested_outputs=["pdf"],
                        document_title="會議摘要",
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我整理成 PDF，摘要也要一起顯示"))
        runtime.process_next_run()
        pushed = self.messenger.pushes[0][1]
        self.assertIn("這是整理好的會議摘要", pushed)
        self.assertIn("輸出檔案：", pushed)
        self.assertIn("PDF", pushed)

    def test_runtime_prefers_file_only_response_when_user_only_wants_file(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="information_request",
                        final_reply="這是整理好的會議摘要",
                        requested_outputs=["docx"],
                        document_title="會議摘要",
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我整理成 Word，最後只要給我檔案下載連結"))
        runtime.process_next_run()
        pushed = self.messenger.pushes[0][1]
        self.assertTrue(pushed.startswith("已完成，請下載檔案："))
        self.assertNotIn("這是整理好的會議摘要", pushed)

    def test_reset_clears_history_and_profile(self):
        self.store.append_history("user:U123", "user", "hello")
        self.store.update_profile("user:U123", {"budget": "mid"})
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient([]),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("reset"))
        self.assertEqual(self.store.history_to_text("user:U123"), "")
        self.assertEqual(self.store.get_profile("user:U123"), {})

    def test_approval_flow_resumes_existing_run(self):
        agent_client = FakeAgentClient(
            [
                PlannerResult(
                    task_type="trip_planning",
                    requires_approval=True,
                    approval_type="missing_info",
                    approval_prompt="請告訴我出發日期與預算。",
                    draft_user_reply="我先幫你規劃泰國旅行。",
                ),
                PlannerResult(
                    task_type="trip_planning",
                    final_reply="以下是泰國旅遊方案與付款連結。",
                ),
            ]
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=agent_client,
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("我要到泰國旅遊", line_event_id="evt-a"))
        runtime.process_next_run()
        self.assertEqual(len(self.messenger.pushes), 1)
        prompt_message_id = self.messenger.pushes[0][2]

        runtime.handle_inbound_message(
            self.inbound("3/20 出發，預算 4 萬", quoted_message_id=prompt_message_id, line_event_id="evt-b")
        )
        self.assertIn("繼續處理", self.messenger.replies[-1][1])
        runtime.process_next_run()
        self.assertEqual(len(self.messenger.pushes), 2)

    def test_memory_command_saves_profile_without_queueing_task(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient([]),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("記住我常用 email 是 user@example.com"))
        profile = self.store.get_profile("user:U123")
        self.assertEqual(profile.get("contact_email"), "user@example.com")
        self.assertIn("已記住你的常用 email", self.messenger.replies[0][1])

    def test_google_action_requests_oauth_when_account_not_connected(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        final_reply="",
                        task_action={"operation": "create_task", "title": "交報告"},
                    )
                ]
            ),
            google_client=FakeGoogleClient(configured=True),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("提醒我明天交報告"))
        runtime.process_next_run()
        self.assertIn("/auth/google/start?state=state-token", self.messenger.pushes[0][1])
        run = self.store.get_task_run(1)
        self.assertEqual(run.current_phase, "awaiting_google_auth")

    def test_browser_request_creates_sensitive_checkpoint(self):
        browser = FakeBrowserAutomation(store=self.store)
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        browser_request={
                            "domain": "agoda.com",
                            "intent": "fill_booking",
                            "target_items": ["曼谷飯店"],
                            "user_profile_fields_needed": ["contact_email"],
                            "stop_before_payment": True,
                        },
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=browser,
        )
        self.store.update_profile("user:U123", {"contact_email": "user@example.com"})
        runtime.handle_inbound_message(self.inbound("幫我填到結帳前"))
        runtime.process_next_run()
        self.assertEqual(len(browser.calls), 1)
        checkpoint = self.store.get_sensitive_checkpoint("checkpoint-token")
        self.assertIsNotNone(checkpoint)
        self.assertIn("/automation/checkpoint-token", self.messenger.pushes[0][1])

    def test_split_text_chunks_long_messages(self):
        text = "a" * 9000
        chunks = split_text(text, 4300)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sum(len(chunk) for chunk in chunks), 9000)

    def test_runtime_hides_internal_error_from_user(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=RaisingAgentClient(RuntimeError("Dify HTTP error 400: sensitive details")),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("幫我查資料"))
        runtime.process_next_run()
        self.assertEqual(len(self.messenger.pushes), 1)
        self.assertEqual(
            self.messenger.pushes[0][1],
            "目前系統發生異常，已通報 IT 人員協助處理，請稍後再試。",
        )

    def test_prune_short_context_keeps_profile_but_removes_old_history_and_stale_approval(self):
        self.store.append_history("user:U123", "user", "近期訊息")
        self.store.update_profile("user:U123", {"travel_style": "美食購物"})
        run_id, _ = self.store.create_task_run(
            memory_key="user:U123",
            user_goal="舊任務",
            normalized_goal="舊任務",
            source_payload={},
            external_event_id="evt-old",
        )
        approval_id = self.store.create_pending_approval(
            run_id=run_id,
            memory_key="user:U123",
            approval_type="missing_info",
            prompt_text="請補日期",
        )
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE conversation_history SET created_at = datetime('now', '-20 days') WHERE memory_key = ?",
                ("user:U123",),
            )
            conn.execute(
                "UPDATE task_runs SET status = 'waiting_approval', current_phase = 'waiting_approval', created_at = datetime('now', '-20 days') WHERE id = ?",
                (run_id,),
            )
            conn.execute(
                "UPDATE pending_approvals SET created_at = datetime('now', '-20 days') WHERE id = ?",
                (approval_id,),
            )
            conn.commit()

        self.store.prune_short_context("user:U123", 14)

        self.assertEqual(self.store.history_to_text("user:U123"), "")
        self.assertEqual(self.store.get_profile("user:U123"), {"travel_style": "美食購物"})
        pending = self.store.get_open_approval("user:U123")
        self.assertIsNone(pending)
        run = self.store.get_task_run(run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.current_phase, "expired")

    def test_extract_sent_message_ids_handles_missing_response(self):
        self.assertEqual(extract_sent_message_ids(None), [])

    def test_background_inbound_does_not_send_ack_reply(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [PlannerResult(task_type="information_request", final_reply="整理完成")]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )
        inbound = self.inbound("語音轉文字內容")
        inbound.reply_enabled = False
        runtime.handle_inbound_message(inbound)
        self.assertEqual(len(self.messenger.replies), 0)
        runtime.process_next_run()
        self.assertEqual(len(self.messenger.pushes), 1)
        self.assertIn("整理完成", self.messenger.pushes[0][1])

    def test_format_diarized_transcript_adds_speaker_labels(self):
        transcript = format_diarized_transcript(
            [
                SpeakerUtterance(speaker="0", text="這次的開發時程大概要多久？", start_ms=1000, end_ms=4000),
                SpeakerUtterance(speaker="1", text="韌體那邊大概需要兩週。", start_ms=5000, end_ms=8000),
                SpeakerUtterance(speaker="0", text="好，那就定在下個月初。", start_ms=9000, end_ms=11000),
            ]
        )
        self.assertIn("Speaker A [00:01-00:04]: 這次的開發時程大概要多久？", transcript)
        self.assertIn("Speaker B [00:05-00:08]: 韌體那邊大概需要兩週。", transcript)

    def test_format_timestamp_supports_hours(self):
        self.assertEqual(format_timestamp(3723000), "01:02:03")

    def test_infer_media_metadata_for_audio_message_defaults_to_m4a(self):
        message = type("Msg", (), {"type": "audio"})()
        filename, mime_type = infer_media_metadata(message)
        self.assertEqual(filename, "audio.m4a")
        self.assertEqual(mime_type, "audio/m4a")

    def test_infer_media_metadata_for_mp3_file_message(self):
        message = type("Msg", (), {"type": "file", "file_name": "meeting.mp3"})()
        filename, mime_type = infer_media_metadata(message)
        self.assertEqual(filename, "meeting.mp3")
        self.assertEqual(mime_type, "audio/mpeg")

    def test_infer_requested_outputs_from_user_text(self):
        outputs = infer_requested_outputs("幫我整理內容，輸出成 Word、PDF 和 txt")
        self.assertEqual(outputs, ["docx", "pdf", "txt"])

    def test_prefers_file_only_response(self):
        self.assertTrue(prefers_file_only_response("幫我整理成 Word，最後只要給我檔案下載連結"))
        self.assertFalse(prefers_file_only_response("幫我整理成 Word，摘要也要一起顯示"))

    def test_format_location_message_contains_address_and_coordinates(self):
        class FakeLocation:
            title = "目前位置"
            address = "台北市信義區市府路 45 號"
            latitude = 25.033968
            longitude = 121.564468

        text = format_location_message(FakeLocation())
        self.assertIn("LINE 位置訊息", text)
        self.assertIn("台北市信義區市府路 45 號", text)
        self.assertIn("25.033968, 121.564468", text)


if __name__ == "__main__":
    unittest.main()
