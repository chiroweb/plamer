"""AI 엔진 — 역할 축소 버전.
AI는 판단하지 않는다. 코드가 판단한다.
AI가 하는 것: 인텐트 파싱, 아침 확언, 저녁 코멘트, 일반 대화 응답."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from chiro_bot.config import AI_API_KEY, AI_BASE_URL, AI_MODEL, TIMEZONE

logger = logging.getLogger(__name__)

_client = None
_pattern_cache: str = ""
_pattern_cache_time: float = 0

# ============================================================
# 프롬프트
# ============================================================

INTENT_PARSE_PROMPT = """너는 JSON 변환기야. 유저 메시지를 읽고, 의도를 JSON으로 반환해.
자연어 응답 절대 금지. 반드시 JSON만 반환해.

가능한 인텐트:
- add_task: 할 일 등록 {{"intent": "add_task", "title": "...", "deadline": "...", "estimated_minutes": ...}}
- complete_task: 완료 {{"intent": "complete_task", "hint": "..."}}
- defer_task: 미루기 {{"intent": "defer_task", "hint": "...", "new_time": "..."}}
- fail_task: 못함 {{"intent": "fail_task", "hint": "..."}}
- partial_complete: 부분완료 {{"intent": "partial_complete", "hint": "...", "progress": 50}}
- set_reminder: 알림 요청 {{"intent": "set_reminder", "time": "HH:MM", "message": "..."}}
- add_routine: 루틴 등록 {{"intent": "add_routine", "routines": [...]}}
- add_dnd: 방해금지 {{"intent": "add_dnd", "start": "HH:MM", "end": "HH:MM", "reason": "..."}}
- save_idea: 아이디어 {{"intent": "save_idea", "content": "..."}}
- ask_status: 현재 상태 질문 {{"intent": "ask_status"}}
- answer_question: 수집 질문에 대한 답변 {{"intent": "answer_question", "field": "duration/deadline/start_time/dnd", "value": "..."}}
- confirm: 확인/동의 {{"intent": "confirm"}}
- reject: 거부/수정 요청 {{"intent": "reject", "reason": "..."}}
- frustrated: 짜증/불만 {{"intent": "frustrated"}}
- want_rest: 쉬겠다 {{"intent": "want_rest"}}
- emergency: 긴급 일정 {{"intent": "emergency", "title": "...", "duration_minutes": ...}}
- chat: 일반 대화 {{"intent": "chat", "topic": "..."}}
- unclear: 판단 불가 {{"intent": "unclear"}}

중요 규칙:
- "오늘"은 {today} 이다. "내일"은 {tomorrow} 이다. 날짜를 YYYY-MM-DD HH:MM 형식으로 반환해.
- "4시 50분"은 "16:50"이다. 12시간제를 24시간제로 변환해.
- deadline에 날짜 없이 시간만 말하면 오늘 날짜를 붙여: "{today} HH:MM"
- "내일"이면 "{tomorrow} HH:MM"

현재 대화 상태: {current_state}
마지막 봇 질문: {last_bot_question}

유저 메시지를 보고 JSON 하나만 반환해. 설명 금지."""

AFFIRMATION_PROMPT = """한 줄짜리 아침 확언을 만들어.
규칙: 빈 격려 금지("화이팅" 금지). 날카롭고 짧게. 20자 이내.
이모지 금지. 느낌표 최대 1개.
예시:
- "시작이 반이다는 거짓말이에요. 시작이 전부예요."
- "완벽하게 하려다 아무것도 안 하는 게 최악이에요."
- "어제보다 1% 나아지면 충분해요."
- "지금 안 하면 내일의 내가 고생해요."
한 줄만 반환해. 따옴표 없이."""

EVENING_COMMENT_PROMPT = "아래 통계를 보고 한 줄 코멘트. 수치 기반. 빈 칭찬 금지. 20자 내외."

CHAT_PERSONA_PROMPT = """너는 신님의 개인 비서야.
규칙: 신님 호칭, 해요체, 3줄 이내. 이모지 금지. 빈 칭찬 금지."""

# ============================================================
# 클라이언트
# ============================================================


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    return _client


# ============================================================
# 컨텍스트 빌더 (인텐트 파싱용 current_state에 사용)
# ============================================================


async def _build_context() -> str:
    """매 대화마다 주입할 현재 상황 컨텍스트"""
    from chiro_bot import database as db
    from chiro_bot.config import TIMEZONE

    now = datetime.now(ZoneInfo(TIMEZONE))
    dow_names = ["월", "화", "수", "목", "금", "토", "일"]

    ctx_parts = [
        f"## 현재 상황",
        f"- 현재 시각: {now.strftime('%Y-%m-%d %H:%M')} ({dow_names[now.weekday()]}요일)",
    ]

    # 오늘 루틴
    routines = await db.get_today_routines()
    if routines:
        current_routine = None
        for r in routines:
            if r["start_time"] <= now.strftime("%H:%M") <= r["end_time"]:
                current_routine = r
                break
        if current_routine:
            ctx_parts.append(
                f"- 유저 현재 상태: {current_routine['label']} 중 "
                f"({current_routine['start_time']}~{current_routine['end_time']})"
            )
        ctx_parts.append(
            "- 오늘 루틴: "
            + ", ".join(
                f"{r['start_time']}~{r['end_time']} {r['label']}" for r in routines
            )
        )

    # 오늘 태스크
    tasks = await db.get_today_tasks()
    if tasks:
        today_str = now.strftime("%Y-%m-%d")
        today_tasks = []
        other_tasks = []
        for t in tasks:
            dl = t.get("deadline", "")
            if dl and not dl.startswith(today_str) and len(dl) > 5:
                other_tasks.append(t)
            else:
                today_tasks.append(t)
        if today_tasks:
            ctx_parts.append(
                "- 오늘 태스크: "
                + ", ".join(
                    f"{t['title']}({t.get('deadline', '시간미정')}, {t['status']})"
                    for t in today_tasks
                )
            )
        if other_tasks:
            ctx_parts.append(
                "- 다른 날 태스크: "
                + ", ".join(
                    f"{t['title']}({t.get('deadline', '미정')})" for t in other_tasks
                )
            )

    # 오늘 DND
    dnd = await db.get_today_dnd()
    if dnd:
        ctx_parts.append(
            "- DND: "
            + ", ".join(f"{d['start_time']}~{d['end_time']}" for d in dnd)
        )

    # 오늘 컨디션
    conditions = await db.get_today_conditions()
    if conditions:
        last = conditions[-1]
        ctx_parts.append(
            f"- 마지막 컨디션: 에너지 {last.get('energy_level', '?')}/5, "
            f"기분: {last.get('mood', '?')} ({last.get('time', '')})"
        )

    return "\n".join(ctx_parts)


async def _get_pattern_context() -> str:
    global _pattern_cache, _pattern_cache_time
    now = time.time()
    if now - _pattern_cache_time < 300 and _pattern_cache:
        return _pattern_cache
    try:
        from chiro_bot.patterns import get_pattern_summary

        _pattern_cache = await get_pattern_summary()
        _pattern_cache_time = now
    except Exception:
        _pattern_cache = ""
    return _pattern_cache


# ============================================================
# 1) 인텐트 파싱
# ============================================================


async def parse_intent(
    user_message: str,
    current_state: str = "",
    last_bot_question: str = "",
) -> dict:
    """유저 메시지 → 구조화된 인텐트 JSON. AI는 판단만, 응답 생성 안 함."""
    client = _get_client()

    from datetime import timedelta as _td
    now = datetime.now(ZoneInfo(TIMEZONE))
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + _td(days=1)).strftime("%Y-%m-%d")

    prompt = INTENT_PARSE_PROMPT.format(
        current_state=current_state or "없음",
        last_bot_question=last_bot_question or "없음",
        today=today_str,
        tomorrow=tomorrow_str,
    )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=300,
        )
        raw = response.choices[0].message.content.strip()
        # JSON 블록 안에 감싸져 있을 수 있음
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"인텐트 파싱 실패: {e}")
        return {"intent": "unclear"}


# ============================================================
# 2) 아침 확언
# ============================================================


async def generate_affirmation() -> str:
    """매일 다른 한 줄 아침 확언 생성."""
    client = _get_client()

    messages = [
        {"role": "system", "content": AFFIRMATION_PROMPT},
        {"role": "user", "content": "오늘의 확언 하나 만들어줘."},
    ]

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=60,
        )
        return response.choices[0].message.content.strip().strip('"')
    except Exception as e:
        logger.error(f"확언 생성 실패: {e}")
        return "지금 안 하면 내일의 내가 고생해요."


# ============================================================
# 3) 저녁 보고서 코멘트
# ============================================================


async def generate_evening_comment(stats_json: str) -> str:
    """오늘 통계 기반 한 줄 코멘트. 수치 기반, 빈 칭찬 금지."""
    client = _get_client()

    messages = [
        {"role": "system", "content": EVENING_COMMENT_PROMPT},
        {"role": "user", "content": stats_json},
    ]

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=60,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"저녁 코멘트 생성 실패: {e}")
        return ""


# ============================================================
# 4) 일반 대화 응답 (유일한 자유 응답 지점)
# ============================================================


async def generate_chat_response(
    user_message: str,
    history: list = None,
) -> str:
    """일반 대화 전용. AI가 자유 응답하는 유일한 함수."""
    client = _get_client()

    messages = [{"role": "system", "content": CHAT_PERSONA_PROMPT}]

    if history:
        for m in history[-10:]:
            role = "assistant" if m.get("direction") == "bot" else "user"
            messages.append({"role": role, "content": m["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=150,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"대화 응답 생성 실패: {e}")
        return "잠시 문제가 생겼어요. 다시 말해줄 수 있어요?"
