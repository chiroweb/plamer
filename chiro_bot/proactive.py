"""프로액티브 루프 — APScheduler로 주기적 트리거, 봇이 먼저 메시지 발송
Phase 2: 7단계 캐스케이드 완전 구현 + 에스컬레이션 + 저녁 보고서 + 헬스체크"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot
from chiro_bot.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE,
    MORNING_HOUR, EVENING_HOUR
)
from chiro_bot import database as db
from chiro_bot.ai_client import generate_proactive_message, generate_response
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
    """텔레그램으로 직접 메시지 발송 (프로액티브용)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("봇 토큰 또는 채팅 ID 미설정 — 메시지 발송 스킵")
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
    # Step 3: 저녁 리뷰
    # ============================================================
    if hour == EVENING_HOUR and minute < 20:
        await _evening_report()
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
    # Step 6: 시작해야 할 태스크 리마인더
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

    # 플랜은 있지만 아직 시간 안 된 경우
    upcoming = [s for s in plan if s["start_time"] > now_hm
                and s.get("task_status") in ("pending", "scheduled")]
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

async def _evening_report():
    """저녁 보고서 생성 + 아카이브 + DB 백업"""
    stats = await db.get_task_stats_today()
    tasks = stats["tasks"]

    if not tasks:
        await _send_bot_message("오늘은 등록된 태스크가 없었어. 내일은 뭐 할지 생각해봐! 🌙")
        return

    # 통계 텍스트
    report_lines = [
        "📊 오늘의 리포트\n",
        f"전체: {stats['total']}개",
        f"✅ 완료: {stats['done']}개",
        f"🟡 부분완료: {stats['partial']}개",
        f"⏸️ 연기: {stats['deferred']}개",
        f"❌ 실패: {stats['failed']}개",
        f"⬜ 미착수: {stats['pending']}개",
    ]

    if stats["total_postpones"] > 0:
        report_lines.append(f"\n총 미룬 횟수: {stats['total_postpones']}회")
    if stats["total_fails"] > 0:
        report_lines.append(f"총 실패 횟수: {stats['total_fails']}회")

    # 완료율
    if stats["total"] > 0:
        rate = (stats["done"] / stats["total"]) * 100
        report_lines.append(f"\n📈 완료율: {rate:.0f}%")

    # 태스크별 상세
    report_lines.append("\n--- 상세 ---")
    for t in tasks:
        emoji = {"done": "✅", "partial": "🟡", "deferred": "⏸️",
                 "failed": "❌", "pending": "⬜", "in_progress": "🔵"}.get(t["status"], "❓")
        line = f"{emoji} {t['title']}"
        if t["postpone_count"] > 0:
            line += f" (미룸 {t['postpone_count']}회)"
        if t["fail_count"] > 0:
            line += f" (실패 {t['fail_count']}회)"
        report_lines.append(line)

    report_text = "\n".join(report_lines)

    # AI로 코멘트 추가
    ai_comment = await generate_response(
        f"유저의 오늘 하루 리포트야:\n{report_text}\n\n"
        f"완료율과 미룸/실패 패턴을 보고 짧게 코멘트해줘 (2~3문장). "
        f"잘했으면 칭찬, 못했으면 건설적 피드백."
    )

    full_report = f"{report_text}\n\n💬 {ai_comment}"
    await _send_bot_message(full_report)

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
