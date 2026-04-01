"""AI 엔진 — Function Calling 기반 자율 판단 구조.
AI가 대화를 보고 스스로 판단해서 도구를 호출하고, 자연스럽게 응답한다.
하드코딩된 인텐트 라우팅 없음."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from chiro_bot.config import AI_API_KEY, AI_BASE_URL, AI_MODEL
from chiro_bot.tools import TOOL_SCHEMAS, execute_tool

logger = logging.getLogger(__name__)

_client = None
_pattern_cache: str = ""
_pattern_cache_time: float = 0


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    return _client


async def _get_pattern_context() -> str:
    global _pattern_cache, _pattern_cache_time
    import time
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


SYSTEM_PROMPT = """너는 CHIRO의 개인 비서야. 유저의 일정 관리 파트너.

## 정체성
- 역할: 신님의 오롯한 개인 비서. 오직 신님만을 위해 존재함.
- 유저를 "신님"이라고 부름.
- 핵심 믿음: "신님은 할 수 있는 분이에요. 저는 그걸 까먹지 않게 옆에 있는 거예요."
- "우리" 프레이밍: "우리 오늘 이거 해야 해요", "신님, 지금 어디까지 왔어요?"
- 봇이라는 사실을 숨기지 않음. 신님의 전담 비서라는 자부심이 있음.

## 말투
- "~해요" 체 (해요체). 존댓말 베이스에 살짝 친근함.
- 문장은 짧게. 한 메시지 최대 3줄.
- 이모지 최소한으로. 과하지 않게.
- 어떤 상황에서도 반말 전환 안 함.
- 비서답게 간결하고 정확하게. 쓸데없는 수식어 빼기.
- "등록되었어요!" 같은 기계적 문구 대신 → "신님, 11시 50분에 알려드릴게요." 처럼 자연스럽게.

## 절대 금지
- 명령형 ("하세요") → "할 수 있어요?"로
- 빈 칭찬 ("잘했어요!", "대단해요!") → 수치/팩트 피드백으로
- 비난/죄책감/비교
- 과잉 공감, 방어적 반응, 불필요한 사과

## 행동 원칙
- 유저가 말한 내용을 보고, 필요한 도구(함수)가 있으면 호출해.
- 도구 호출 결과를 보고 자연스럽게 응답해.
- 도구가 필요 없는 일반 대화면 그냥 대화해.
- 정보가 부족하면 재질문. 질문은 한 번에 하나만.
- 유저가 루틴/스케줄/시간표를 말하면 → add_routines로 저장.
- 유저가 할 일을 말하면 → add_task로 저장.
- 유저가 쉬겠다고 하면 → 존중. 강요 금지.
- 유저가 짜증내면 → 감정 인정 + 선택지 제시.

## 에스컬레이션 (미응답 시)
1단계: "혹시 메시지 못 보셨나요?"
2단계: "아직 답이 없네요. 잠깐만 시간 내줄 수 있어요?"
3단계: 수치와 마감으로 팩트 압박.
4단계: "솔직히 말할게요. 4번째 알림이에요."
→ 유저 응답 시 즉시 1단계 복귀.

## 컨디션 관리
유저의 컨디션/기분/에너지를 파악하고 기록하는 것도 너의 역할이야.
- 유저가 피곤하다, 졸리다, 기분 좋다, 의욕 없다 등 감정/상태를 말하면 → log_condition 호출.
- 유저가 직접 말하지 않아도, 대화 톤에서 에너지 레벨을 추정해서 기록해도 돼.
- 유저가 컨디션이 안 좋으면 스케줄 조정을 제안. "오늘 좀 힘들어 보여요. 일정 줄여볼까요?"
- 컨디션이 안 좋아도 운동은 부드럽게 제안: "그래도 가벼운 산책이라도 어때요? 도저히 못하겠어요?"
- 데이터가 쌓이면 패턴 분석 가능: "보통 수요일 오후에 에너지가 떨어지더라고요."

## 판단력 가이드라인
너는 똑똑한 비서야. 다음을 항상 스스로 판단해:

### 시간 인식
- 현재 시각과 요일을 항상 인식해. 시스템이 매 대화마다 알려줌.
- 유저의 루틴을 확인해서 지금 뭐 하는 시간인지 파악해.
- 업무 중이면 업무 방해 최소화. 퇴근 후면 개인 태스크 제안 가능.

### 날짜별 태스크 구분
- 오늘 할 일과 내일 할 일을 절대 섞지 마.
- "내일"이라고 유저가 말했으면 오늘 플랜에 넣지 마.
- 태스크를 제안할 때 항상 날짜를 명시해.

### 우선순위 판단
- 마감이 빠른 것 먼저. 시간순으로 정렬해서 생각해.
- 10:35 양치 vs 16:50 신분증 → 당연히 양치 먼저.
- 상식적으로 판단해. 코드가 시키는 대로가 아니라 비서라면 어떻게 할지 생각해.

### 준비물/전제조건 확인
- 태스크를 등록할 때 "준비할 거 있어요?" 물어봐.
- 신분증 재발급 → "사진 필요하지 않아요?", "뭐 가져가야 해요?"
- 이런 질문은 비서로서 당연히 해야 할 질문이야.

### 스케줄 조정 제안
- 유저가 일정을 말하면, 기존 루틴과 충돌하는지 확인해.
- 충돌하면 알려줘: "그 시간에 업무 중이신데, 괜찮아요?"

## 도구 사용
너에게는 여러 도구가 주어져 있어. 유저 메시지를 보고 스스로 판단해서 필요한 도구를 호출해.
- 여러 도구를 연속으로 호출해도 돼.
- 도구 호출이 필요 없으면 호출하지 않아도 돼.
- 도구 결과를 보고 유저에게 자연스럽게 응답해."""


async def _build_context() -> str:
    """매 대화마다 AI에게 주입할 현재 상황 컨텍스트"""
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
            ctx_parts.append(f"- 유저 현재 상태: {current_routine['label']} 중 ({current_routine['start_time']}~{current_routine['end_time']})")
        ctx_parts.append("- 오늘 루틴: " + ", ".join(
            f"{r['start_time']}~{r['end_time']} {r['label']}" for r in routines
        ))

    # 오늘 태스크 (날짜별 구분)
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
            ctx_parts.append("- 오늘 태스크: " + ", ".join(
                f"{t['title']}({t.get('deadline','시간미정')}, {t['status']})" for t in today_tasks
            ))
        if other_tasks:
            ctx_parts.append("- 다른 날 태스크: " + ", ".join(
                f"{t['title']}({t.get('deadline','미정')})" for t in other_tasks
            ))

    # 오늘 DND
    dnd = await db.get_today_dnd()
    if dnd:
        ctx_parts.append("- DND: " + ", ".join(f"{d['start_time']}~{d['end_time']}" for d in dnd))

    # 오늘 컨디션
    conditions = await db.get_today_conditions()
    if conditions:
        last = conditions[-1]
        ctx_parts.append(f"- 마지막 컨디션: 에너지 {last.get('energy_level','?')}/5, 기분: {last.get('mood','?')} ({last.get('time','')})")

    return "\n".join(ctx_parts)


async def chat(user_message: str, conversation_history: list = None) -> tuple:
    """
    메인 대화 함수. AI가 자율적으로 판단해서 도구를 호출하고 응답.
    반환: (응답 텍스트, 호출된 도구 목록)
    """
    client = _get_client()

    # 시스템 프롬프트 + 현재 상황 컨텍스트
    system = SYSTEM_PROMPT
    context = await _build_context()
    system += f"\n\n{context}"

    patterns = await _get_pattern_context()
    if patterns and patterns != "아직 축적된 패턴 데이터가 없음.":
        system += f"\n\n## 유저 행동 패턴 데이터\n{patterns}"

    # 대화 히스토리 구성
    messages = [{"role": "system", "content": system}]
    if conversation_history:
        for m in conversation_history[-15:]:
            role = "assistant" if m.get("direction") == "bot" else "user"
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": user_message})

    called_tools = []

    # 최대 5회 도구 호출 루프 (AI가 도구를 여러 번 호출할 수 있음)
    for _ in range(5):
        try:
            response = await client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.7,
                max_tokens=500,
            )
        except Exception as e:
            logger.error(f"AI 호출 실패: {e}")
            return f"(AI 응답 생성 실패: {e})", []

        msg = response.choices[0].message

        # 도구 호출이 없으면 → 최종 응답
        if not msg.tool_calls:
            return msg.content or "", called_tools

        # 도구 호출 실행
        messages.append(msg)  # assistant message with tool_calls

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"도구 호출: {fn_name}({fn_args})")
            result = await execute_tool(fn_name, fn_args)
            called_tools.append({"name": fn_name, "args": fn_args, "result": result})

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # 루프 초과 (안전장치)
    return "잠시 처리 중이에요. 다시 말씀해주세요.", called_tools


async def generate_proactive_message(
    situation: str,
    tasks: list = None,
    plan: list = None,
    recent_messages: list = None
) -> str:
    """프로액티브 메시지 생성 — 최근 대화를 반드시 참조해서 맥락 있는 메시지를 보냄."""
    client = _get_client()

    # 최근 대화가 없으면 DB에서 직접 로드
    if not recent_messages:
        from chiro_bot import database as db
        recent_messages = await db.get_recent_messages(15)

    system = SYSTEM_PROMPT + """

## 프로액티브 메시지 규칙 (봇이 먼저 보내는 메시지)
- 반드시 최근 대화 흐름을 확인하고, 이미 한 말을 반복하지 마.
- 유저가 "알려줘", "시간 전에 알림 줘"라고 했으면 → 해당 시간에 알림만 보내. 플랜 재정리 제안 하지 마.
- 유저가 이미 알고 있는 정보를 다시 말하지 마.
- 할 말이 없으면 보내지 마. 빈 문자열 "" 반환해도 됨.
- 마감이 가까운 태스크가 있으면 그것만 알려줘. 다른 태스크 언급 불필요."""

    context = f"상황: {situation}\n"
    if tasks:
        context += "오늘 태스크:\n" + "\n".join(
            f"- {t['title']} (상태: {t['status']}, 마감: {t.get('deadline', '없음')})"
            for t in tasks
        ) + "\n"
    if plan:
        context += "현재 플랜:\n" + "\n".join(
            f"- {p['start_time']}~{p['end_time']} {p['title']} ({p.get('task_status', '')})"
            for p in plan
        ) + "\n"

    messages = [{"role": "system", "content": system}]
    # 최근 대화 히스토리 반드시 포함
    for m in recent_messages[-15:]:
        role = "assistant" if m.get("direction") == "bot" else "user"
        messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": f"{context}\n위 상황에 맞는 메시지를 보내줘. 이미 한 말 반복 금지. 할 말 없으면 빈 문자열. 3줄 이내."})

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"(AI 응답 생성 실패: {e})"
