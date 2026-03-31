"""리액티브 핸들러 — 유저 메시지 수신 → 인텐트 파싱 → 라우팅 → 응답
Phase 2: 5갈래 상태 분기 완전 구현, DND 실시간+플랜 재조정, 미응답 리셋"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from chiro_bot import database as db
from chiro_bot.config import TIMEZONE
from chiro_bot.ai_client import parse_intent, generate_response
from chiro_bot.planner import generate_plan, format_plan_text, format_plan_change, insert_urgent_task, add_task_and_replan

logger = logging.getLogger(__name__)


# ==================== 태스크 수집 5단계 ====================

COLLECT_QUESTIONS = {
    1: "오늘 뭐 하실 거예요?",
    2: "어떤 종류예요? (학업/과제/개인/기타)",
    3: "언제까지 해야 해요? (예: 오늘 18:00, 내일 23:59, 없음)",
    4: "얼마나 걸려요? (분 단위, 예: 60)",
    5: "언제 시작할 수 있어요? (예: 14:00, 없으면 '알아서')",
}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """메인 메시지 핸들러"""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    # 유저 응답 수신 → 미응답 카운트 리셋
    await db.reset_no_response()

    # 메시지 로그
    await db.log_message("user", user_text)

    # 유저 상태 확인
    state = await db.get_user_state()
    current_flow = state.get("current_flow")

    # 플로우 중이면 플로우 핸들러로
    if current_flow == "task_collect":
        await _handle_task_collect(update, context, state, user_text)
        return
    if current_flow == "plan_confirm":
        await _handle_plan_confirm(update, context, state, user_text)
        return
    if current_flow == "task_select":
        await _handle_task_select(update, context, state, user_text)
        return

    # 인텐트 파싱
    recent = await db.get_recent_messages(10)
    context_str = "\n".join(
        f"{'봇' if m['direction']=='bot' else '유저'}: {m['content']}"
        for m in recent[-6:]
    )
    result = await parse_intent(user_text, context_str)
    intent = result.get("intent", "general")
    data = result.get("data", {})

    # 인텐트 라우팅
    if intent == "task_add":
        await _start_task_collect(update, context, data)
    elif intent == "task_done":
        await _handle_task_done(update, context, data)
    elif intent == "task_defer":
        await _handle_task_defer(update, context, data)
    elif intent == "task_fail":
        await _handle_task_fail(update, context, data)
    elif intent == "task_partial":
        await _handle_task_partial(update, context, data)
    elif intent == "task_status":
        await _handle_task_status(update, context, data)
    elif intent == "dnd_add":
        await _handle_dnd_add(update, context, data)
    elif intent == "idea_capture":
        await _handle_idea(update, context, data)
    elif intent == "plan_confirm":
        await _handle_plan_confirm_intent(update, context, data)
    elif intent == "plan_modify":
        await _handle_plan_modify(update, context, data)
    elif intent == "urgent_add":
        await _handle_urgent_add(update, context, data)
    else:
        # 일반 대화
        recent_msgs = await db.get_recent_messages(10)
        reply = await generate_response(user_text, recent_msgs)
        await _send(update, context, reply)


# ==================== 태스크 수집 플로우 ====================

async def _start_task_collect(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """태스크 수집 시작 — AI가 이미 파싱한 데이터가 있으면 활용"""
    flow_data = {}
    next_step = 1

    if data.get("title"):
        flow_data["title"] = data["title"]
        next_step = 2
    if data.get("category"):
        flow_data["category"] = data["category"]
        if next_step <= 2:
            next_step = 3
    if data.get("deadline"):
        flow_data["deadline"] = data["deadline"]
        if next_step <= 3:
            next_step = 4

    await db.update_user_state(
        current_flow="task_collect",
        flow_step=next_step,
        flow_data=flow_data
    )

    if next_step > 1 and flow_data.get("title"):
        await _send(update, context, f"'{flow_data['title']}' 추가할게요. {COLLECT_QUESTIONS[next_step]}")
    else:
        await _send(update, context, COLLECT_QUESTIONS[next_step])


async def _handle_task_collect(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, user_text: str):
    """태스크 수집 플로우 진행"""
    step = state.get("flow_step", 1)
    flow_data = state.get("flow_data") or {}

    if step == 1:
        flow_data["title"] = user_text
        next_step = 2
    elif step == 2:
        flow_data["category"] = user_text
        next_step = 3
    elif step == 3:
        flow_data["deadline"] = None if user_text.strip() in ("없음", "없어", "없어요", "x", "X") else user_text
        next_step = 4
    elif step == 4:
        try:
            flow_data["estimated_minutes"] = int("".join(c for c in user_text if c.isdigit()) or "60")
        except ValueError:
            flow_data["estimated_minutes"] = 60
        next_step = 5
    elif step == 5:
        flow_data["preferred_start"] = None if user_text in ("알아서", "없음", "없어", "x", "X") else user_text
        next_step = 6
    else:
        next_step = 6

    if next_step <= 5:
        await db.update_user_state(flow_step=next_step, flow_data=flow_data)
        await _send(update, context, COLLECT_QUESTIONS[next_step])
    else:
        # 수집 완료 → 태스크 저장
        task_id = await db.add_task(
            title=flow_data.get("title", "무제"),
            category=flow_data.get("category"),
            deadline=flow_data.get("deadline"),
            estimated_minutes=flow_data.get("estimated_minutes"),
            preferred_start=flow_data.get("preferred_start"),
        )
        await db.update_user_state(current_flow=None, flow_step=0, flow_data=None)

        reply = (
            f"✅ 태스크 추가했어요.\n"
            f"• {flow_data.get('title')}\n"
            f"• 카테고리: {flow_data.get('category', '-')}\n"
            f"• 마감: {flow_data.get('deadline', '없음')}\n"
            f"• 예상: {flow_data.get('estimated_minutes', 60)}분\n\n"
            f"더 추가할 거 있어요? 없으면 '플랜 짜줘'라고 해주세요."
        )
        await _send(update, context, reply)


# ==================== 플랜 확인 플로우 ====================

async def start_plan_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """플랜 생성 및 확인 요청"""
    tasks = await db.get_today_tasks()
    dnd_slots = await db.get_today_dnd()

    if not tasks:
        await _send(update, context, "오늘 등록된 태스크가 없어요. 뭐 하실 거예요?")
        return

    plan_slots = await generate_plan(tasks, dnd_slots)
    if not plan_slots:
        await _send(update, context, "스케줄 가능한 태스크가 없어요. 태스크 상태를 확인해볼까요?")
        return

    plan_text = format_plan_text(plan_slots, tasks)
    await db.update_user_state(
        current_flow="plan_confirm",
        flow_step=0,
        flow_data={"plan_slots": plan_slots}
    )
    await _send(update, context, plan_text)


async def _handle_plan_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, user_text: str):
    """플랜 확인/수정 응답 처리"""
    flow_data = state.get("flow_data") or {}
    plan_slots = flow_data.get("plan_slots", [])

    result = await parse_intent(user_text, "봇이 오늘의 플랜을 제안했고 유저가 응답함")
    intent = result.get("intent", "general")

    yes_words = ("ㅇㅇ", "좋아", "확인", "ㄱㄱ", "고", "오케이", "ok", "네", "응", "그래", "ㅇ")
    no_words = ("수정", "바꿔", "변경", "아니", "다시")

    if intent == "plan_confirm" or any(w in user_text for w in yes_words):
        await db.set_daily_plan(plan_slots)
        await db.update_user_state(current_flow=None, flow_step=0, flow_data=None)
        await _send(update, context, "✅ 플랜 확정했어요. 시간 되면 알려드릴게요.")
    elif intent == "plan_modify" or any(w in user_text for w in no_words):
        await db.update_user_state(current_flow=None, flow_step=0, flow_data=None)
        reply = await generate_response(
            f"유저가 플랜 수정을 원함: '{user_text}'. 어떻게 바꿀지 물어봐."
        )
        await _send(update, context, reply)
    else:
        await _send(update, context, "이대로 갈까요? 수정하고 싶으면 말해주세요.")


async def _handle_plan_confirm_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    await start_plan_flow(update, context)


async def _handle_plan_modify(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    await start_plan_flow(update, context)


# ==================== 태스크 상태 분기 5갈래 ====================

def _find_target_task(tasks: list, data: dict, prefer_status: list = None):
    """인텐트 data에서 태스크 매칭 or 활성 태스크 반환"""
    # AI가 제목을 파싱했으면 매칭 시도
    title_hint = data.get("title", "")
    if title_hint:
        for t in tasks:
            if title_hint.lower() in t["title"].lower():
                return t

    # 진행 중 우선
    if prefer_status:
        for status in prefer_status:
            matched = [t for t in tasks if t["status"] == status]
            if matched:
                return matched[0]

    # in_progress > pending 순
    for status in ("in_progress", "pending"):
        matched = [t for t in tasks if t["status"] == status]
        if matched:
            return matched[0]
    return None


async def _handle_task_done(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """완료 → 다음 태스크 자동 연결"""
    tasks = await db.get_today_tasks()
    task = _find_target_task(tasks, data, prefer_status=["in_progress"])

    if not task:
        # 태스크 선택 요청
        actionable = [t for t in tasks if t["status"] in ("pending", "in_progress")]
        if actionable:
            await _ask_task_select(update, context, actionable, "done")
        else:
            await _send(update, context, "완료할 태스크가 없어요.")
        return

    await db.update_task_status(task["id"], "done")

    # 다음 태스크 자동 연결
    plan = await db.get_today_plan()
    remaining = [t for t in tasks if t["id"] != task["id"] and t["status"] in ("pending", "in_progress")]
    next_in_plan = [p for p in plan if p.get("task_status") in ("pending", "scheduled")]

    if next_in_plan:
        nxt = next_in_plan[0]
        reply = (
            f"✅ '{task['title']}' 완료했어요.\n"
            f"다음은 {nxt['title']} ({nxt['start_time']}~{nxt['end_time']})이에요. 시작할 수 있어요?"
        )
    elif remaining:
        reply = (
            f"✅ '{task['title']}' 완료했어요.\n"
            f"남은 태스크 {len(remaining)}개:\n"
            + "\n".join(f"  • {t['title']}" for t in remaining[:3])
        )
    else:
        stats = await db.get_task_stats_today()
        reply = (
            f"✅ '{task['title']}' 완료. 오늘 태스크 전부 끝났어요.\n"
            f"{stats['done']}/{stats['total']}개 완료. 저녁에 리뷰 보내드릴게요."
        )
    await _send(update, context, reply)


async def _handle_task_defer(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """미루기 → 카운트 + 압박 + 플랜 재조정"""
    tasks = await db.get_today_tasks()
    task = _find_target_task(tasks, data, prefer_status=["in_progress", "pending"])

    if not task:
        await _send(update, context, "미룰 태스크가 없어요.")
        return

    await db.increment_postpone(task["id"])
    count = task["postpone_count"] + 1

    if count >= 3:
        deadline_info = ""
        if task.get("deadline"):
            try:
                now = datetime.now(ZoneInfo(TIMEZONE))
                dl_str = task["deadline"]
                if len(dl_str) <= 5:
                    dl = datetime.combine(now.date(),
                                          datetime.strptime(dl_str, "%H:%M").time(),
                                          tzinfo=ZoneInfo(TIMEZONE))
                else:
                    dl = datetime.fromisoformat(dl_str)
                remaining = (dl - now).total_seconds() / 60
                deadline_info = f" 마감까지 {int(remaining)}분 남았어요."
            except (ValueError, TypeError):
                deadline_info = f" 마감: {task['deadline']}."

        reply = (
            f"'{task['title']}' 벌써 {count}번째 미루신 거예요.{deadline_info}\n"
            f"지금 안 하면 나중에 더 힘들어져요. 지금 시작할 수 있어요?"
        )
    else:
        reply = f"알겠어요. '{task['title']}' 미뤘어요. (미룸 {count}회)\n언제 할 수 있어요?"

    await _send(update, context, reply)

    # 플랜 재조정
    await _regenerate_plan_silently()


async def _handle_task_fail(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """못함 → 카운트 + 태스크 분해 제안"""
    tasks = await db.get_today_tasks()
    task = _find_target_task(tasks, data, prefer_status=["in_progress", "pending"])

    if not task:
        await _send(update, context, "진행 중인 태스크가 없어요.")
        return

    await db.increment_fail(task["id"])
    count = task["fail_count"] + 1

    if count >= 3:
        subtask_suggestion = await generate_response(
            f"유저가 '{task['title']}' 태스크를 {count}번 못하겠다고 했어요. "
            f"이 태스크를 3~5개의 작은 단계로 쪼개서 제안해주세요. "
            f"각 단계는 30분 이하로. 첫 단계는 특히 부담 적게."
        )
        reply = (
            f"'{task['title']}' {count}번째예요. 혹시 태스크가 너무 큰 건 아닌가요?\n"
            f"이렇게 쪼개보면 어때요?\n\n"
            f"{subtask_suggestion}"
        )
    else:
        reply = (
            f"알겠어요. '{task['title']}' 일단 보류할게요. (실패 {count}회)\n"
            f"뭐가 어려운지 알려주실 수 있어요?"
        )
    await _send(update, context, reply)


async def _handle_task_partial(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """부분 완료 → 남은 부분 새 태스크로 분리 가능"""
    tasks = await db.get_today_tasks()
    task = _find_target_task(tasks, data, prefer_status=["in_progress"])

    if not task:
        await _send(update, context, "진행 중인 태스크가 없어요.")
        return

    await db.update_task_status(task["id"], "partial")

    # 다음 태스크 연결
    plan = await db.get_today_plan()
    next_slots = [p for p in plan if p.get("task_status") in ("pending", "scheduled")]

    reply = f"'{task['title']}' 부분 완료 기록했어요. 남은 부분은 나중에 이어서 해요."
    if next_slots:
        reply += f"\n다음은 {next_slots[0]['title']} ({next_slots[0]['start_time']}~{next_slots[0]['end_time']})이에요."
    await _send(update, context, reply)


async def _handle_task_status(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """진행 상태 질문/응답"""
    tasks = await db.get_today_tasks()
    plan = await db.get_today_plan()

    if not tasks:
        await _send(update, context, "오늘 등록된 태스크가 없어요.")
        return

    stats = await db.get_task_stats_today()
    now_hm = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")

    lines = [f"📊 현재 상태 ({now_hm})\n"]
    lines.append(f"전체 {stats['total']}개 | 완료 {stats['done']} | 진행중 {stats['in_progress']} | 대기 {stats['pending']}")

    if plan:
        lines.append("\n📋 오늘 플랜:")
        for p in plan:
            emoji = {"done": "✅", "pending": "⬜", "scheduled": "⬜",
                     "in_progress": "🔵", "active": "🔵"}.get(p.get("task_status", ""), "❓")
            marker = " ◀ 지금" if p["start_time"] <= now_hm <= p["end_time"] else ""
            lines.append(f"  {emoji} {p['start_time']}~{p['end_time']} {p['title']}{marker}")

    await _send(update, context, "\n".join(lines))


# ==================== 태스크 선택 플로우 ====================

async def _ask_task_select(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           tasks: list, action: str):
    """여러 태스크 중 하나 선택 요청"""
    task_list = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(tasks))
    await db.update_user_state(
        current_flow="task_select",
        flow_step=0,
        flow_data={"task_ids": [t["id"] for t in tasks], "action": action}
    )
    action_text = {"done": "완료", "defer": "미루기", "fail": "실패"}.get(action, action)
    await _send(update, context, f"어떤 태스크를 {action_text}할 건지 번호로 알려주세요.\n{task_list}")


async def _handle_task_select(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              state: dict, user_text: str):
    """태스크 번호 선택 처리"""
    flow_data = state.get("flow_data") or {}
    task_ids = flow_data.get("task_ids", [])
    action = flow_data.get("action", "done")

    try:
        idx = int("".join(c for c in user_text if c.isdigit())) - 1
        if 0 <= idx < len(task_ids):
            task_id = task_ids[idx]
            await db.update_user_state(current_flow=None, flow_step=0, flow_data=None)

            if action == "done":
                await db.update_task_status(task_id, "done")
                tasks = await db.get_today_tasks()
                task = next((t for t in tasks if t["id"] == task_id), None)
                name = task["title"] if task else "태스크"
                await _send(update, context, f"✅ '{name}' 완료 기록했어요.")
            elif action == "defer":
                await db.increment_postpone(task_id)
                await _send(update, context, "알겠어요. 언제 할 수 있어요?")
            elif action == "fail":
                await db.increment_fail(task_id)
                await _send(update, context, "알겠어요. 뭐가 어려운지 알려주실 수 있어요?")
        else:
            await _send(update, context, "번호가 맞지 않아요. 다시 골라주세요.")
    except (ValueError, IndexError):
        await db.update_user_state(current_flow=None, flow_step=0, flow_data=None)
        await _send(update, context, "알겠어요, 취소할게요.")


# ==================== DND (실시간 + 플랜 재조정) ====================

async def _handle_dnd_add(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    start = data.get("start_time", "")
    end = data.get("end_time", "")
    reason = data.get("reason", "")

    if start and end:
        await db.add_dnd_slot(start, end, reason)

        # 플랜 재조정
        plan = await db.get_today_plan()
        if plan:
            await _regenerate_plan_silently()
            await _send(update, context,
                        f"🔕 {start}~{end} 방해금지 등록했어요. 그때까지 조용히 할게요.\n"
                        f"플랜도 재조정했어요.")
        else:
            await _send(update, context, f"🔕 {start}~{end} 방해금지 등록했어요. 그때까지 조용히 할게요.")
    else:
        reply = await generate_response(
            f"유저가 DND를 추가하려는데 시간 정보가 불분명: '{update.message.text}'. "
            f"시작/끝 시간을 물어봐. 예시: '14:00~16:00 수업'"
        )
        await _send(update, context, reply)


async def _regenerate_plan_silently():
    """플랜을 조용히 재생성 (DND 추가, 태스크 미루기 등에서 호출)"""
    try:
        tasks = await db.get_today_tasks()
        dnd_slots = await db.get_today_dnd()
        new_plan = await generate_plan(tasks, dnd_slots)
        if new_plan:
            await db.set_daily_plan(new_plan)
    except Exception as e:
        logger.error(f"플랜 재생성 실패: {e}")


# ==================== 긴급 일정 삽입 ====================

async def _handle_urgent_add(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """긴급 일정 삽입 — 기존 플랜을 뒤로 밀고 즉시 배치"""
    title = data.get("title", "")
    if not title:
        await db.update_user_state(
            current_flow="task_collect",
            flow_step=1,
            flow_data={"category": "긴급"}
        )
        await _send(update, context, "🚨 긴급 일정이요? 뭔지 알려주세요.")
        return

    # 바로 추가 + 플랜 재조정
    old_plan = await db.get_today_plan()
    task_id, new_plan = await add_task_and_replan(
        title=title,
        category="긴급",
        deadline=data.get("deadline"),
        estimated_minutes=data.get("estimated_minutes"),
    )

    tasks = await db.get_today_tasks()
    if old_plan and new_plan:
        change_text = format_plan_change(old_plan, new_plan, tasks)
        await _send(update, context,
                    f"🚨 긴급 추가: '{title}'\n\n{change_text}")
    else:
        await _send(update, context,
                    f"🚨 '{title}' 긴급 추가했어요. /plan으로 플랜 잡아볼까요?")


# ==================== 아이디어 볼트 ====================

async def _handle_idea(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    content = data.get("content", update.message.text)
    async with (await db.get_db()) as conn:
        await conn.execute("INSERT INTO ideas (content) VALUES (?)", (content,))
        await conn.commit()
    await _send(update, context,
                f"💡 아이디어 저장했어요: '{content[:50]}'\n나중에 태스크로 전환할 수 있어요.")


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """아이디어 목록 조회"""
    async with (await db.get_db()) as conn:
        cursor = await conn.execute(
            "SELECT * FROM ideas WHERE converted_to_task_id IS NULL ORDER BY id DESC LIMIT 20"
        )
        rows = await cursor.fetchall()

    if not rows:
        await _send(update, context, "저장된 아이디어가 없어요. '아이디어: ...'로 저장해보세요.")
        return

    lines = ["💡 아이디어 볼트:\n"]
    for r in rows:
        lines.append(f"  {r[0]}. {r[1][:60]}")
    lines.append("\n태스크로 전환하려면 '아이디어 N번 태스크로'라고 해주세요.")
    await _send(update, context, "\n".join(lines))


async def convert_idea_to_task(idea_id: int) -> int:
    """아이디어를 태스크로 전환"""
    async with (await db.get_db()) as conn:
        cursor = await conn.execute("SELECT content FROM ideas WHERE id = ?", (idea_id,))
        row = await cursor.fetchone()
        if not row:
            return -1
        content = row[0]
        task_id = await db.add_task(title=content)
        await conn.execute(
            "UPDATE ideas SET converted_to_task_id = ? WHERE id = ?",
            (task_id, idea_id)
        )
        await conn.commit()
        return task_id


# ==================== 루틴 관리 ====================

async def cmd_routines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """루틴 목록 조회"""
    import aiosqlite
    from chiro_bot.config import DB_PATH

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM routines ORDER BY day_of_week, start_time")
        rows = [dict(r) for r in await cursor.fetchall()]

    if not rows:
        await _send(update, context,
                    "등록된 루틴이 없어!\n"
                    "예: /add_routine 월 09:00 10:30 데이터베이스수업")
        return

    dow_names = ["월", "화", "수", "목", "금", "토", "일"]
    lines = ["📅 등록된 루틴:\n"]
    current_dow = -1
    for r in rows:
        if r["day_of_week"] != current_dow:
            current_dow = r["day_of_week"]
            lines.append(f"\n[{dow_names[current_dow]}]")
        lines.append(f"  {r['start_time']}~{r['end_time']}  {r['label']}")

    lines.append("\n추가: /add_routine 요일 시작 종료 이름")
    lines.append("삭제: /del_routine 번호")
    await _send(update, context, "\n".join(lines))


async def cmd_add_routine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """루틴 추가: /add_routine 월 09:00 10:30 수업이름"""
    args = context.args
    if not args or len(args) < 4:
        await _send(update, context,
                    "사용법: /add_routine 요일 시작시간 종료시간 이름\n"
                    "예: /add_routine 월 09:00 10:30 데이터베이스수업\n"
                    "요일: 월,화,수,목,금,토,일 또는 매일")
        return

    dow_map = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
    dow_str = args[0]
    start_time = args[1]
    end_time = args[2]
    label = " ".join(args[3:])

    if dow_str == "매일":
        for dow in range(7):
            await db.add_routine(dow, start_time, end_time, label)
        await _send(update, context, f"✅ 매일 {start_time}~{end_time} '{label}' 루틴 등록!")
    elif dow_str in dow_map:
        await db.add_routine(dow_map[dow_str], start_time, end_time, label)
        await _send(update, context, f"✅ {dow_str} {start_time}~{end_time} '{label}' 루틴 등록!")
    else:
        await _send(update, context, f"요일을 인식 못했어: '{dow_str}'\n월,화,수,목,금,토,일 또는 매일")


async def cmd_add_dnd_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """기본 DND 등록: /add_dnd 월 12:00 13:00 점심"""
    args = context.args
    if not args or len(args) < 3:
        await _send(update, context,
                    "사용법: /add_dnd 요일 시작시간 종료시간 [사유]\n"
                    "예: /add_dnd 매일 23:00 08:00 수면")
        return

    dow_map = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
    dow_str = args[0]
    start_time = args[1]
    end_time = args[2]
    label = " ".join(args[3:]) if len(args) > 3 else None

    if dow_str == "매일":
        await db.add_dnd_default(start_time, end_time, label, day_of_week=None)
        await _send(update, context, f"🔕 매일 {start_time}~{end_time} 기본 DND 등록!")
    elif dow_str in dow_map:
        await db.add_dnd_default(start_time, end_time, label, day_of_week=dow_map[dow_str])
        await _send(update, context, f"🔕 {dow_str} {start_time}~{end_time} 기본 DND 등록!")
    else:
        await _send(update, context, f"요일을 인식 못했어: '{dow_str}'")


# ==================== 패턴 조회 ====================

async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """행동 패턴 조회"""
    from chiro_bot.patterns import get_pattern_summary
    summary = await get_pattern_summary()
    await _send(update, context, f"📊 행동 패턴 분석:\n\n{summary}")


# ==================== 공통 ====================

async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """메시지 발송 + 로그"""
    await db.log_message("bot", text)
    await db.update_user_state(
        last_bot_message_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    )
    await update.message.reply_text(text)
