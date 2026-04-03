from __future__ import annotations

from datetime import datetime

from .models import DataRetentionPolicy, MessagePurpose
from .persona import DEFAULT_PERSONA


DEFAULT_RETENTION_POLICY = DataRetentionPolicy()

MESSAGE_PREFIX_TO_PURPOSE: list[tuple[str, MessagePurpose]] = [
    ("💚 정상 가동 중.", MessagePurpose.SYSTEM_HEALTHCHECK),
    ("좋은 아침이에요.", MessagePurpose.MORNING_BRIEFING),
    ("오늘 리뷰예요.", MessagePurpose.EVENING_REVIEW),
    ("잘 시간이에요.", MessagePurpose.BEDTIME_NOTICE),
    ("혹시 메시지 못 보셨나요?", MessagePurpose.PLAN_REQUEST),
    ("아직 답이 없네요.", MessagePurpose.PLAN_REQUEST),
    ("아직 답이 없어요.", MessagePurpose.PLAN_REQUEST),
    ("솔직히 말할게요. 지금 4번째 알림이에요.", MessagePurpose.PLAN_REQUEST),
    ("플랜 확정했어요.", MessagePurpose.PLAN_CONFIRMATION),
    ("지금 어디까지 했어요?", MessagePurpose.PROGRESS_PROMPT),
    ("방해금지 시간 끝났어요.", MessagePurpose.REMINDER_NOTICE),
    ("제가 잔소리처럼 느껴질 수 있어요.", MessagePurpose.EMOTIONAL_REPAIR),
    ("알겠어요. 오늘은 플랜 없이 갈게요.", MessagePurpose.EMOTIONAL_REPAIR),
]

ESCALATION_ELIGIBLE_PURPOSES = {
    MessagePurpose.PLAN_REQUEST,
    MessagePurpose.TASK_PROMPT,
    MessagePurpose.PROGRESS_PROMPT,
    MessagePurpose.PLAN_CONFIRMATION,
}


def classify_message_purpose(text: str) -> MessagePurpose:
    normalized = (text or "").strip()
    if not normalized:
        return MessagePurpose.UNKNOWN

    for prefix, purpose in MESSAGE_PREFIX_TO_PURPOSE:
        if normalized.startswith(prefix):
            return purpose

    if "시작할 수 있어요?" in normalized:
        return MessagePurpose.TASK_PROMPT
    if "언제까지" in normalized or "얼마나 걸려요?" in normalized:
        return MessagePurpose.TASK_PROMPT
    if "마감까지" in normalized:
        return MessagePurpose.DEADLINE_WARNING
    if "알림" in normalized:
        return MessagePurpose.REMINDER_NOTICE
    return MessagePurpose.SMALL_TALK


def is_escalation_eligible_message(text: str) -> bool:
    purpose = classify_message_purpose(text)
    return purpose in ESCALATION_ELIGIBLE_PURPOSES


def should_reset_escalation_for_new_day(
    last_prompt_at: datetime | None,
    now: datetime,
) -> bool:
    if last_prompt_at is None:
        return False
    return last_prompt_at.date() != now.date()
