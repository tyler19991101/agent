from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class InboundMessage:
    source_type: str
    source_id: str
    user_id: Optional[str]
    reply_token: str
    text: str
    quoted_message_id: Optional[str]
    received_at: datetime
    line_event_id: Optional[str]

    @property
    def memory_key(self) -> str:
        if self.user_id:
            return f"user:{self.user_id}"
        return f"target:{self.source_id}"


@dataclass
class PlannerResult:
    task_type: str = "information_request"
    goal_summary: str = ""
    subtasks: List[str] = field(default_factory=list)
    needed_inputs: List[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_type: str = "decision"
    approval_prompt: str = ""
    draft_user_reply: str = ""
    final_reply: str = ""
    options: List[Dict[str, Any]] = field(default_factory=list)
    recommendation: str = ""
    rationale: List[str] = field(default_factory=list)
    action_links: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_info: List[str] = field(default_factory=list)
    profile_updates: Dict[str, Any] = field(default_factory=dict)
    raw_answer: str = ""


@dataclass
class TaskRun:
    id: int
    memory_key: str
    task_type: str
    status: str
    user_goal: str
    normalized_goal: str
    requires_approval: bool
    current_phase: str
    error: Optional[str] = None
