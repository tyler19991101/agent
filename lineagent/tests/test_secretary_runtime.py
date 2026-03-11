import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from secretary_agent.audio_transcriber import format_diarized_transcript, format_timestamp
from secretary_agent.image_storage import ImageStorageManager
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
    image_storage_dir = ""
    image_retention_days = 7
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
        self.created_tasks = []
        self.updated_tasks = []
        self.completed_tasks = []
        self.deleted_tasks = []
        self.created_events = []
        self.updated_events = []
        self.deleted_events = []

    def new_state_token(self):
        token = "state-token"
        self.state_tokens.append(token)
        return token

    def create_task(self, token_payload, task):
        payload = {
            "id": f"task-{len(self.created_tasks)+1}",
            "title": task.get("title", ""),
            "status": "needsAction",
            "web_view_link": "https://tasks.example/task-1",
            "due": task.get("due", ""),
        }
        self.created_tasks.append(payload)
        return payload

    def update_task(self, token_payload, *, task_id, task):
        payload = {
            "id": task_id,
            "title": task.get("title", "開會"),
            "status": "needsAction",
            "web_view_link": "https://tasks.example/task-1",
            "due": task.get("due", ""),
        }
        self.updated_tasks.append(payload)
        return payload

    def complete_task(self, token_payload, *, task_id):
        payload = {"id": task_id, "title": "開會", "status": "completed"}
        self.completed_tasks.append(payload)
        return payload

    def delete_task(self, token_payload, *, task_id):
        payload = {"id": task_id, "status": "deleted"}
        self.deleted_tasks.append(payload)
        return payload

    def list_tasks(self, token_payload, *, show_completed=False, max_results=10):
        return {"items": list(self.tasks)[:max_results]}

    def create_event(self, token_payload, event):
        payload = {
            "id": f"event-{len(self.created_events)+1}",
            "summary": event.get("summary", ""),
            "html_link": "https://calendar.example/event-1",
            "status": "confirmed",
        }
        self.created_events.append(payload)
        return payload

    def update_event(self, token_payload, *, event_id, event):
        payload = {
            "id": event_id,
            "summary": event.get("summary", "會議"),
            "html_link": "https://calendar.example/event-1",
            "status": "confirmed",
        }
        self.updated_events.append(payload)
        return payload

    def delete_event(self, token_payload, *, event_id):
        payload = {"id": event_id, "status": "cancelled"}
        self.deleted_events.append(payload)
        return payload

    def list_events(self, token_payload, *, time_min=None, time_max=None, max_results=10):
        return {"items": list(self.events)[:max_results]}


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


class FakeAdminNotifier:
    def __init__(self):
        self.calls = []

    def notify_system_error(self, *, event, summary, fields=None):
        self.calls.append({"event": event, "summary": summary, "fields": fields or {}})


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

    def plan(self, *, memory_key, user_goal, runtime_context, files=None):
        self.calls.append(
            {
                "memory_key": memory_key,
                "user_goal": user_goal,
                "runtime_context": runtime_context,
                "files": files or [],
            }
        )
        if not self.results:
            raise AssertionError("No fake planner result available")
        return self.results.pop(0)


class RaisingAgentClient:
    def __init__(self, error):
        self.error = error

    def plan(self, *, memory_key, user_goal, runtime_context, files=None):
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

    def test_store_build_runtime_context_includes_image_assets(self):
        image_dir = os.path.join(self.tempdir.name, "images")
        storage = ImageStorageManager(base_dir=image_dir, retention_days=7)
        saved = storage.save_image(message_id="img-1", image_bytes=b"\x89PNG\r\n\x1a\nfakepng")
        run_id, _ = self.store.create_task_run(
            memory_key="user:U123",
            user_goal="請分析圖片",
            normalized_goal="請分析圖片",
            source_payload={},
            external_event_id="evt-img-1",
        )
        image_asset_id = self.store.create_image_asset(
            memory_key="user:U123",
            message_id="img-1",
            sha256=saved.sha256,
            mime_type=saved.mime_type,
            size_bytes=saved.size_bytes,
            path=saved.path,
            expires_at=saved.expires_at,
        )
        self.store.attach_image_assets_to_run(run_id, [image_asset_id])

        context = self.store.build_runtime_context(run_id, "user:U123")
        self.assertEqual(len(context["image_assets"]), 1)
        self.assertEqual(context["image_assets"][0]["message_id"], "img-1")
        self.assertEqual(context["image_assets"][0]["mime_type"], "image/png")

    def test_runtime_cleanup_expired_images_marks_deleted(self):
        image_dir = os.path.join(self.tempdir.name, "images")
        storage = ImageStorageManager(base_dir=image_dir, retention_days=7)
        saved = storage.save_image(message_id="img-expired", image_bytes=b"\xff\xd8\xffjpeg")
        image_asset_id = self.store.create_image_asset(
            memory_key="user:U123",
            message_id="img-expired",
            sha256=saved.sha256,
            mime_type=saved.mime_type,
            size_bytes=saved.size_bytes,
            path=saved.path,
            expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient([PlannerResult(final_reply="ok")]),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
        )

        runtime._cleanup_expired_image_assets()
        self.assertFalse(os.path.exists(saved.path))
        with self.store.connect() as conn:
            row = conn.execute("SELECT status FROM image_assets WHERE id = ?", (image_asset_id,)).fetchone()
        self.assertEqual(row["status"], "deleted")

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

    def test_casual_message_does_not_resume_pending_approval(self):
        agent_client = FakeAgentClient(
            [
                PlannerResult(
                    task_type="trip_planning",
                    requires_approval=True,
                    approval_type="missing_info",
                    approval_prompt="請告訴我出發日期與預算。",
                ),
                PlannerResult(
                    conversation_mode="casual_reply",
                    context_usage="none",
                    task_type="information_request",
                    final_reply="你好，我在。你可以直接告訴我現在要我幫你做什麼。",
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
        runtime.handle_inbound_message(self.inbound("我要去日本玩", line_event_id="evt-a"))
        runtime.process_next_run()

        runtime.handle_inbound_message(self.inbound("hi", line_event_id="evt-b"))

        self.assertEqual(self.messenger.replies[-1][1], "你好，我在。你可以直接告訴我現在要我幫你做什麼。")
        pending = self.store.get_open_approval("user:U123")
        self.assertIsNotNone(pending)
        self.assertEqual(int(pending["run_id"]), 1)

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

    def test_runtime_notifies_admin_on_task_failure(self):
        notifier = FakeAdminNotifier()
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=RaisingAgentClient(RuntimeError("planner exploded")),
            google_client=FakeGoogleClient(),
            browser_automation=FakeBrowserAutomation(),
            admin_notifier=notifier,
        )

        runtime.handle_inbound_message(self.inbound("幫我查今天行程"))
        runtime.process_next_run()

        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["event"], "task_failed")
        self.assertEqual(notifier.calls[0]["fields"]["error_type"], "RuntimeError")

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

    def test_update_task_uses_recent_google_artifact(self):
        google = FakeGoogleClient(configured=True)
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        task_action={"operation": "update_task", "title": "開會", "due": "2026-03-12T15:00:00+08:00"},
                        final_reply="已幫你調整提醒時間。",
                    )
                ]
            ),
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="user@example.com",
            display_name="User",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        run_id, _ = self.store.create_task_run(
            memory_key="user:U123",
            user_goal="提醒我明天下午三點開會",
            normalized_goal="提醒我明天下午三點開會",
            source_payload={},
            external_event_id="evt-prior",
        )
        self.store.add_artifact(
            run_id,
            kind="google_task",
            ref_key="task-1",
            content="開會",
            metadata={"task_id": "task-1", "title": "開會", "web_view_link": "https://tasks.example/task-1"},
        )
        runtime.handle_inbound_message(self.inbound("改到3/12", line_event_id="evt-update"))
        runtime.process_next_run()
        self.assertEqual(len(google.updated_tasks), 1)
        self.assertEqual(google.updated_tasks[0]["id"], "task-1")
        self.assertIn("已替你調整 Google Tasks 提醒", self.messenger.pushes[0][1])

    def test_ambiguous_task_update_requires_selection_then_updates_selected_item(self):
        google = FakeGoogleClient(configured=True)
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        task_action={"operation": "update_task", "due": "2026-03-12T15:00:00+08:00"},
                        final_reply="已幫你調整提醒時間。",
                    ),
                    PlannerResult(
                        task_type="action_prep",
                        task_action={"operation": "update_task", "due": "2026-03-12T15:00:00+08:00"},
                        final_reply="已幫你調整提醒時間。",
                    ),
                ]
            ),
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="user@example.com",
            display_name="User",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        run_id, _ = self.store.create_task_run(
            memory_key="user:U123",
            user_goal="提醒我明天下午三點開會",
            normalized_goal="提醒我明天下午三點開會",
            source_payload={},
            external_event_id="evt-prior",
        )
        self.store.add_artifact(
            run_id,
            kind="google_task",
            ref_key="task-1",
            content="開會",
            metadata={"task_id": "task-1", "title": "開會", "due": "2026-03-09T15:00:00+08:00"},
        )
        self.store.add_artifact(
            run_id,
            kind="google_task",
            ref_key="task-2",
            content="開會",
            metadata={"task_id": "task-2", "title": "開會", "due": "2026-03-10T15:00:00+08:00"},
        )

        runtime.handle_inbound_message(self.inbound("改到3/12", line_event_id="evt-update"))
        runtime.process_next_run()

        self.assertEqual(len(google.updated_tasks), 0)
        self.assertIn("請直接回覆選項編號或完整事項名稱", self.messenger.pushes[0][1])
        self.assertIn("1. 開會（2026-03-10 15:00:00）", self.messenger.pushes[0][1])
        self.assertIn("2. 開會（2026-03-09 15:00:00）", self.messenger.pushes[0][1])

        runtime.handle_inbound_message(self.inbound("2", line_event_id="evt-update-2"))
        runtime.process_next_run()

        self.assertEqual(len(google.updated_tasks), 1)
        self.assertEqual(google.updated_tasks[0]["id"], "task-1")
        self.assertIn("收到你的回覆，我繼續處理", self.messenger.replies[-1][1])
        self.assertIn("已替你調整 Google Tasks 提醒", self.messenger.pushes[-1][1])

    def test_browser_request_is_deferred_for_next_phase(self):
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
        self.assertEqual(len(browser.calls), 0)
        self.assertIn("下一階段", self.messenger.pushes[0][1])

    def test_invalid_browser_request_is_ignored(self):
        browser = FakeBrowserAutomation(store=self.store)
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="research_and_compare",
                        final_reply="附近有幾家餐廳可以考慮。",
                        browser_request={"domain": "agoda.com", "intent": "   "},
                    )
                ]
            ),
            google_client=FakeGoogleClient(),
            browser_automation=browser,
        )
        runtime.handle_inbound_message(self.inbound("三峽北大有什麼好吃的", line_event_id="evt-invalid-browser-request"))
        runtime.process_next_run()
        self.assertEqual(len(browser.calls), 0)
        self.assertEqual(len(self.messenger.pushes), 1)
        self.assertIn("附近有幾家餐廳可以考慮", self.messenger.pushes[0][1])
        self.assertNotIn("下一階段", self.messenger.pushes[0][1])

    def test_calendar_query_executes_dify_list_events_action(self):
        google = FakeGoogleClient(
            configured=True,
            events=[
                {
                    "summary": "開會",
                    "start": {"dateTime": "2026-03-10T15:00:00+08:00"},
                }
            ],
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        calendar_action={
                            "operation": "list_events",
                            "time_min": "2026-03-08T00:00:00+08:00",
                            "time_max": "2026-03-15T00:00:00+08:00",
                            "timezone": "Asia/Taipei",
                        },
                        final_reply="你近期的 Google 行程如下：",
                    )
                ]
            ),
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="google-linked-account",
            display_name="Google",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        runtime.handle_inbound_message(self.inbound("我近期的行程有什麼", line_event_id="evt-calendar-query"))
        runtime.process_next_run()
        self.assertIn("你近期的 Google 行程", self.messenger.pushes[0][1])
        self.assertIn("開會", self.messenger.pushes[0][1])

    def test_task_query_executes_dify_list_tasks_action(self):
        google = FakeGoogleClient(
            configured=True,
            tasks=[{"title": "開會", "due": "2026-03-10T15:00:00+08:00", "status": "needsAction"}],
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        task_action={"operation": "list_tasks"},
                        final_reply="你目前的提醒如下：",
                    )
                ]
            ),
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="google-linked-account",
            display_name="Google",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        runtime.handle_inbound_message(self.inbound("我目前有哪些提醒", line_event_id="evt-task-query"))
        runtime.process_next_run()
        self.assertIn("待辦事項", self.messenger.pushes[0][1])
        self.assertIn("開會", self.messenger.pushes[0][1])

    def test_google_query_without_structured_action_triggers_contract_replan(self):
        google = FakeGoogleClient(
            configured=True,
            tasks=[{"title": "開會", "due": "2026-03-10T18:00:00+08:00", "status": "needsAction"}],
        )
        agent = FakeAgentClient(
            [
                PlannerResult(task_type="information_request", final_reply="你今天目前有 1 件待辦事項。"),
                PlannerResult(
                    task_type="action_prep",
                    task_action={"operation": "list_tasks"},
                    final_reply="你今天的提醒如下：",
                ),
            ]
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=agent,
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="google-linked-account",
            display_name="Google",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        runtime.handle_inbound_message(self.inbound("我今天有什麼事情", line_event_id="evt-google-query-replan"))
        runtime.process_next_run()
        self.assertEqual(len(agent.calls), 2)
        self.assertIn("Google 個人助理查詢", agent.calls[1]["user_goal"])
        self.assertIn("待辦事項", self.messenger.pushes[0][1])
        self.assertIn("開會", self.messenger.pushes[0][1])

    def test_trip_planning_does_not_trigger_google_auth_for_itinerary_text_request(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="trip_planning",
                        final_reply="這是 6 天 5 夜的日本名古屋行程建議。",
                        calendar_action={
                            "operation": "create_event",
                            "summary": "日本名古屋行程",
                            "start": "2026-09-28T09:00:00+08:00",
                            "end": "2026-09-28T18:00:00+08:00",
                        },
                    )
                ]
            ),
            google_client=FakeGoogleClient(configured=True),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("安排9/28-10/11之間，日本最佳行程，地點名古屋及附近城市，住飯店，列出6天5夜行程，並預估所有經費"))
        runtime.process_next_run()
        self.assertNotIn("Google 授權", self.messenger.pushes[0][1])
        self.assertIn("名古屋行程建議", self.messenger.pushes[0][1])

    def test_invalid_empty_calendar_action_is_ignored(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="research_and_compare",
                        final_reply="附近有幾家餐廳可以考慮。",
                        calendar_action={"operation": "   "},
                    )
                ]
            ),
            google_client=FakeGoogleClient(configured=True),
            browser_automation=FakeBrowserAutomation(),
        )
        runtime.handle_inbound_message(self.inbound("三峽北大有什麼好吃的", line_event_id="evt-empty-calendar-action"))
        runtime.process_next_run()
        self.assertEqual(len(self.messenger.pushes), 1)
        self.assertIn("附近有幾家餐廳可以考慮", self.messenger.pushes[0][1])

    def test_invalid_calendar_action_falls_back_to_valid_task_action(self):
        google = FakeGoogleClient(
            configured=True,
            tasks=[{"title": "開會", "due": "2026-03-10T15:00:00+08:00", "status": "needsAction"}],
        )
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [
                    PlannerResult(
                        task_type="action_prep",
                        calendar_action={"operation": ""},
                        task_action={"operation": "list_tasks"},
                        final_reply="你目前的提醒如下：",
                    )
                ]
            ),
            google_client=google,
            browser_automation=FakeBrowserAutomation(),
        )
        self.store.upsert_connected_account(
            "user:U123",
            service_name="google",
            login_identifier="google-linked-account",
            display_name="Google",
            oauth_provider="google",
            session_available=True,
            metadata={"access_token": "fake", "refresh_token": "fake"},
        )
        runtime.handle_inbound_message(self.inbound("我目前有哪些提醒", line_event_id="evt-task-fallback"))
        runtime.process_next_run()
        self.assertIn("待辦事項", self.messenger.pushes[0][1])
        self.assertIn("開會", self.messenger.pushes[0][1])

    def test_declining_google_auth_replans_as_text_only(self):
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient(
                [PlannerResult(task_type="trip_planning", final_reply="改成純文字行程規劃。")]
            ),
            google_client=FakeGoogleClient(configured=True),
            browser_automation=FakeBrowserAutomation(),
        )
        run_id, _ = self.store.create_task_run(
            memory_key="user:U123",
            user_goal="安排9/28-10/11之間，日本最佳行程",
            normalized_goal="安排9/28-10/11之間，日本最佳行程",
            source_payload={},
            external_event_id="evt-prior-google",
        )
        approval_id = self.store.create_pending_approval(
            run_id=run_id,
            memory_key="user:U123",
            approval_type="decision",
            prompt_text="要替你建立 Google 行程/提醒，請先完成 Google 授權：...",
            options=[],
        )
        self.store.update_run_status(
            run_id,
            status="waiting_approval",
            current_phase="awaiting_google_auth",
            requires_approval=True,
        )
        self.store.set_approval_prompt_message(approval_id, "push-google-auth")
        runtime.handle_inbound_message(self.inbound("先不用進入行事曆，只要給我文字稿", quoted_message_id="push-google-auth", line_event_id="evt-text-only"))
        self.assertIn("改成只提供文字規劃", self.messenger.replies[0][1])
        runtime.process_next_run()
        self.assertIn("純文字行程規劃", self.messenger.pushes[0][1])

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
