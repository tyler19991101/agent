import os
import tempfile
import unittest

from secretary_agent.memory import SQLiteStore
from secretary_agent.models import InboundMessage, PlannerResult
from secretary_agent.runtime import SecretaryRuntime
from secretary_agent.transport_line import extract_sent_message_ids
from secretary_agent.utils import split_text


class FakeSettings:
    dify_api_key = "fake"
    dify_base_url = "https://example.invalid/v1"
    dify_user_prefix = "line"
    worker_poll_seconds = 0.01


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


class SecretaryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "bot_memory.sqlite3")
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
        )
        runtime.handle_inbound_message(self.inbound("幫我查機票"))
        runtime.process_next_run()
        self.assertIn("這是摘要", self.messenger.pushes[0][1])
        self.assertIn("相關連結：", self.messenger.pushes[0][1])
        self.assertIn("https://www.skyscanner.com.tw", self.messenger.pushes[0][1])

    def test_reset_clears_history_and_profile(self):
        self.store.append_history("user:U123", "user", "hello")
        self.store.update_profile("user:U123", {"budget": "mid"})
        runtime = SecretaryRuntime(
            settings=FakeSettings(),
            store=self.store,
            messenger=self.messenger,
            agent_client=FakeAgentClient([]),
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
        self.assertIn("泰國旅遊方案", self.messenger.pushes[-1][1])
        self.assertEqual(len(agent_client.calls), 2)
        self.assertIn("原始任務：我要到泰國旅遊", agent_client.calls[-1]["user_goal"])
        self.assertIn("3/20 出發，預算 4 萬", agent_client.calls[-1]["user_goal"])

    def test_split_text_chunks_long_messages(self):
        text = "a" * 9000
        chunks = split_text(text, 4300)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sum(len(chunk) for chunk in chunks), 9000)

    def test_extract_sent_message_ids_handles_missing_response(self):
        self.assertEqual(extract_sent_message_ids(None), [])


if __name__ == "__main__":
    unittest.main()
