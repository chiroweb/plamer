"""리액티브 핸들러 — AI 자율 판단 구조.
하드코딩된 인텐트 라우팅 없음. AI가 대화를 보고 스스로 도구를 호출한다."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from chiro_bot import database as db
from chiro_bot.config import TIMEZONE
from chiro_bot.ai_client import chat

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """메인 메시지 핸들러 — 모든 메시지를 AI에게 전달"""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    # 유저 응답 수신 → 미응답 카운트 리셋
    await db.reset_no_response()

    # 메시지 로그
    await db.log_message("user", user_text)

    # 최근 대화 히스토리 로드
    recent = await db.get_recent_messages(15)

    # AI에게 전달 — AI가 알아서 판단하고 도구 호출
    reply, called_tools = await chat(user_text, recent)

    if called_tools:
        logger.info(f"AI 도구 호출: {[t['name'] for t in called_tools]}")

    # 응답 발송
    if reply:
        await db.log_message("bot", reply)
        await db.update_user_state(
            last_bot_message_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        )
        await update.message.reply_text(reply)


# ==================== 커맨드용 함수 (main.py에서 import) ====================

async def start_plan_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """플랜 생성 — /plan 커맨드용"""
    recent = await db.get_recent_messages(5)
    reply, _ = await chat("플랜을 짜줘", recent)
    if reply:
        await db.log_message("bot", reply)
        await update.message.reply_text(reply)


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """아이디어 목록"""
    async with (await db.get_db()) as conn:
        cursor = await conn.execute(
            "SELECT id, content FROM ideas WHERE converted_to_task_id IS NULL ORDER BY id DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("저장된 아이디어가 없어요.")
        return
    lines = ["💡 아이디어 볼트:\n"]
    for r in rows:
        lines.append(f"  {r[0]}. {r[1][:60]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_routines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """루틴 목록"""
    import aiosqlite
    from chiro_bot.config import DB_PATH
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM routines ORDER BY day_of_week, start_time")
        rows = [dict(r) for r in await cursor.fetchall()]
    if not rows:
        await update.message.reply_text("등록된 루틴이 없어요. 루틴을 말씀해주시면 등록해드릴게요.")
        return
    dow_names = ["월", "화", "수", "목", "금", "토", "일"]
    lines = ["📅 등록된 루틴:\n"]
    current_dow = -1
    for r in rows:
        if r["day_of_week"] != current_dow:
            current_dow = r["day_of_week"]
            lines.append(f"\n[{dow_names[current_dow]}]")
        lines.append(f"  {r['start_time']}~{r['end_time']}  {r['label']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """행동 패턴"""
    from chiro_bot.patterns import get_pattern_summary
    summary = await get_pattern_summary()
    await update.message.reply_text(f"📊 행동 패턴 분석:\n\n{summary}")
