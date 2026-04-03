from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ConversationMode(str, Enum):
    IDLE = "idle"
    COLLECTING = "collecting"
    PLANNING = "planning"
    TRACKING = "tracking"
    REVIEWING = "reviewing"
    CHAT = "chat"


class MessagePurpose(str, Enum):
    UNKNOWN = "unknown"
    SYSTEM_HEALTHCHECK = "system_healthcheck"
    MORNING_BRIEFING = "morning_briefing"
    EVENING_REVIEW = "evening_review"
    BEDTIME_NOTICE = "bedtime_notice"
    PLAN_REQUEST = "plan_request"
    PLAN_CONFIRMATION = "plan_confirmation"
    TASK_PROMPT = "task_prompt"
    PROGRESS_PROMPT = "progress_prompt"
    REMINDER_NOTICE = "reminder_notice"
    DEADLINE_WARNING = "deadline_warning"
    EMOTIONAL_REPAIR = "emotional_repair"
    SMALL_TALK = "small_talk"


@dataclass
class BotPersona:
    name: str
    tone: str
    honorific: str
    max_lines: int
    allow_pressure: bool
    forbid_empty_cheerleading: bool
    forbid_command_tone: bool


@dataclass
class DataRetentionPolicy:
    daily_operational_days: int = 3
    conversation_log_days: int = 3
    keep_reports_forever: bool = True
    keep_patterns_forever: bool = True


@dataclass
class ConversationState:
    mode: ConversationMode = ConversationMode.IDLE
    waiting_for: str | None = None
    subject_type: str | None = None
    subject_ref: str | None = None
    last_bot_prompt_purpose: MessagePurpose = MessagePurpose.UNKNOWN
    last_user_intent: str | None = None
    expires_at: datetime | None = None


@dataclass
class ProactiveState:
    escalation_level: int = 0
    last_prompt_requiring_response_at: datetime | None = None
    last_prompt_purpose: MessagePurpose = MessagePurpose.UNKNOWN
    last_user_response_at: datetime | None = None
    last_morning_at: datetime | None = None
    last_evening_review_at: datetime | None = None
    last_bedtime_at: datetime | None = None
    healthcheck_sent_at: datetime | None = None


@dataclass
class DailyRuntimeSnapshot:
    now: datetime
    routines: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    dnd_slots: list[dict] = field(default_factory=list)
    reminders: list[dict] = field(default_factory=list)
    recent_messages: list[dict] = field(default_factory=list)
    conversation_state: ConversationState = field(default_factory=ConversationState)
    proactive_state: ProactiveState = field(default_factory=ProactiveState)
