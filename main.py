"""CHIRO Bot — 메인 엔트리포인트
Phase 2: 추가 커맨드, 헬스체크, DND 기본값 로드"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from chiro_bot.config import TELEGRAM_BOT_TOKEN, PROACTIVE_INTERVAL_MINUTES, TIMEZONE
from chiro_bot.database import (
    init_db,
    get_today_tasks,
    get_today_plan,
    get_today_dnd,
    get_task_stats_today,
    get_today_routines,
    get_upcoming_master_tasks,
)
from chiro_bot.handlers import (
    handle_message, start_plan_flow,
    cmd_ideas, cmd_routines, cmd_patterns,
)
from chiro_bot.proactive import proactive_check

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("chiro")


# ==================== 커맨드 핸들러 ====================

async def cmd_start(update, context):
    await update.message.reply_text(
        "안녕하세요. 저는 당신의 일정 관리 파트너예요.\n"
        "할 일을 말해주시면 수집하고, 플랜을 짜드릴게요.\n\n"
        "/plan — 플랜 생성\n"
        "/tasks — 태스크 목록\n"
        "/help — 도움말"
    )


async def cmd_plan(update, context):
    await start_plan_flow(update, context)


async def cmd_tasks(update, context):
    tasks = await get_today_tasks()
    if not tasks:
        await update.message.reply_text("오늘 등록된 태스크가 없어요. 뭐 하실 거예요?")
        return
    lines = ["📋 오늘의 태스크:\n"]
    for i, t in enumerate(tasks, 1):
        emoji = {"pending": "⬜", "in_progress": "🔵", "done": "✅",
                 "deferred": "⏸️", "partial": "🟡", "failed": "❌"}.get(t["status"], "❓")
        line = f"{emoji} {i}. {t['title']}"
        extras = []
        if t.get("deadline"):
            extras.append(f"마감: {t['deadline']}")
        if t["postpone_count"] > 0:
            extras.append(f"미룸 {t['postpone_count']}회")
        if t["fail_count"] > 0:
            extras.append(f"실패 {t['fail_count']}회")
        if extras:
            line += f"\n   ({', '.join(extras)})"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update, context):
    """현재 진행 상태 요약"""
    stats = await get_task_stats_today()
    plan = await get_today_plan()
    routines = await get_today_routines()
    upcoming = await get_upcoming_master_tasks(5)
    now_hm = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")

    if stats["total"] == 0 and not routines and not upcoming:
        await update.message.reply_text("오늘 등록된 태스크가 없어요.")
        return

    rate = (stats["done"] / stats["total"] * 100) if stats["total"] > 0 else 0
    lines = [
        f"📊 현재 상태 ({now_hm})\n",
        f"전체: {stats['total']}개 | 완료: {stats['done']} | 진행중: {stats['in_progress']} | 대기: {stats['pending']}",
        f"완료율: {rate:.0f}%",
    ]

    if routines:
        lines.append("\n📅 오늘 루틴:")
        for routine in routines:
            lines.append(f"  {routine['start_time']}~{routine['end_time']} {routine['label']}")

    if upcoming:
        dow_names = ["월", "화", "수", "목", "금", "토", "일"]
        lines.append("\n🗓️ 예정 태스크:")
        for task in upcoming:
            if task["task_type"] == "recurring":
                days = ",".join(dow_names[d] for d in task.get("recurrence_days", []))
                lines.append(f"  {task['title']} (반복: {days or '미정'})")
            elif task["task_type"] == "span":
                lines.append(f"  {task['title']} ({task['start_date']}~{task.get('end_date')})")
            else:
                lines.append(f"  {task['title']} ({task['start_date']})")

    if stats["total_postpones"] > 0:
        lines.append(f"미룬 횟수: {stats['total_postpones']}회")

    if plan:
        lines.append("\n📋 플랜:")
        for p in plan:
            emoji = {"done": "✅", "pending": "⬜", "scheduled": "⬜",
                     "in_progress": "🔵"}.get(p.get("task_status", ""), "⬜")
            marker = " ◀ NOW" if p["start_time"] <= now_hm <= p["end_time"] else ""
            lines.append(f"  {emoji} {p['start_time']}~{p['end_time']} {p['title']}{marker}")

    await update.message.reply_text("\n".join(lines))


async def cmd_dnd(update, context):
    """오늘 DND 목록"""
    dnd_slots = await get_today_dnd()
    if not dnd_slots:
        await update.message.reply_text("오늘 등록된 방해금지 시간이 없어요.\n'DND 14:00~16:00 수업' 이렇게 추가할 수 있어요.")
        return
    lines = ["🔕 오늘의 방해금지:\n"]
    for s in dnd_slots:
        lines.append(f"  {s['start_time']}~{s['end_time']}  {s.get('reason', '')}")
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update, context):
    await update.message.reply_text(
        "🤖 치로봇 사용법:\n\n"
        "💬 그냥 할 일을 말해! → 자동 수집\n"
        "🚨 '급해! ...' → 긴급 일정 삽입\n\n"
        "--- 커맨드 ---\n"
        "📋 /plan — 오늘 플랜 생성\n"
        "📝 /tasks — 오늘 태스크 목록\n"
        "📊 /status — 현재 진행 상태\n"
        "🔕 /dnd — 오늘 방해금지 목록\n"
        "💡 /ideas — 아이디어 볼트\n"
        "📅 /routines — 루틴 목록\n"
        "📈 /patterns — 행동 패턴 분석\n\n"
        "--- 설정 ---\n"
        "/add_routine 요일 시작 종료 이름\n"
        "/add_dnd 요일 시작 종료 사유\n\n"
        "--- 상태 변경 ---\n"
        "  ✅ '끝났어' — 완료\n"
        "  ⏸️ '미룰래' — 연기\n"
        "  ❌ '못하겠어' — 실패\n"
        "  🟡 '절반 했어' — 부분 완료\n"
    )


# ==================== 메인 ====================

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        print("\n⚠️  .env 파일에 TELEGRAM_BOT_TOKEN을 설정해주세요!")
        print("   1. BotFather(@BotFather)에서 봇 생성")
        print("   2. 받은 토큰을 .env에 입력")
        print("   3. 봇과 대화 시작 후 TELEGRAM_CHAT_ID도 설정\n")
        return

    # DB 초기화
    asyncio.get_event_loop().run_until_complete(init_db())
    logger.info("DB 초기화 완료")

    # 텔레그램 봇 빌드
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # 커맨드 핸들러
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("dnd", cmd_dnd))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
    app.add_handler(CommandHandler("routines", cmd_routines))
    app.add_handler(CommandHandler("patterns", cmd_patterns))
    app.add_handler(CommandHandler("help", cmd_help))

    # 일반 메시지 핸들러
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # APScheduler — 프로액티브 루프
    tz = str(ZoneInfo(TIMEZONE))
    scheduler = AsyncIOScheduler(timezone=tz)

    # 메인 프로액티브 체크 (매 N분)
    scheduler.add_job(
        proactive_check,
        trigger=IntervalTrigger(minutes=PROACTIVE_INTERVAL_MINUTES),
        id="proactive_loop",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"프로액티브 루프 시작 (간격: {PROACTIVE_INTERVAL_MINUTES}분)")

    # 봇 시작 (로컬 개발은 polling 모드)
    logger.info("치로봇 시작! (polling 모드)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
