"""프로액티브 루프 — APScheduler로 주기적 트리거, 봇이 먼저 메시지 발송
Phase 2: 7단계 캐스케이드 완전 구현 + 에스컬레이션 + 저녁 보고서 + 헬스체크"""

from __future__ import annotations

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

# 에스컬레이션 레벨별 재촉 간격 (분)
ESCALATION_INTERVALS = {
    0: 30,   # 정상 — 30분 후 재촉
    1: 15,   # 부드러운 재촉 — 15분 후
    2: 7,    # 강한 재촉 — 7분 후
    3: 3,    # 압박 — 3분 후
}


async def _send_bot_message(text: str):
    """텔레그램으로 직접 메시지 발송 (프로액티브용). 빈 메시지는 무시."""
    if not text or not text.strip() or text.strip() == '""':
        logger.debug("빈 메시지 — 발송 스킵")
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("봇 토큰 또는 채팅 ID 미설정 — 메시지 발송 스킵")
        return

    # 마지막 봇 메시지와 너무 비슷하면 스킵 (반복 방지)
    recent = await db.get_recent_messages(3)
    for m in recent:
        if m.get("direction") == "bot" and m["content"].strip() == text.strip():
            logger.debug("중복 메시지 — 발송 스킵")
            return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    await db.log_message("bot", text)
    await db.update_user_state(last_bot_message_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat())


async def _should_escalate() -> bool:
    """마지막 봇 메시지 이후 유저 응답이 없고, 에스컬레이션 간격이 지났는지 확인"""
    state = await db.get_user_state()
    last_bot = await db.get_last_bot_message_time()
    last_user = await db.get_last_user_message_time()

    if not last_bot:
        return False

    # 유저가 마지막 봇 메시지 이후 응답했으면 에스컬레이션 불필요
    if last_user and last_user > last_bot:
        return False

    # 에스컬레이션 간격 체크
    level = state.get("escalation_level", 0)
    interval = ESCALATION_INTERVALS.get(level, 30)
    elapsed = (datetime.now() - last_bot).total_seconds() / 60

    return elapsed >= interval


async def proactive_check():
    """
    프로액티브 루프 메인 함수 — APScheduler가 주기적으로 호출.
    7단계 판단 캐스케이드 (우선순위 순서):
    1. 헬스체크 시간인가? → 헬스체크 발송
    2. 아침 시작 시간인가? → 아침 인사 + DND 기본값 로드 + 태스크 수집 유도
    3. 저녁 마감 시간인가? → 저녁 보고서 + 일간 아카이브 + DB 백업
    4. DND 시간대인가? → 조용히 대기
    5. 현재 진행 중인 태스크가 있는가? → 진행 확인 + 마감 압박
    6. 시작해야 할 태스크가 있는가? → 시작 리마인더
    7. 미응답 에스컬레이션 / 할 것 없음 → 재촉 or 대기
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    hour = now.hour
    minute = now.minute
    now_hm = now.strftime("%H:%M")

    # ============================================================
    # Step 1: 헬스체크 (매일 07:00에 1회)
    # ============================================================
    if hour == MORNING_HOUR - 1 and minute < 15:
        if not await db.is_healthcheck_sent_today():
            await _send_bot_message("💚 치로봇 정상 가동 중!")
            await db.log_healthcheck()
            # DB 백업도 아침에
            try:
                await db.backup_db()
                logger.info("DB 백업 완료")
            except Exception as e:
                logger.error(f"DB 백업 실패: {e}")
            return

    # ============================================================
    # Step 2: 아침 시작
    # ============================================================
    if hour == MORNING_HOUR and minute < 20:
        # DND 기본값 로드
        await db.load_dnd_defaults_for_today()

        tasks = await db.get_today_tasks()
        if not tasks:
            msg = await generate_proactive_message(
                "아침 시작 시간. 오늘의 태스크를 수집해야 함. "
                "좋은 아침 인사 + 오늘 뭐 해야 하는지 물어봐."
            )
        else:
            # 이미 태스크가 있으면 (전날 미리 등록 등)
            pending = [t for t in tasks if t["status"] == "pending"]
            msg = await generate_proactive_message(
                f"아침 시작. 이미 {len(pending)}개 태스크 등록됨. "
                "플랜을 짜자고 제안하거나, 추가할 게 있는지 물어봐.",
                tasks=tasks
            )
        await _send_bot_message(msg)
        return

    # ============================================================
    # Step 3: 취침 전 루틴
    # ============================================================
    if hour == BEDTIME_HOUR and minute < 20:
        await _bedtime_routine()
        return

    # ============================================================
    # Step 4: DND 체크
    # ============================================================
    if await db.is_dnd_now():
        logger.debug(f"DND 시간대 ({now_hm}) — 대기")
        return

    # 활동 시간 외면 대기
    if hour < MORNING_HOUR or hour >= EVENING_HOUR:
        return

    # ============================================================
    # Step 5: 진행 중 태스크 확인 + 마감 압박
    # ============================================================
    tasks = await db.get_today_tasks()
    plan = await db.get_today_plan()
    recent = await db.get_recent_messages(10)
    active_tasks = [t for t in tasks if t["status"] == "in_progress"]

    if active_tasks:
        task = active_tasks[0]

        # --- 마감 기반 압박 ---
        if task.get("deadline"):
            try:
                dl_str = task["deadline"]
                # "YYYY-MM-DD HH:MM" 또는 "HH:MM" 처리
                if len(dl_str) <= 5:
                    dl = datetime.combine(now.date(),
                                          datetime.strptime(dl_str, "%H:%M").time(),
                                          tzinfo=ZoneInfo(TIMEZONE))
                else:
                    dl = datetime.fromisoformat(dl_str)
                    if dl.tzinfo is None:
                        dl = dl.replace(tzinfo=ZoneInfo(TIMEZONE))

                remaining_min = (dl - now).total_seconds() / 60
                est = task.get("estimated_minutes") or 60

                if remaining_min <= 0:
                    msg = await generate_proactive_message(
                        f"🚨 마감 초과! '{task['title']}' 마감이 이미 지났어! "
                        f"지금이라도 바로 끝내야 해.",
                        tasks=tasks, plan=plan
                    )
                    await _send_bot_message(msg)
                    return
                elif remaining_min <= est:
                    msg = await generate_proactive_message(
                        f"⚠️ 긴급! '{task['title']}' 마감까지 {int(remaining_min)}분, "
                        f"예상 소요 {est}분. 남은 시간이 소요시간 이하야. 지금 바로 끝내.",
                        tasks=tasks, plan=plan
                    )
                    await _send_bot_message(msg)
                    return
                elif remaining_min <= est * 1.5:
                    msg = await generate_proactive_message(
                        f"'{task['title']}' 마감까지 {int(remaining_min)}분 남았어. "
                        f"집중해서 끝내자!",
                        tasks=tasks, plan=plan
                    )
                    await _send_bot_message(msg)
                    return
            except (ValueError, TypeError) as e:
                logger.debug(f"마감 파싱 실패: {e}")

        # --- 반복 실패/미룸 감지 ---
        if task["postpone_count"] >= 3:
            msg = await generate_proactive_message(
                f"'{task['title']}' {task['postpone_count']}번 미뤘어. "
                f"마감: {task.get('deadline', '없음')}. "
                f"더 이상 미루면 안 돼. 구체적 수치로 압박해.",
                tasks=tasks, plan=plan
            )
            await _send_bot_message(msg)
            return

        if task["fail_count"] >= 3:
            msg = await generate_proactive_message(
                f"'{task['title']}' {task['fail_count']}번 못하겠다고 했어. "
                f"태스크가 너무 큰 건 아닌지 물어보고, 작게 쪼개는 걸 제안해.",
                tasks=tasks, plan=plan
            )
            await _send_bot_message(msg)
            return

        # --- 일반 진행 확인 (에스컬레이션 간격 체크) ---
        if await _should_escalate():
            msg = await generate_proactive_message(
                f"'{task['title']}' 하고 있지? 어떻게 되고 있어?",
                tasks=tasks, plan=plan
            )
            await _send_bot_message(msg)
            await db.bump_no_response()
        return

    # ============================================================
    # Step 5.5: 마감 임박 태스크 직접 알림 (플랜 없이도 작동)
    # ============================================================
    pending_tasks = [t for t in tasks if t["status"] in ("pending", "in_progress")]
    for task in pending_tasks:
        if task.get("deadline"):
            try:
                dl_str = task["deadline"]
                if len(dl_str) <= 5:
                    dl = datetime.combine(now.date(),
                                          datetime.strptime(dl_str, "%H:%M").time(),
                                          tzinfo=ZoneInfo(TIMEZONE))
                else:
                    dl = datetime.fromisoformat(dl_str)
                    if dl.tzinfo is None:
                        dl = dl.replace(tzinfo=ZoneInfo(TIMEZONE))

                minutes_until = (dl - now).total_seconds() / 60

                # 15분 전 알림
                if 0 < minutes_until <= 15 and task["status"] == "pending":
                    await db.update_task_status(task["id"], "in_progress")
                    msg = await generate_proactive_message(
                        f"'{task['title']}' {int(minutes_until)}분 후예요! 준비됐어요?",
                        tasks=tasks, plan=plan
                    )
                    await _send_bot_message(msg)
                    return
                # 30분 전 미리 알림
                elif 15 < minutes_until <= 30 and task["status"] == "pending":
                    msg = await generate_proactive_message(
                        f"'{task['title']}' 30분 후예요. 준비해주세요.",
                        tasks=tasks, plan=plan
                    )
                    await _send_bot_message(msg)
                    return
            except (ValueError, TypeError):
                continue

    # ============================================================
    # Step 6: 시작해야 할 태스크 리마인더 (플랜 기반)
    # ============================================================
    for slot in plan:
        if slot.get("task_status") == "pending" and slot["start_time"] <= now_hm:
            task = next((t for t in tasks if t["id"] == slot["task_id"]), None)
            if task:
                await db.update_task_status(task["id"], "in_progress")
                msg = await generate_proactive_message(
                    f"'{task['title']}' 시작할 시간이야! "
                    f"({slot['start_time']}~{slot['end_time']}). 시작하자!",
                    tasks=tasks, plan=plan
                )
                await _send_bot_message(msg)
                return

    # 플랜은 있지만 아직 시간 안 된 경우 → 컨디션 체크 기회
    upcoming = [s for s in plan if s["start_time"] > now_hm
                and s.get("task_status") in ("pending", "scheduled")]

    # ============================================================
    # Step 6.5: 컨디션 체크 (2~3시간마다)
    # ============================================================
    today_conditions = await db.get_today_conditions()
    last_condition_time = None
    if today_conditions:
        last_condition_time = today_conditions[-1].get("time", "00:00")

    # 마지막 컨디션 체크로부터 2시간 이상 지났으면
    should_check_condition = False
    if not today_conditions:
        # 오늘 첫 체크 — 오전 중 1회
        if hour >= MORNING_HOUR + 1:
            should_check_condition = True
    elif last_condition_time:
        last_min = int(last_condition_time.split(":")[0]) * 60 + int(last_condition_time.split(":")[1])
        now_min = hour * 60 + minute
        if now_min - last_min >= 120:  # 2시간
            should_check_condition = True

    if should_check_condition and not active_tasks:
        # 다른 긴급한 알림이 없을 때만
        conditions_today = len(today_conditions)
        if conditions_today == 0:
            msg = await generate_proactive_message(
                "컨디션 체크 시간. 유저에게 지금 기분이나 에너지 상태를 가볍게 물어봐. "
                "강요하지 말고 자연스럽게. 1줄이면 충분.",
                tasks=tasks
            )
        elif conditions_today <= 2:
            last_mood = today_conditions[-1].get("mood", "")
            msg = await generate_proactive_message(
                f"컨디션 중간 체크. 이전 기분: {last_mood}. "
                "지금은 어떤지 가볍게 물어봐. 운동 같은 건강 활동도 슬쩍 제안 가능.",
                tasks=tasks
            )
        else:
            msg = None  # 하루 3번 이상은 과하니까
        if msg:
            await _send_bot_message(msg)
            return

    if upcoming:
        return  # 다음 태스크까지 조용히 대기

    # ============================================================
    # Step 7: 미응답 에스컬레이션 / 할 것 없음
    # ============================================================

    # 미응답 체크
    if await _should_escalate():
        state = await db.get_user_state()
        level = state.get("escalation_level", 0)

        if level >= 3:
            msg = "솔직히 말할게요. 지금 4번째 알림이에요. 1분만 써서 답 주세요."
        elif level >= 2:
            msg = "아직 답이 없네요. 지금 잠깐만 시간 내줄 수 있어요?"
        elif level >= 1:
            msg = "혹시 메시지 못 보셨나요?"
        else:
            # 에스컬레이션 시작
            msg = None

        if msg:
            await _send_bot_message(msg)
            await db.bump_no_response()
            return

    # 태스크는 있는데 플랜 없음
    pending = [t for t in tasks if t["status"] in ("pending", "deferred")]
    if pending and not plan:
        msg = await generate_proactive_message(
            f"태스크 {len(pending)}개 있는데 플랜이 없어. 플랜 짜자!",
            tasks=tasks
        )
        await _send_bot_message(msg)
        return

    # 진짜 할 것 없음
    logger.debug("할 것 없음 — 대기")


# ============================================================
# 저녁 보고서
# ============================================================

async def _bedtime_routine():
    """취침 전 루틴 — AI에게 데이터만 주고 전부 맡김"""
    from datetime import timedelta

    stats = await db.get_task_stats_today()
    conditions = await db.get_today_conditions()
    all_tasks = await db.get_today_tasks()

    # 내일 태스크
    tomorrow = (datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_tasks = [t for t in all_tasks if t.get("deadline", "").startswith(tomorrow)]

    # 데이터를 JSON으로 넘김 — AI가 알아서 판단
    import json
    data = {
        "오늘_통계": {
            "전체": stats["total"], "완료": stats["done"],
            "미완료": stats["pending"], "연기": stats["deferred"],
            "실패": stats["failed"], "부분완료": stats["partial"],
            "총_미룸횟수": stats["total_postpones"],
        },
        "오늘_태스크": [
            {"제목": t["title"], "상태": t["status"],
             "미룸": t["postpone_count"], "실패": t["fail_count"]}
            for t in stats["tasks"]
        ],
        "오늘_컨디션": [
            {"시각": c["time"], "에너지": c.get("energy_level"),
             "기분": c.get("mood"), "활동": c.get("activity")}
            for c in conditions
        ],
        "내일_태스크": [
            {"제목": t["title"], "마감": t.get("deadline")}
            for t in tomorrow_tasks
        ],
    }

    msg = await generate_proactive_message(
        f"취침 전 루틴이야. 아래 데이터를 보고 신님에게 하루 마무리 메시지를 보내줘.\n"
        f"너의 판단으로 자유롭게 구성해. 단, 반드시 포함할 것:\n"
        f"- 오늘 하루 10점 만점 점수\n"
        f"- 내일 할 일이 있으면 미리 알려주고 준비물 체크\n"
        f"- 동기부여 한마디\n"
        f"- 잘 자라는 인사\n"
        f"형식, 길이, 톤은 네가 판단해.\n\n"
        f"데이터:\n{json.dumps(data, ensure_ascii=False, indent=2)}",
        tasks=stats["tasks"]
    )
    await _send_bot_message(msg)

    # DB 저장
    await db.save_daily_report(stats["total"], stats["done"], full_report)

    # 아카이브
    await db.archive_today()

    # 패턴 업데이트
    try:
        await update_patterns()
        logger.info("패턴 업데이트 완료")
    except Exception as e:
        logger.error(f"패턴 업데이트 실패: {e}")

    # 저녁 DB 백업
    try:
        await db.backup_db()
        logger.info("저녁 DB 백업 완료")
    except Exception as e:
        logger.error(f"저녁 DB 백업 실패: {e}")
