"""프로액티브 루프 — 7단계 캐스케이드 (기획서 기준 재정렬)
수정사항:
1. 캐스케이드 순서: DND 직후 미응답 에스컬레이션
2. 저녁 리뷰 ≠ 취침 루틴 분리
3. 내부 프롬프트에서 명령형 제거 → 상황 설명 + 톤 가이드
4. 컨디션 체크: 아침/저녁 1회씩만"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot
from chiro_bot.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE,
    MORNING_HOUR, EVENING_HOUR, BEDTIME_HOUR
)
from chiro_bot import database as db
from chiro_bot.ai_client import generate_proactive_message
from chiro_bot.patterns import update_patterns

logger = logging.getLogger(__name__)

ESCALATION_INTERVALS = {0: 30, 1: 15, 2: 7, 3: 3}


async def _send_bot_message(text: str):
    """텔레그램 발송. 빈/중복 메시지는 스킵."""
    if not text or not text.strip() or text.strip() == '""':
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    recent = await db.get_recent_messages(3)
    for m in recent:
        if m.get("direction") == "bot" and m["content"].strip() == text.strip():
            logger.debug("중복 메시지 스킵")
            return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    await db.log_message("bot", text)
    await db.update_user_state(last_bot_message_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat())


async def _should_escalate() -> bool:
    """마지막 봇 메시지 이후 유저 응답 없고, 간격이 지났는지"""
    state = await db.get_user_state()
    last_bot = await db.get_last_bot_message_time()
    last_user = await db.get_last_user_message_time()
    if not last_bot:
        return False
    if last_user and last_user > last_bot:
        return False
    level = state.get("escalation_level", 0)
    interval = ESCALATION_INTERVALS.get(level, 30)
    elapsed = (datetime.now() - last_bot).total_seconds() / 60
    return elapsed >= interval


def _parse_deadline(dl_str: str, now: datetime) -> datetime:
    """마감 문자열 → datetime. 실패 시 None."""
    try:
        if len(dl_str) <= 5:
            return datetime.combine(now.date(),
                                    datetime.strptime(dl_str, "%H:%M").time(),
                                    tzinfo=ZoneInfo(TIMEZONE))
        dl = datetime.fromisoformat(dl_str)
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=ZoneInfo(TIMEZONE))
        return dl
    except (ValueError, TypeError):
        return None


async def proactive_check():
    """
    7단계 판단 캐스케이드 (기획서 순서):
    1. 헬스체크
    2. 아침 시작 (+ 컨디션 체크)
    3. 저녁 리뷰 (성과 보고서)
    4. 취침 루틴 (점수 + 내일 + 동기부여)
    5. DND → 조용히 대기
    6. 미응답 에스컬레이션 (DND 직후!)
    7. 마감 알림 + 진행 확인 + 시작 리마인더
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    hour = now.hour
    minute = now.minute
    now_hm = now.strftime("%H:%M")

    # ---- Step 1: 헬스체크 ----
    if hour == MORNING_HOUR - 1 and minute < 15:
        if not await db.is_healthcheck_sent_today():
            await _send_bot_message("정상 가동 중이에요, 신님.")
            await db.log_healthcheck()
            try:
                await db.backup_db()
            except Exception:
                pass
            return

    # ---- Step 2: 아침 시작 (+ 컨디션 1회차) ----
    if hour == MORNING_HOUR and minute < 20:
        await db.load_dnd_defaults_for_today()
        tasks = await db.get_today_tasks()
        if not tasks:
            msg = await generate_proactive_message(
                "아침 시작 시간이에요. 신님에게 좋은 아침 인사를 하고, "
                "오늘 뭐 할 건지 물어봐주세요. 컨디션도 가볍게 여쭤보세요.",
            )
        else:
            pending = [t for t in tasks if t["status"] == "pending"]
            msg = await generate_proactive_message(
                f"아침이에요. 이미 {len(pending)}개 태스크가 등록돼 있어요. "
                "신님에게 인사하고, 오늘 컨디션이 어떤지 가볍게 여쭤보세요. "
                "추가할 일이 있는지도 물어봐주세요.",
                tasks=tasks
            )
        await _send_bot_message(msg)
        return

    # ---- Step 3: 저녁 리뷰 (성과 보고서) ----
    if hour == EVENING_HOUR and minute < 20:
        await _evening_review()
        return

    # ---- Step 4: 취침 루틴 (점수 + 내일 + 잠자리) ----
    if hour == BEDTIME_HOUR and minute < 20:
        await _bedtime_routine()
        return

    # ---- Step 5: DND ----
    if await db.is_dnd_now():
        logger.debug(f"DND ({now_hm})")
        return

    # 활동 시간 외
    if hour < MORNING_HOUR or hour > BEDTIME_HOUR:
        return

    # ---- Step 6: 미응답 에스컬레이션 (DND 직후!) ----
    if await _should_escalate():
        state = await db.get_user_state()
        level = state.get("escalation_level", 0)
        msg = await generate_proactive_message(
            f"미응답 상태예요. 에스컬레이션 레벨: {level}. "
            f"레벨에 맞는 톤으로 신님에게 응답을 부탁하세요. "
            f"레벨 0~1은 부드럽게, 2는 직접적으로, 3은 단호하게 (하지만 존댓말).",
        )
        if msg:
            await _send_bot_message(msg)
            await db.bump_no_response()
        return

    # ---- 쿨다운 (마감 긴급 제외) ----
    last_bot = await db.get_last_bot_message_time()
    if last_bot:
        elapsed = (datetime.now() - last_bot).total_seconds() / 60
        if elapsed < 15:
            # 마감 15분 이내만 예외
            has_urgent = False
            for t in (await db.get_today_tasks()):
                if t.get("deadline") and t["status"] in ("pending", "in_progress"):
                    dl = _parse_deadline(t["deadline"], now)
                    if dl and 0 < (dl - now).total_seconds() / 60 <= 15:
                        has_urgent = True
                        break
            if not has_urgent:
                return

    # ---- Step 7: 태스크 관련 알림 ----
    tasks = await db.get_today_tasks()
    plan = await db.get_today_plan()

    # 7a. 마감 임박 알림 (플랜 없이도 작동)
    for task in tasks:
        if task.get("deadline") and task["status"] in ("pending", "in_progress"):
            dl = _parse_deadline(task["deadline"], now)
            if not dl:
                continue
            minutes_until = (dl - now).total_seconds() / 60

            if minutes_until <= 0:
                msg = await generate_proactive_message(
                    f"'{task['title']}' 마감이 지났어요. "
                    "신님에게 이 상황을 알려주세요. 수치 기반으로, 명령하지 말고.",
                    tasks=tasks
                )
                await _send_bot_message(msg)
                return
            elif minutes_until <= 15 and task["status"] == "pending":
                await db.update_task_status(task["id"], "in_progress")
                msg = await generate_proactive_message(
                    f"'{task['title']}' {int(minutes_until)}분 남았어요. "
                    "신님에게 준비 상태를 여쭤보세요.",
                    tasks=tasks
                )
                await _send_bot_message(msg)
                return
            elif minutes_until <= 30 and task["status"] == "pending":
                msg = await generate_proactive_message(
                    f"'{task['title']}' 30분 정도 남았어요. "
                    "신님에게 미리 알려주세요.",
                    tasks=tasks
                )
                await _send_bot_message(msg)
                return

            # 마감 소요시간 압박
            est = task.get("estimated_minutes") or 60
            if task["status"] == "in_progress" and minutes_until <= est:
                msg = await generate_proactive_message(
                    f"'{task['title']}' 마감까지 {int(minutes_until)}분, 예상 소요 {est}분. "
                    "시간이 빠듯한 상황이에요. 수치와 함께 신님에게 알려주세요.",
                    tasks=tasks
                )
                await _send_bot_message(msg)
                return

    # 7b. 진행 중 태스크 확인
    active = [t for t in tasks if t["status"] == "in_progress"]
    if active:
        task = active[0]
        if task["postpone_count"] >= 3:
            msg = await generate_proactive_message(
                f"'{task['title']}' {task['postpone_count']}번 미뤄진 태스크예요. "
                f"마감: {task.get('deadline', '없음')}. "
                "신님에게 상황을 구체적 수치로 알려주세요. 명령이 아닌 사실 전달로.",
                tasks=tasks
            )
            await _send_bot_message(msg)
            return
        if task["fail_count"] >= 3:
            msg = await generate_proactive_message(
                f"'{task['title']}' {task['fail_count']}번 어려워하신 태스크예요. "
                "태스크를 더 작게 쪼개볼지 신님에게 제안해보세요.",
                tasks=tasks
            )
            await _send_bot_message(msg)
            return

    # 7c. 플랜 기반 시작 리마인더
    for slot in plan:
        if slot.get("task_status") == "pending" and slot["start_time"] <= now_hm:
            task = next((t for t in tasks if t["id"] == slot["task_id"]), None)
            if task:
                await db.update_task_status(task["id"], "in_progress")
                msg = await generate_proactive_message(
                    f"'{task['title']}' 시작 시간({slot['start_time']})이에요. "
                    "신님에게 시작할 수 있는지 여쭤보세요.",
                    tasks=tasks
                )
                await _send_bot_message(msg)
                return

    # 아무것도 해당 안 됨
    logger.debug("보낼 이유 없음")


# ============================================================
# 저녁 리뷰 (EVENING_HOUR) — 성과 보고서
# ============================================================

async def _evening_review():
    """저녁 리뷰: 오늘 성과 보고서. 취침 루틴과 분리."""
    stats = await db.get_task_stats_today()
    conditions = await db.get_today_conditions()

    data = {
        "통계": {
            "전체": stats["total"], "완료": stats["done"],
            "미완료": stats["pending"], "연기": stats["deferred"],
            "실패": stats["failed"],
        },
        "태스크": [
            {"제목": t["title"], "상태": t["status"], "미룸": t["postpone_count"]}
            for t in stats["tasks"]
        ],
        "컨디션": [
            {"시각": c["time"], "에너지": c.get("energy_level"), "기분": c.get("mood")}
            for c in conditions
        ],
    }

    msg = await generate_proactive_message(
        f"저녁 리뷰 시간이에요. 아래 데이터를 보고 신님에게 오늘 성과를 정리해주세요.\n"
        f"형식과 톤은 자유롭게 판단해주세요. 수치 기반 피드백, 빈 칭찬 금지.\n"
        f"컨디션 데이터가 있으면 참고해서 코멘트해주세요.\n\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}",
        tasks=stats["tasks"]
    )
    await _send_bot_message(msg)
    await db.save_daily_report(stats["total"], stats["done"], msg or "")


# ============================================================
# 취침 루틴 (BEDTIME_HOUR) — 점수 + 내일 + 잠자리
# ============================================================

async def _bedtime_routine():
    """취침 전: 하루 점수, 내일 준비, 동기부여, 잘 자라. 아카이브+패턴+백업도 여기서."""
    stats = await db.get_task_stats_today()
    conditions = await db.get_today_conditions()
    all_tasks = await db.get_today_tasks()

    tomorrow = (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_tasks = [t for t in all_tasks if t.get("deadline", "").startswith(tomorrow)]

    data = {
        "오늘_통계": {
            "전체": stats["total"], "완료": stats["done"],
            "미완료": stats["pending"], "연기": stats["deferred"],
        },
        "오늘_컨디션": [
            {"시각": c["time"], "에너지": c.get("energy_level"), "기분": c.get("mood")}
            for c in conditions
        ],
        "내일_태스크": [
            {"제목": t["title"], "마감": t.get("deadline")} for t in tomorrow_tasks
        ],
    }

    msg = await generate_proactive_message(
        f"취침 전이에요. 아래 데이터를 보고 신님에게 하루 마무리 메시지를 보내주세요.\n"
        f"반드시 포함: 오늘 10점 만점 점수, 내일 할 일과 준비물, 동기부여 한마디, 잘 자라는 인사.\n"
        f"형식과 길이는 자유롭게.\n\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}",
        tasks=stats["tasks"]
    )
    await _send_bot_message(msg)

    # 하루 마감 처리
    await db.archive_today()
    try:
        await update_patterns()
    except Exception as e:
        logger.error(f"패턴 업데이트 실패: {e}")
    try:
        await db.backup_db()
    except Exception as e:
        logger.error(f"DB 백업 실패: {e}")
