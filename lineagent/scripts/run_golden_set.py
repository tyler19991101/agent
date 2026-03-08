import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from secretary_agent.dify_client import DifyAgentClient


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_cases(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_runtime_context() -> Dict[str, Any]:
    now = datetime.now().astimezone()
    return {
        "history": "",
        "profile": {},
        "connected_accounts": [],
        "artifacts": [],
        "recent_service_artifacts": [],
        "latest_approval_response": "",
        "current_datetime_local": now.isoformat(),
        "current_date_local": now.date().isoformat(),
        "current_timezone": str(now.tzinfo or "UTC"),
    }


def get_nested(data: Dict[str, Any], path: str) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def planner_to_dict(result: Any) -> Dict[str, Any]:
    return {
        "conversation_mode": result.conversation_mode,
        "context_usage": result.context_usage,
        "task_type": result.task_type,
        "goal_summary": result.goal_summary,
        "subtasks": result.subtasks,
        "needed_inputs": result.needed_inputs,
        "requires_approval": result.requires_approval,
        "approval_type": result.approval_type,
        "approval_prompt": result.approval_prompt,
        "draft_user_reply": result.draft_user_reply,
        "final_reply": result.final_reply,
        "options": result.options,
        "recommendation": result.recommendation,
        "rationale": result.rationale,
        "action_links": result.action_links,
        "warnings": result.warnings,
        "missing_info": result.missing_info,
        "profile_updates": result.profile_updates,
        "account_updates": result.account_updates,
        "memory_actions": result.memory_actions,
        "calendar_action": result.calendar_action,
        "task_action": result.task_action,
        "requested_outputs": result.requested_outputs,
        "document_title": result.document_title,
        "raw_answer": result.raw_answer,
    }


def check_expectations(expected: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    for key in ("conversation_mode", "context_usage", "task_type", "document_title"):
        if key in expected and actual.get(key) != expected[key]:
            errors.append(f"{key}: expected {expected[key]!r}, got {actual.get(key)!r}")

    if "requested_outputs" in expected and actual.get("requested_outputs") != expected["requested_outputs"]:
        errors.append(
            f"requested_outputs: expected {expected['requested_outputs']!r}, got {actual.get('requested_outputs')!r}"
        )

    execution_fields = expected.get("execution_fields", {})
    for field_name, field_expectation in execution_fields.items():
        actual_value = actual.get(field_name)
        if isinstance(field_expectation, dict):
            if not isinstance(actual_value, dict):
                errors.append(f"{field_name}: expected dict, got {type(actual_value).__name__}")
                continue
            for subkey, subvalue in field_expectation.items():
                if actual_value.get(subkey) != subvalue:
                    errors.append(
                        f"{field_name}.{subkey}: expected {subvalue!r}, got {actual_value.get(subkey)!r}"
                    )
            if field_expectation == {} and actual_value != {}:
                errors.append(f"{field_name}: expected empty dict, got {actual_value!r}")
        elif isinstance(field_expectation, list):
            if actual_value != field_expectation:
                errors.append(f"{field_name}: expected {field_expectation!r}, got {actual_value!r}")
        else:
            if actual_value != field_expectation:
                errors.append(f"{field_name}: expected {field_expectation!r}, got {actual_value!r}")

    return (len(errors) == 0, errors)


def main() -> int:
    load_env_file(ROOT / ".env.bot")
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    base_url = os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").strip()
    user_prefix = os.getenv("DIFY_USER_PREFIX", "line").strip()
    if not api_key:
        print("Missing DIFY_API_KEY", file=sys.stderr)
        return 1

    cases_path = ROOT / "tests" / "golden_set.json"
    cases = load_cases(cases_path)
    client = DifyAgentClient(api_key=api_key, base_url=base_url, user_prefix=user_prefix)

    report_rows: List[Dict[str, Any]] = []
    passed = 0

    for case in cases:
        runtime_context = build_runtime_context()
        result = client.plan(
            memory_key=f"golden-set:{case['id']}",
            user_goal=case["user_input"],
            runtime_context=runtime_context,
        )
        actual = planner_to_dict(result)
        ok, errors = check_expectations(case["expected"], actual)
        if ok:
            passed += 1
        report_rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "user_input": case["user_input"],
                "passed": ok,
                "errors": errors,
                "expected": deepcopy(case["expected"]),
                "actual": actual,
            }
        )

    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"golden_set_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "summary": {
                    "total": len(report_rows),
                    "passed": passed,
                    "failed": len(report_rows) - passed,
                },
                "results": report_rows,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )

    print(
        json.dumps(
            {
                "total": len(report_rows),
                "passed": passed,
                "failed": len(report_rows) - passed,
                "report_path": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed == len(report_rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
