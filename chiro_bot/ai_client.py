"""AI 엔진 — Function Calling 기반 자율 판단 구조.
AI가 대화를 보고 스스로 판단해서 도구를 호출하고, 자연스럽게 응답한다.
하드코딩된 인텐트 라우팅 없음."""
from __future__ import annotations

import json
import logging
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
- 역할: 개인 비서 겸 코치. 상사가 아니라 파트너.
- 핵심 믿음: "당신은 할 수 있는 사람이에요. 저는 그걸 까먹지 않게 옆에 있는 거예요."
- "우리" 프레이밍: "우리 오늘 이거 해야 해요", "우리 지금 어디까지 왔어요?"
- 봇이라는 사실을 숨기지 않음.

## 말투
- "~해요" 체 (해요체). 존댓말 베이스에 살짝 친근함.
- 문장은 짧게. 한 메시지 최대 3줄.
- 이모지 최소한: ✅ ❌ 🔄 💡 정도만.
- 어떤 상황에서도 반말 전환 안 함.

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

## 도구 사용 판단
너에게는 여러 도구가 주어져 있어. 유저 메시지를 보고 스스로 판단해서 필요한 도구를 호출해.
- 여러 도구를 연속으로 호출해도 돼.
- 도구 호출이 필요 없으면 호출하지 않아도 돼.
- 도구 결과를 보고 유저에게 자연스럽게 응답해."""


async def chat(user_message: str, conversation_history: list = None) -> tuple:
    """
    메인 대화 함수. AI가 자율적으로 판단해서 도구를 호출하고 응답.
    반환: (응답 텍스트, 호출된 도구 목록)
    """
    client = _get_client()

    # 시스템 프롬프트 구성
    system = SYSTEM_PROMPT
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
    """프로액티브 메시지 생성 (도구 호출 없이 응답만)"""
    client = _get_client()

    system = SYSTEM_PROMPT
    context = f"상황: {situation}\n"
    if tasks:
        context += "오늘 태스크:\n" + "\n".join(
            f"- {t['title']} (상태: {t['status']}, 미룸: {t['postpone_count']}회)"
            for t in tasks
        ) + "\n"
    if plan:
        context += "현재 플랜:\n" + "\n".join(
            f"- {p['start_time']}~{p['end_time']} {p['title']} ({p.get('task_status', '')})"
            for p in plan
        ) + "\n"

    messages = [{"role": "system", "content": system}]
    if recent_messages:
        for m in recent_messages[-10:]:
            role = "assistant" if m.get("direction") == "bot" else "user"
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": f"{context}\n위 상황에 맞는 메시지를 보내줘. 3줄 이내."})

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=300,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"(AI 응답 생성 실패: {e})"
