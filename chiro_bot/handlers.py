"""리액티브 핸들러 — 코드가 라우팅하는 구조.
AI는 인텐트 파싱만 담당. 메시지 생성은 templates.py가 담당."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from chiro_bot import database as db
from chiro_bot import templates
from chiro_bot.config import TIMEZONE
from chiro_bot.ai_client import parse_intent, generate_chat_response
from chiro_bot.tools import execute_tool

logger = logging.getLogger(__name__)


# ============================================================
# 헬퍼
# ============================================================


async def _get_last_bot_question() -> str:
    """최근 봇 메시지 중 마지막 질문을 반환."""
    recent = await db.get_recent_messages(5)
    for m in reversed(recent):
        if m.get("direction") == "bot":
            return m.get("content", "")
    return ""


async def _get_current_state_text() -> str:
    """현재 user_state를 인텐트 파싱에 넘길 텍스트로 구성."""
    state = await db.get_user_state()
    flow = state.get("flow_data") or {}
    flow_step = flow.get("step", "")
    parts = []
    if flow_step:
        parts.append(f"flow_step={flow_step}")
    tasks = await db.get_today_tasks()
    active = [t for t in tasks if t["status"] == "in_progress"]
    if active:
        parts.append(f"active_task={active[0]['title']}")
    pending = [t for t in tasks if t["status"] == "pending"]
    if pending:
        parts.append(f"pending_count={len(pending)}")
    return ", ".join(parts) if parts else "없음"


def _calculate_actual_minutes(task: dict) -> int:
    """태스크의 실제 소요 시간(분) 계산. started_at이 없으면 estimated_minutes 반환."""
    started = task.get("started_at")
    if not started:
        return task.get("estimated_minutes") or 0
    try:
        now = datetime.now(ZoneInfo(TIMEZONE))
        start_dt = datetime.fromisoformat(started)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        return max(1, int((now - start_dt).total_seconds() / 60))
    except Exception:
        return task.get("estimated_minutes") or 0


async def _get_next_task() -> tuple[str | None, str | None]:
    """다음 대기 중인 태스크 이름과 시작 시간을 반환."""
    tasks = await db.get_today_tasks()
    pending = [t for t in tasks if t["status"] == "pending"]
    if not pending:
        return None, None
    nxt = pending[0]
    plan = await db.get_today_plan()
    start_time = None
    for p in plan:
        if p.get("task_id") == nxt["id"]:
            start_time = p.get("start_time")
            break
    return nxt["title"], start_time


def _remaining_hours(task: dict) -> float:
    """마감까지 남은 시간."""
    dl = task.get("deadline")
    if not dl:
        return 24.0
    try:
        now = datetime.now(ZoneInfo(TIMEZONE))
        dl_dt = datetime.fromisoformat(dl)
        if dl_dt.tzinfo is None:
            dl_dt = dl_dt.replace(tzinfo=ZoneInfo(TIMEZONE))
        return max(0, (dl_dt - now).total_seconds() / 3600)
    except Exception:
        return 24.0


# ============================================================
# 메인 핸들러
# ============================================================


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """메인 메시지 핸들러 — AI 인텐트 파싱 → 코드 라우팅 → 템플릿 응답."""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    try:
        # 유저 응답 수신 → 미응답 카운트 리셋, 메시지 로그
        await db.reset_no_response()
        await db.log_message("user", user_text)

        # 현재 상태 & 마지막 봇 질문
        current_state = await _get_current_state_text()
        last_bot_question = await _get_last_bot_question()

        # 1. AI에게 인텐트 파싱만 요청
        intent_json = await parse_intent(user_text, current_state, last_bot_question)
        intent = intent_json.get("intent", "unclear")
        logger.info(f"인텐트 파싱 결과: {intent} — {intent_json}")

        reply: str | None = None

        # 2. 인텐트별 코드 라우팅
        # ----------------------------------------------------------
        if intent == "add_task":
            result_raw = await execute_tool("add_task", intent_json)
            reply = templates.task_added(
                intent_json.get("title", "태스크"),
                intent_json.get("deadline"),
            )

        # ----------------------------------------------------------
        elif intent == "complete_task":
            result_raw = await execute_tool("complete_task", {
                "task_title_hint": intent_json.get("hint", ""),
            })
            result = json.loads(result_raw)
            if result.get("success"):
                # 완료된 태스크 정보 조회
                tasks = await db.get_today_tasks()
                completed_title = result.get("completed", "")
                # 완료 태스크의 수치 정보 (이미 done으로 변경됨)
                task = None
                for t in tasks:
                    if t["title"] == completed_title:
                        task = t
                        break
                estimated = (task or {}).get("estimated_minutes") or 0
                actual = _calculate_actual_minutes(task) if task else estimated
                next_task, next_time = await _get_next_task()
                reply = templates.task_completed(
                    completed_title, estimated, actual, next_task, next_time,
                )
            else:
                reply = result.get("message", "완료할 태스크를 찾지 못했어요.")

        # ----------------------------------------------------------
        elif intent == "defer_task":
            result_raw = await execute_tool("defer_task", {
                "task_title_hint": intent_json.get("hint", ""),
            })
            result = json.loads(result_raw)
            if result.get("success"):
                count = result.get("postpone_count", 1)
                task_title = result.get("deferred", "")
                if count >= 3:
                    # 팩트 압박
                    deadline = result.get("deadline", "")
                    tasks = await db.get_today_tasks()
                    task = next((t for t in tasks if t["title"] == task_title), None)
                    remaining_h = _remaining_hours(task) if task else 24.0
                    estimated_h = ((task or {}).get("estimated_minutes") or 60) / 60
                    reply = templates.postpone_pressure(
                        count, task_title, deadline, remaining_h, estimated_h,
                    )
                else:
                    reply = templates.postpone_response(count)
            else:
                reply = result.get("message", "미룰 태스크를 찾지 못했어요.")

        # ----------------------------------------------------------
        elif intent == "fail_task":
            result_raw = await execute_tool("fail_task", {
                "task_title_hint": intent_json.get("hint", ""),
            })
            result = json.loads(result_raw)
            if result.get("success"):
                count = result.get("fail_count", 1)
                task_title = result.get("failed", "")
                if count >= 3:
                    # 태스크 분해 제안 (간단 기본값)
                    sub_tasks = [
                        {"title": f"{task_title} - 1단계", "minutes": 15},
                        {"title": f"{task_title} - 2단계", "minutes": 15},
                        {"title": f"{task_title} - 3단계", "minutes": 15},
                    ]
                    reply = templates.fail_decompose(task_title, sub_tasks)
                else:
                    reply = templates.fail_response(count)
            else:
                reply = result.get("message", "태스크를 찾지 못했어요.")

        # ----------------------------------------------------------
        elif intent == "partial_complete":
            progress = intent_json.get("progress", 50)
            remaining = max(1, int((100 - progress) / 100 * 60))  # 대략 추정
            reply = templates.partial_complete(progress, remaining)

        # ----------------------------------------------------------
        elif intent == "set_reminder":
            await execute_tool("set_reminder", {
                "time": intent_json.get("time", ""),
                "message": intent_json.get("message", "알림"),
            })
            reply = templates.reminder_set(
                intent_json.get("time", ""),
                intent_json.get("message", ""),
            )

        # ----------------------------------------------------------
        elif intent == "add_routine":
            result_raw = await execute_tool("add_routines", intent_json)
            result = json.loads(result_raw)
            routines = intent_json.get("routines", [])
            if routines:
                first = routines[0]
                label = first.get("label", "루틴")
                time_range = f"{first.get('start_time', '')}~{first.get('end_time', '')}"
                reply = templates.routine_added(label, time_range)
            else:
                reply = "루틴 등록했어요."

        # ----------------------------------------------------------
        elif intent == "add_dnd":
            end_time = intent_json.get("end")
            if not end_time:
                # 종료 시간 모름 → 물어보기
                reply = templates.dnd_ask_end_time()
            else:
                await execute_tool("add_dnd", {
                    "start_time": intent_json.get("start", ""),
                    "end_time": end_time,
                    "reason": intent_json.get("reason", ""),
                    "today_only": True,
                })
                reply = f"방해금지 {intent_json.get('start', '')}~{end_time} 등록했어요. 그때까지 조용히 할게요."

        # ----------------------------------------------------------
        elif intent == "save_idea":
            content = intent_json.get("content", user_text)
            await execute_tool("save_idea", {"content": content})
            summary = content[:40]
            reply = templates.idea_saved(summary)

        # ----------------------------------------------------------
        elif intent == "ask_status":
            result_raw = await execute_tool("get_today_status", {})
            result = json.loads(result_raw)
            tasks_info = result.get("tasks", [])
            stats = result.get("stats", {})
            if not tasks_info:
                reply = "오늘 등록된 태스크가 없어요."
            else:
                lines = [f"현재 {result.get('current_time', '')}"]
                done = stats.get("done", 0)
                total = stats.get("total", len(tasks_info))
                lines.append(f"완료 {done}/{total}")
                for t in tasks_info:
                    status_map = {
                        "done": "완료", "pending": "대기",
                        "in_progress": "진행중", "failed": "실패",
                        "deferred": "미룸",
                    }
                    s = status_map.get(t["status"], t["status"])
                    dl = f" (마감 {t['deadline']})" if t.get("deadline") else ""
                    lines.append(f"- {t['title']}: {s}{dl}")
                reply = "\n".join(lines)

        # ----------------------------------------------------------
        elif intent == "confirm":
            state = await db.get_user_state()
            flow_data = (state.get("flow_data") or {})
            pending_plan = flow_data.get("pending_plan")

            if pending_plan:
                await execute_tool("confirm_plan", {})
                reply = templates.plan_confirmed()
            elif "시작" in last_bot_question or "할 수 있어요" in last_bot_question:
                # 시작 리마인더에 대한 확인
                reply = "좋아요."
            else:
                reply = "좋아요."

        # ----------------------------------------------------------
        elif intent == "reject":
            reply = "어떻게 바꿀까요?"

        # ----------------------------------------------------------
        elif intent == "frustrated":
            reply = templates.user_frustrated("지금 하", "내일로 넘기")

        # ----------------------------------------------------------
        elif intent == "want_rest":
            reply = templates.user_wants_rest()

        # ----------------------------------------------------------
        elif intent == "emergency":
            # 긴급 일정 삽입: 태스크 추가 후 플랜 재생성
            title = intent_json.get("title", "긴급 일정")
            duration = intent_json.get("duration_minutes", 60)
            await execute_tool("add_task", {
                "title": title,
                "estimated_minutes": duration,
            })
            plan_result_raw = await execute_tool("create_plan", {})
            plan_result = json.loads(plan_result_raw)
            if plan_result.get("success"):
                plan_text = plan_result.get("plan_text", "")
                reply = f"긴급 일정 '{title}' 추가하고 플랜 재조정했어요.\n{plan_text}\n이대로 갈까요?"
            else:
                reply = f"긴급 일정 '{title}' 추가했어요. 플랜 재조정이 필요하면 말해주세요."

        # ----------------------------------------------------------
        elif intent == "chat":
            recent = await db.get_recent_messages(10)
            reply = await generate_chat_response(user_text, recent)

        # ----------------------------------------------------------
        elif intent == "answer_question":
            # 태스크 수집 플로우 중 질문에 대한 답변 처리
            field = intent_json.get("field", "")
            value = intent_json.get("value", "")
            state = await db.get_user_state()
            flow_data = (state.get("flow_data") or {})

            if field == "duration":
                # 소요 시간 답변 → 다음 질문(마감)
                flow_data["estimated_minutes"] = value
                await db.update_user_state(flow_data=flow_data)
                reply = templates.ask_deadline(flow_data.get("pending_title", "이 태스크"))
            elif field == "deadline":
                flow_data["deadline"] = value
                await db.update_user_state(flow_data=flow_data)
                reply = templates.ask_start_time()
            elif field == "start_time":
                flow_data["start_time"] = value
                await db.update_user_state(flow_data=flow_data)
                reply = templates.ask_dnd()
            elif field == "dnd":
                # 수집 완료 → 태스크 등록
                flow_data["dnd"] = value
                pending_title = flow_data.get("pending_title", "태스크")
                await execute_tool("add_task", {
                    "title": pending_title,
                    "deadline": flow_data.get("deadline"),
                    "estimated_minutes": flow_data.get("estimated_minutes"),
                })
                await db.update_user_state(flow_data=None)
                reply = templates.task_added(pending_title, flow_data.get("deadline"))
            else:
                reply = "다시 말해줄 수 있어요?"

        # ----------------------------------------------------------
        elif intent == "unclear":
            reply = "다시 말해줄 수 있어요?"

        # ----------------------------------------------------------
        else:
            reply = "다시 말해줄 수 있어요?"

        # 3. 응답 발송
        if reply:
            await db.log_message("bot", reply)
            await db.update_user_state(
                last_bot_message_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            )
            await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"메시지 처리 오류: {e}", exc_info=True)
        await update.message.reply_text("잠시 문제가 생겼어요. 다시 말씀해주세요.")


# ============================================================
# 커맨드용 함수 (main.py에서 import)
# ============================================================


async def start_plan_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """플랜 생성 — /plan 커맨드용."""
    try:
        result_raw = await execute_tool("create_plan", {})
        result = json.loads(result_raw)
        if result.get("success"):
            plan_text = result.get("plan_text", "")
            reply = f"{plan_text}\n이대로 갈까요?"
        else:
            reply = result.get("message", "스케줄 가능한 태스크가 없어요.")
        await db.log_message("bot", reply)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"플랜 생성 오류: {e}", exc_info=True)
        await update.message.reply_text("플랜 생성 중 문제가 생겼어요.")


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """아이디어 목록."""
    async with (await db.get_db()) as conn:
        cursor = await conn.execute(
            "SELECT id, content FROM ideas WHERE converted_to_task_id IS NULL "
            "ORDER BY id DESC LIMIT 20"
        )
        rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("저장된 아이디어가 없어요.")
        return
    lines = ["아이디어 목록:\n"]
    for r in rows:
        lines.append(f"  {r[0]}. {r[1][:60]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_routines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """루틴 목록."""
    import aiosqlite
    from chiro_bot.config import DB_PATH

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM routines ORDER BY day_of_week, start_time"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    if not rows:
        await update.message.reply_text("등록된 루틴이 없어요.")
        return
    dow_names = ["월", "화", "수", "목", "금", "토", "일"]
    lines = ["등록된 루틴:\n"]
    current_dow = -1
    for r in rows:
        if r["day_of_week"] != current_dow:
            current_dow = r["day_of_week"]
            lines.append(f"\n[{dow_names[current_dow]}]")
        lines.append(f"  {r['start_time']}~{r['end_time']}  {r['label']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """행동 패턴."""
    from chiro_bot.patterns import get_pattern_summary

    summary = await get_pattern_summary()
    await update.message.reply_text(f"행동 패턴 분석:\n\n{summary}")
