import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from secretary_agent.dify_client import DifyAgentClient


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "我要到泰國旅遊，預算四萬，四天三夜"
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    base_url = os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").strip()
    user_prefix = os.getenv("DIFY_USER_PREFIX", "line").strip()

    if not api_key:
        print("Missing DIFY_API_KEY")
        return 1

    client = DifyAgentClient(api_key=api_key, base_url=base_url, user_prefix=user_prefix)
    result = client.plan(
        memory_key="smoke-test",
        user_goal=query,
        runtime_context={
            "history": "",
            "profile": {},
            "artifacts": [],
            "latest_approval_response": "",
        },
    )
    print(json.dumps(
        {
            "task_type": result.task_type,
            "goal_summary": result.goal_summary,
            "requires_approval": result.requires_approval,
            "approval_type": result.approval_type,
            "approval_prompt": result.approval_prompt,
            "final_reply": result.final_reply,
            "needed_inputs": result.needed_inputs,
            "action_links": result.action_links,
            "warnings": result.warnings,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
