import unittest

from secretary_agent.dify_client import DifyAgentClient


class DifyAgentClientParseAnswerTest(unittest.TestCase):
    def make_client(self) -> DifyAgentClient:
        return DifyAgentClient(
            api_key="fake",
            base_url="https://example.invalid/v1",
            user_prefix="line",
        )

    def test_parse_answer_discards_blank_calendar_operation(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "research_and_compare",
              "final_reply": "附近有幾家餐廳可以考慮。",
              "calendar_action": {
                "operation": "   "
              }
            }
            """
        )

        self.assertEqual(result.calendar_action, {})
        self.assertEqual(result.final_reply, "附近有幾家餐廳可以考慮。")

    def test_parse_answer_discards_unknown_calendar_operation(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "action_prep",
              "final_reply": "已整理。",
              "calendar_action": {
                "operation": "search_restaurants",
                "query": "三峽北大"
              }
            }
            """
        )

        self.assertEqual(result.calendar_action, {})

    def test_parse_answer_keeps_valid_task_action(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "action_prep",
              "final_reply": "你目前的提醒如下：",
              "task_action": {
                "operation": "list_tasks"
              }
            }
            """
        )

        self.assertEqual(result.task_action, {"operation": "list_tasks"})

    def test_parse_answer_discards_incomplete_browser_request(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "action_prep",
              "final_reply": "目前先提供一般建議。",
              "browser_request": {
                "domain": "agoda.com",
                "intent": "   "
              }
            }
            """
        )

        self.assertEqual(result.browser_request, {})

    def test_parse_answer_normalizes_memory_actions_and_requested_outputs(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "information_request",
              "final_reply": "已整理。",
              "memory_actions": ["save_profile", "bad_action", "save_profile"],
              "requested_outputs": ["PDF", "docx", "xlsx"]
            }
            """
        )

        self.assertEqual(result.memory_actions, ["save_profile"])
        self.assertEqual(result.requested_outputs, ["pdf", "docx"])

    def test_parse_answer_keeps_conversation_mode_and_context_usage(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "conversation_mode": "casual_reply",
              "context_usage": "none",
              "task_type": "information_request",
              "final_reply": "你好，我在。"
            }
            """
        )

        self.assertEqual(result.conversation_mode, "casual_reply")
        self.assertEqual(result.context_usage, "none")

    def test_parse_answer_drops_memory_updates_without_memory_actions(self):
        client = self.make_client()
        result = client._parse_answer(
            """
            {
              "task_type": "information_request",
              "final_reply": "已整理。",
              "profile_updates": {
                "food_preference": "牛肉麵"
              },
              "account_updates": [
                {
                  "service_name": "google",
                  "login_identifier": "user@example.com"
                }
              ]
            }
            """
        )

        self.assertEqual(result.memory_actions, [])
        self.assertEqual(result.profile_updates, {})
        self.assertEqual(result.account_updates, [])

    def test_build_prompt_contains_only_goal_and_runtime_context(self):
        client = self.make_client()
        prompt = client._build_prompt(
            user_goal="提醒我明天下午三點開會",
            runtime_context={"current_date_local": "2026-03-08", "current_timezone": "Asia/Taipei"},
        )

        self.assertIn("使用者最新目標：提醒我明天下午三點開會", prompt)
        self.assertIn('"current_date_local": "2026-03-08"', prompt)
        self.assertNotIn("Execution contract:", prompt)

    def test_build_chat_payload_includes_uploaded_files(self):
        client = self.make_client()
        payload = client._build_chat_payload(
            query="請分析這張圖",
            user="line:user:U123",
            files=[
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": "file-123",
                }
            ],
        )

        self.assertEqual(payload["query"], "請分析這張圖")
        self.assertEqual(payload["user"], "line:user:U123")
        self.assertEqual(payload["files"][0]["upload_file_id"], "file-123")


if __name__ == "__main__":
    unittest.main()
