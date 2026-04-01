"""AI가 호출할 수 있는 도구(함수) 정의.
OpenAI function calling 형식으로 AI에게 전달되고, AI가 판단해서 호출한다."""
from __future__ import annotations

import json
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from chiro_bot import database as db
from chiro_bot.config import TIMEZONE
from chiro_bot.planner import generate_plan, format_plan_text

logger = logging.getLogger(__name__)

# ============================================================
# 도구 스키마 (OpenAI function calling 형식)
# ============================================================

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "새 태스크를 등록한다. 유저가 할 일을 말하면 호출.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "태스크 제목"},
                    "category": {"type": "string", "description": "카테고리 (학업/과제/업무/개인/기타)"},
                    "deadline": {"type": "string", "description": "마감 (YYYY-MM-DD HH:MM 또는 자연어). 없으면 null"},
                    "estimated_minutes": {"type": "integer", "description": "예상 소요시간(분). 모르면 null"},
                    "preferred_start": {"type": "string", "description": "희망 시작시간 HH:MM. 없으면 null"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "태스크를 완료로 표시한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title_hint": {"type": "string", "description": "어떤 태스크인지 힌트 (제목 일부)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "defer_task",
            "description": "태스크를 미룬다. 미룸 횟수가 증가한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title_hint": {"type": "string", "description": "어떤 태스크인지 힌트"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fail_task",
            "description": "태스크를 못하겠다고 표시한다. 실패 횟수가 증가한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title_hint": {"type": "string", "description": "어떤 태스크인지 힌트"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_status",
            "description": "오늘의 태스크 목록, 플랜, 상태를 조회한다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "오늘의 태스크와 DND/루틴을 기반으로 플랜을 자동 생성한다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_plan",
            "description": "현재 생성된 플랜을 확정한다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_routines",
            "description": "고정 루틴(수업, 출퇴근 등)을 등록한다. 유저가 반복 일정을 말하면 호출. 여러 개를 한번에 등록 가능.",
            "parameters": {
                "type": "object",
                "properties": {
                    "routines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "days": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "요일 배열. 0=월, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일",
                                },
                                "start_time": {"type": "string", "description": "시작 HH:MM"},
                                "end_time": {"type": "string", "description": "종료 HH:MM"},
                                "label": {"type": "string", "description": "루틴 이름"},
                            },
                            "required": ["days", "start_time", "end_time", "label"],
                        },
                    },
                },
                "required": ["routines"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_dnd",
            "description": "방해금지(DND) 시간을 등록한다. 오늘 임시이면 today=true, 매주 고정이면 today=false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string", "description": "시작 HH:MM"},
                    "end_time": {"type": "string", "description": "종료 HH:MM"},
                    "reason": {"type": "string", "description": "사유"},
                    "today_only": {"type": "boolean", "description": "true=오늘만, false=매주 고정"},
                    "days": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "고정 DND일 때 요일 배열. today_only=false일 때만.",
                    },
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_idea",
            "description": "아이디어를 저장한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "아이디어 내용"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routines",
            "description": "등록된 루틴 목록을 조회한다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_condition",
            "description": "유저의 현재 컨디션/기분/에너지 상태를 기록한다. 유저가 기분, 컨디션, 피곤함, 졸림, 의욕 등을 말하면 호출. 유저가 뭐하고 있는지 말할 때도 활용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "energy_level": {"type": "integer", "description": "에너지 레벨 1~5 (1=매우 저조, 3=보통, 5=매우 좋음). AI가 대화에서 추정."},
                    "mood": {"type": "string", "description": "기분 키워드 (좋음/보통/피곤/짜증/우울/의욕넘침/졸림 등)"},
                    "activity": {"type": "string", "description": "지금 하고 있는 활동"},
                    "reason": {"type": "string", "description": "그 기분/컨디션의 이유 (유저가 말했으면)"},
                    "note": {"type": "string", "description": "추가 메모"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_condition_history",
            "description": "유저의 컨디션/기분 기록을 조회한다. 패턴 분석이나 유저가 자기 컨디션 이력을 물어볼 때.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "조회할 일수 (기본 7일)"},
                },
                "required": [],
            },
        },
    },
]

# ============================================================
# 도구 실행기
# ============================================================

async def execute_tool(name: str, args: dict) -> str:
    """도구를 실행하고 결과를 텍스트로 반환. AI가 이 결과를 보고 유저에게 응답."""
    try:
        if name == "add_task":
            task_id = await db.add_task(
                title=args["title"],
                category=args.get("category"),
                deadline=args.get("deadline"),
                estimated_minutes=args.get("estimated_minutes"),
                preferred_start=args.get("preferred_start"),
            )
            return json.dumps({
                "success": True,
                "task_id": task_id,
                "title": args["title"],
                "message": f"태스크 '{args['title']}' 등록 완료 (ID: {task_id})",
            }, ensure_ascii=False)

        elif name == "complete_task":
            tasks = await db.get_today_tasks()
            task = _find_task(tasks, args.get("task_title_hint", ""), ["in_progress", "pending"])
            if not task:
                return json.dumps({"success": False, "message": "완료할 태스크를 찾지 못했어요.", "tasks": [t["title"] for t in tasks]}, ensure_ascii=False)
            await db.update_task_status(task["id"], "done")
            remaining = [t for t in tasks if t["id"] != task["id"] and t["status"] in ("pending", "in_progress")]
            return json.dumps({
                "success": True,
                "completed": task["title"],
                "remaining_count": len(remaining),
                "remaining": [t["title"] for t in remaining[:5]],
            }, ensure_ascii=False)

        elif name == "defer_task":
            tasks = await db.get_today_tasks()
            task = _find_task(tasks, args.get("task_title_hint", ""), ["in_progress", "pending"])
            if not task:
                return json.dumps({"success": False, "message": "미룰 태스크를 찾지 못했어요."}, ensure_ascii=False)
            await db.increment_postpone(task["id"])
            count = task["postpone_count"] + 1
            return json.dumps({
                "success": True,
                "deferred": task["title"],
                "postpone_count": count,
                "deadline": task.get("deadline"),
            }, ensure_ascii=False)

        elif name == "fail_task":
            tasks = await db.get_today_tasks()
            task = _find_task(tasks, args.get("task_title_hint", ""), ["in_progress", "pending"])
            if not task:
                return json.dumps({"success": False, "message": "태스크를 찾지 못했어요."}, ensure_ascii=False)
            await db.increment_fail(task["id"])
            count = task["fail_count"] + 1
            return json.dumps({
                "success": True,
                "failed": task["title"],
                "fail_count": count,
            }, ensure_ascii=False)

        elif name == "get_today_status":
            tasks = await db.get_today_tasks()
            plan = await db.get_today_plan()
            dnd = await db.get_today_dnd()
            stats = await db.get_task_stats_today()
            now_hm = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
            return json.dumps({
                "current_time": now_hm,
                "stats": {k: v for k, v in stats.items() if k != "tasks"},
                "tasks": [{"title": t["title"], "status": t["status"],
                           "deadline": t.get("deadline"), "postpone_count": t["postpone_count"]}
                          for t in tasks],
                "plan": [{"start": p["start_time"], "end": p["end_time"],
                          "title": p["title"], "status": p.get("task_status", "")}
                         for p in plan],
                "dnd": [{"start": d["start_time"], "end": d["end_time"],
                         "reason": d.get("reason", "")} for d in dnd],
            }, ensure_ascii=False)

        elif name == "create_plan":
            tasks = await db.get_today_tasks()
            dnd = await db.get_today_dnd()
            plan_slots = await generate_plan(tasks, dnd)
            if not plan_slots:
                return json.dumps({"success": False, "message": "스케줄 가능한 태스크가 없어요."}, ensure_ascii=False)
            # 임시 저장 (confirm_plan에서 확정)
            await db.update_user_state(flow_data={"pending_plan": plan_slots})
            plan_text = format_plan_text(plan_slots, tasks)
            return json.dumps({"success": True, "plan_text": plan_text, "slot_count": len(plan_slots)}, ensure_ascii=False)

        elif name == "confirm_plan":
            state = await db.get_user_state()
            flow_data = state.get("flow_data") or {}
            plan_slots = flow_data.get("pending_plan", [])
            if not plan_slots:
                return json.dumps({"success": False, "message": "확정할 플랜이 없어요."}, ensure_ascii=False)
            await db.set_daily_plan(plan_slots)
            await db.update_user_state(flow_data=None)
            return json.dumps({"success": True, "message": "플랜 확정 완료."}, ensure_ascii=False)

        elif name == "add_routines":
            routines = args.get("routines", [])
            count = 0
            for r in routines:
                for day in r["days"]:
                    await db.add_routine(day, r["start_time"], r["end_time"], r["label"])
                    count += 1
            return json.dumps({
                "success": True,
                "registered_count": count,
                "routines": [r["label"] for r in routines],
                "message": f"루틴 {count}개 등록 완료.",
            }, ensure_ascii=False)

        elif name == "add_dnd":
            if args.get("today_only", True):
                await db.add_dnd_slot(args["start_time"], args["end_time"], args.get("reason", ""))
            else:
                days = args.get("days")
                if days:
                    for day in days:
                        await db.add_dnd_default(args["start_time"], args["end_time"], args.get("reason"), day_of_week=day)
                else:
                    await db.add_dnd_default(args["start_time"], args["end_time"], args.get("reason"), day_of_week=None)
            return json.dumps({"success": True, "message": f"DND {args['start_time']}~{args['end_time']} 등록."}, ensure_ascii=False)

        elif name == "save_idea":
            async with (await db.get_db()) as conn:
                await conn.execute("INSERT INTO ideas (content) VALUES (?)", (args["content"],))
                await conn.commit()
            return json.dumps({"success": True, "content": args["content"][:50]}, ensure_ascii=False)

        elif name == "get_routines":
            import aiosqlite
            from chiro_bot.config import DB_PATH
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("SELECT * FROM routines ORDER BY day_of_week, start_time")
                rows = [dict(r) for r in await cursor.fetchall()]
            dow_names = ["월", "화", "수", "목", "금", "토", "일"]
            formatted = []
            for r in rows:
                formatted.append(f"{dow_names[r['day_of_week']]} {r['start_time']}~{r['end_time']} {r['label']}")
            return json.dumps({"routines": formatted, "count": len(rows)}, ensure_ascii=False)

        elif name == "log_condition":
            now_hm = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
            await db.log_condition(
                time_hm=now_hm,
                energy_level=args.get("energy_level"),
                mood=args.get("mood"),
                activity=args.get("activity"),
                reason=args.get("reason"),
                note=args.get("note"),
            )
            today_conditions = await db.get_today_conditions()
            return json.dumps({
                "success": True,
                "logged_at": now_hm,
                "energy": args.get("energy_level"),
                "mood": args.get("mood"),
                "today_logs_count": len(today_conditions),
                "message": "컨디션 기록 완료.",
            }, ensure_ascii=False)

        elif name == "get_condition_history":
            days = args.get("days", 7)
            history = await db.get_condition_history(days)
            return json.dumps({
                "count": len(history),
                "logs": [
                    {"date": h["date"], "time": h["time"], "energy": h.get("energy_level"),
                     "mood": h.get("mood"), "activity": h.get("activity"), "reason": h.get("reason")}
                    for h in history[:30]
                ],
            }, ensure_ascii=False)

        else:
            return json.dumps({"error": f"알 수 없는 도구: {name}"}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"도구 실행 오류 [{name}]: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _find_task(tasks: list, hint: str, prefer_status: list) -> dict:
    """태스크 매칭"""
    if hint:
        for t in tasks:
            if hint.lower() in t["title"].lower():
                return t
    for status in prefer_status:
        matched = [t for t in tasks if t["status"] == status]
        if matched:
            return matched[0]
    return None
