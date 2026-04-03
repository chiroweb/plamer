"""proactive.py — 7-stage cascade (REBUILD_GUIDE section 5).

AI calls: ZERO except generate_affirmation() for morning greeting.
All user-facing messages come from templates.py functions.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

from chiro_bot.architecture.policy import (
    classify_message_purpose,
    is_escalation_eligible_message,
    should_reset_escalation_for_new_day,
)
from chiro_bot.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    MORNING_HOUR,
    EVENING_HOUR,
    BEDTIME_HOUR,
)
from chiro_bot import database as db
from chiro_bot import templates
from chiro_bot.ai_client import generate_affirmation, generate_evening_comment
from chiro_bot.patterns import update_patterns
from chiro_bot.planner import generate_plan

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Escalation intervals (section 8)
# ------------------------------------------------------------------
ESCALATION_INTERVALS: dict[int, int] = {0: 30, 1: 20, 2: 10, 3: 5}

# Cooldown between bot messages (minutes). Urgent deadline exempted.
COOLDOWN_MINUTES = 15

# ------------------------------------------------------------------
# Daily-once flags  (keyed by date string, e.g. "2026-03-31")
# ------------------------------------------------------------------
_daily_flags: dict[str, dict[str, bool]] = {}


def _flag(key: str, now: datetime) -> bool:
    """Return True if *key* was already marked done today."""
    today = now.strftime("%Y-%m-%d")
    return _daily_flags.get(today, {}).get(key, False)


def _mark(key: str, now: datetime) -> None:
    today = now.strftime("%Y-%m-%d")
    _daily_flags.setdefault(today, {})[key] = True


# ------------------------------------------------------------------
# Telegram sender (duplicate-safe)
# ------------------------------------------------------------------
async def _send(text: str) -> None:
    """Send *text* via Telegram. Skip empty / duplicate messages."""
    if not text or not text.strip() or text.strip() == '""':
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    # Duplicate guard: skip if same text was just sent recently
    recent = await db.get_recent_messages(3)
    for m in recent:
        if m.get("direction") == "bot" and m["content"].strip() == text.strip():
            logger.debug("중복 메시지 스킵")
            return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    await db.log_message("bot", text)
    sent_at = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    purpose = classify_message_purpose(text)
    await db.update_user_state(
        last_bot_message_at=sent_at,
    )
    updates = {}
    if is_escalation_eligible_message(text):
        updates["last_prompt_requiring_response_at"] = sent_at
        updates["last_prompt_purpose"] = purpose.value
    if purpose.value == "morning_briefing":
        updates["last_morning_at"] = sent_at
    elif purpose.value == "evening_review":
        updates["last_evening_review_at"] = sent_at
    elif purpose.value == "bedtime_notice":
        updates["last_bedtime_at"] = sent_at
    elif purpose.value == "system_healthcheck":
        updates["healthcheck_sent_at"] = sent_at
    if updates:
        await db.update_proactive_state(**updates)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _parse_deadline(dl_str: str | None, now: datetime) -> datetime | None:
    """Parse a deadline string into a timezone-aware datetime, or None."""
    if not dl_str:
        return None
    try:
        # "HH:MM" — treat as today
        if len(dl_str) <= 5:
            return datetime.combine(
                now.date(),
                datetime.strptime(dl_str, "%H:%M").time(),
                tzinfo=ZoneInfo(TIMEZONE),
            )
        dl = datetime.fromisoformat(dl_str)
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=ZoneInfo(TIMEZONE))
        return dl
    except (ValueError, TypeError):
        return None


async def _should_escalate(now: datetime) -> bool:
    """True when the user hasn't replied and the escalation interval has elapsed."""
    state = await db.get_proactive_state()
    last_prompt = state.last_prompt_requiring_response_at
    if not last_prompt or not state.last_prompt_purpose:
        return False

    if last_prompt.tzinfo is None:
        last_prompt = last_prompt.replace(tzinfo=ZoneInfo(TIMEZONE))

    last_user = await db.get_last_user_message_time()
    if last_user:
        if last_user.tzinfo is None:
            last_user = last_user.replace(tzinfo=ZoneInfo(TIMEZONE))
        if last_user > last_prompt:
            return False

    if should_reset_escalation_for_new_day(last_prompt, now):
        await db.reset_no_response()
        return False

    level = state.escalation_level
    interval = ESCALATION_INTERVALS.get(level, ESCALATION_INTERVALS[3])
    elapsed = (now - last_prompt).total_seconds() / 60
    return elapsed >= interval


async def _minutes_since_last_bot_message(now: datetime) -> float | None:
    last_bot = await db.get_last_bot_message_time()
    if not last_bot:
        return None
    # timezone 맞추기
    if last_bot.tzinfo is None:
        last_bot = last_bot.replace(tzinfo=ZoneInfo(TIMEZONE))
    return (now - last_bot).total_seconds() / 60


async def _cooldown_ok(now: datetime) -> bool:
    """Return True if enough time has passed since the last bot message."""
    elapsed = await _minutes_since_last_bot_message(now)
    if elapsed is None:
        return True
    return elapsed >= COOLDOWN_MINUTES


async def _has_urgent_deadline(now: datetime) -> bool:
    """Return True when any task has a deadline within COOLDOWN_MINUTES."""
    tasks = await db.get_today_tasks()
    for t in tasks:
        if t["status"] not in ("pending", "in_progress"):
            continue
        dl = _parse_deadline(t.get("deadline"), now)
        if dl is None:
            continue
        remaining = (dl - now).total_seconds() / 60
        if 0 < remaining <= COOLDOWN_MINUTES:
            return True
    return False


# ------------------------------------------------------------------
# Evening review data builder
# ------------------------------------------------------------------
STATUS_LABELS: dict[str, str] = {
    "done": "완료",
    "failed": "실패",
    "partial": "부분완료",
    "deferred": "연기",
    "pending": "미완료",
    "in_progress": "진행중",
}


async def _build_evening_data() -> tuple[list[dict], int, str | None]:
    """Return (tasks_summary, completion_rate, pattern_comment)."""
    stats = await db.get_task_stats_today()
    tasks_summary: list[dict] = []
    for t in stats["tasks"]:
        extra_parts: list[str] = []
        if t["postpone_count"]:
            extra_parts.append(f"미룸 {t['postpone_count']}회")
        if t["fail_count"]:
            extra_parts.append(f"실패 {t['fail_count']}회")
        tasks_summary.append({
            "title": t["title"],
            "status": t["status"],
            "status_label": STATUS_LABELS.get(t["status"], t["status"]),
            "extra": ", ".join(extra_parts) if extra_parts else None,
        })
    total = stats["total"]
    completion_rate = int(stats["done"] / total * 100) if total else 0

    # Generate a short pattern-based comment via AI (only allowed AI call besides affirmation)
    pattern_comment: str | None = None
    try:
        import json
        comment = await generate_evening_comment(json.dumps({
            "total": total,
            "done": stats["done"],
            "failed": stats["failed"],
            "deferred": stats["deferred"],
            "postpones": stats["total_postpones"],
        }, ensure_ascii=False))
        if comment and comment.strip():
            pattern_comment = comment.strip()
    except Exception as e:
        logger.warning(f"저녁 코멘트 생성 실패: {e}")

    return tasks_summary, completion_rate, pattern_comment


async def _ensure_morning_plan() -> list[dict]:
    plan = await db.get_today_plan()
    if plan:
        return plan

    tasks = await db.get_today_tasks()
    plannable = [t for t in tasks if t["status"] in ("pending", "deferred")]
    if not plannable:
        return []

    dnd = await db.get_today_dnd()
    routines = await db.get_today_routines()
    plan_slots = await generate_plan(tasks, dnd, routines)
    if not plan_slots:
        return []

    await db.set_daily_plan(plan_slots)
    return await db.get_today_plan()


async def _build_morning_briefing() -> tuple[list[str], bool]:
    tasks = await db.get_today_tasks()
    routines = await db.get_today_routines()
    plan = await _ensure_morning_plan()
    conditions = await db.get_today_conditions()

    lines: list[str] = []

    if plan:
        lines.append("오늘 일정:")
        for slot in plan[:3]:
            lines.append(f"{slot['start_time']}~{slot['end_time']} {slot['title']}")
        if len(plan) > 3:
            lines.append(f"외 {len(plan) - 3}개 더 있어요.")
    elif tasks:
        pending = [t for t in tasks if t["status"] in ("pending", "deferred", "in_progress")]
        if pending:
            summary = ", ".join(t["title"] for t in pending[:3])
            if len(pending) > 3:
                summary += f" 외 {len(pending) - 3}개"
            lines.append(f"오늘 할 일: {summary}")

    if routines:
        routine_summary = ", ".join(
            f"{routine['start_time']}~{routine['end_time']} {routine['label']}"
            for routine in routines[:2]
        )
        if len(routines) > 2:
            routine_summary += f" 외 {len(routines) - 2}개"
        lines.append(f"오늘 루틴: {routine_summary}")

    return lines, not bool(conditions)


# ------------------------------------------------------------------
# Main cascade
# ------------------------------------------------------------------
async def proactive_check() -> None:  # noqa: C901
    """7-stage proactive cascade. Called periodically by the scheduler."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    hour = now.hour
    minute = now.minute
    now_hm = now.strftime("%H:%M")

    # ================================================================
    # Step 1: Healthcheck  (MORNING_HOUR - 1, once daily)
    # ================================================================
    if hour == MORNING_HOUR - 1 and minute < 15:
        if not await db.is_healthcheck_sent_today():
            await db.reset_no_response()
            await _send(templates.healthcheck())
            await db.log_healthcheck()
            try:
                await db.backup_db()
            except Exception:
                pass
            return

    # ================================================================
    # Step 1.5: Reminder check  (reminders table, time <= now, sent=0)
    # ================================================================
    reminders = await db.get_pending_reminders()
    for r in reminders:
        if r["time"] <= now_hm:
            await _send(r["message"])
            await db.mark_reminder_sent(r["id"])
            return

    # ================================================================
    # Step 2: Morning greeting  (MORNING_HOUR, once daily)
    # ================================================================
    if hour == MORNING_HOUR and minute < 20 and not _flag("morning", now):
        await db.load_dnd_defaults_for_today()
        await db.reset_no_response()
        affirmation = await generate_affirmation()  # sole AI call
        briefing_lines, ask_condition = await _build_morning_briefing()
        await _send(templates.morning_greeting(affirmation, briefing_lines, ask_condition))
        _mark("morning", now)
        return

    # ================================================================
    # Step 3: Evening review  (EVENING_HOUR, once daily)
    # ================================================================
    if hour == EVENING_HOUR and minute < 20 and not _flag("evening", now):
        tasks_summary, completion_rate, pattern_comment = await _build_evening_data()
        await db.reset_no_response()
        await _send(templates.evening_review(tasks_summary, completion_rate, pattern_comment))
        await db.archive_today()
        _mark("evening", now)
        return

    # ================================================================
    # Step 3.5: Bedtime  (BEDTIME_HOUR, once daily)
    # ================================================================
    if hour == BEDTIME_HOUR and minute < 20 and not _flag("bedtime", now):
        await db.reset_no_response()
        await _send(templates.bedtime())
        try:
            await update_patterns()
        except Exception as e:
            logger.error(f"패턴 업데이트 실패: {e}")
        try:
            await db.backup_db()
        except Exception as e:
            logger.error(f"DB 백업 실패: {e}")
        _mark("bedtime", now)
        return

    # ================================================================
    # Step 4: DND check  → return silently
    # ================================================================
    if await db.is_dnd_now():
        logger.debug(f"DND ({now_hm})")
        return

    # Outside active hours → silent
    if hour < MORNING_HOUR or hour > BEDTIME_HOUR:
        return

    # ================================================================
    # Step 5: Escalation  (DND 직후 — important)
    # ================================================================
    if await _should_escalate(now):
        state = await db.get_proactive_state()
        user_state = await db.get_user_state()
        level = state.escalation_level
        attempt_count = (user_state.get("no_response_count") or 0) + 1

        # Build optional task context string
        task_context: str | None = None
        tasks = await db.get_today_tasks()
        active = [t for t in tasks if t["status"] == "in_progress"]
        if active:
            task_context = active[0]["title"]

        await _send(templates.escalation_message(level, task_context, attempt_count))
        await db.bump_no_response()
        return

    # ================================================================
    # Cooldown gate (urgent deadline exempted)
    # ================================================================
    if not await _cooldown_ok(now) and not await _has_urgent_deadline(now):
        return

    # ================================================================
    # Step 6: Deadline urgent check
    # ================================================================
    tasks = await db.get_today_tasks()
    for task in tasks:
        if task["status"] not in ("pending", "in_progress"):
            continue
        dl = _parse_deadline(task.get("deadline"), now)
        if dl is None:
            continue
        remaining_min = int((dl - now).total_seconds() / 60)
        estimated = task.get("estimated_minutes") or 60

        # Fire when remaining time is tight relative to estimated work
        if remaining_min <= estimated:
            await _send(templates.deadline_urgent(task["title"], remaining_min, estimated))
            return

    # ================================================================
    # Step 7: Start reminder (plan-based)
    # ================================================================
    plan = await db.get_today_plan()
    for slot in plan:
        if slot.get("task_status") == "pending" and slot["start_time"] <= now_hm:
            task = next((t for t in tasks if t["id"] == slot["task_id"]), None)
            if task:
                await _send(templates.start_reminder(task["title"], slot["start_time"]))
                return

    # ================================================================
    # Step 7.5: Progress check (active task, cooldown respected)
    # ================================================================
    active = [t for t in tasks if t["status"] == "in_progress"]
    if active and await _cooldown_ok(now):
        await _send(templates.progress_check())
        return

    # ================================================================
    # No match → silent
    # ================================================================
    logger.debug("보낼 이유 없음")
