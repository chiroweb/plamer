"""Layered runtime architecture primitives for CHIRO Bot."""

from .models import (
    BotPersona,
    ConversationMode,
    ConversationState,
    DailyRuntimeSnapshot,
    DataRetentionPolicy,
    MessagePurpose,
    ProactiveState,
)
from .policy import (
    DEFAULT_PERSONA,
    DEFAULT_RETENTION_POLICY,
    classify_message_purpose,
    is_escalation_eligible_message,
    should_reset_escalation_for_new_day,
)

__all__ = [
    "BotPersona",
    "ConversationMode",
    "ConversationState",
    "DailyRuntimeSnapshot",
    "DataRetentionPolicy",
    "MessagePurpose",
    "ProactiveState",
    "DEFAULT_PERSONA",
    "DEFAULT_RETENTION_POLICY",
    "classify_message_purpose",
    "is_escalation_eligible_message",
    "should_reset_escalation_for_new_day",
]
