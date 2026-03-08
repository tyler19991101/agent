import json
import os
import unittest


class GoldenSetContractTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__), "golden_set.json")
        with open(self.path, "r", encoding="utf-8") as fh:
            self.cases = json.load(fh)

    def test_golden_set_has_unique_case_ids(self):
        case_ids = [case["id"] for case in self.cases]
        self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_golden_set_covers_core_categories(self):
        categories = {case["category"] for case in self.cases}
        self.assertTrue(
            {
                "casual_reply",
                "continuation",
                "calendar_query",
                "task_query",
                "trip_planning",
                "memory",
                "file_export",
            }.issubset(categories)
        )

    def test_each_case_has_minimum_contract_fields(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIsInstance(case["user_input"], str)
                self.assertTrue(case["user_input"].strip())
                self.assertIn("expected", case)
                self.assertIsInstance(case["expected"], dict)
                self.assertIn("conversation_mode", case["expected"])

    def test_cases_with_execution_fields_only_use_supported_keys(self):
        allowed_keys = {
            "calendar_action",
            "task_action",
            "memory_actions",
            "profile_updates",
            "account_updates",
            "requested_outputs",
        }
        for case in self.cases:
            execution_fields = case["expected"].get("execution_fields", {})
            with self.subTest(case=case["id"]):
                self.assertTrue(set(execution_fields).issubset(allowed_keys))


if __name__ == "__main__":
    unittest.main()
