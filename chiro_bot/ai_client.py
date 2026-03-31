"""AI API 연동 모듈 — 인텐트 파싱 + 메시지 생성
페르소나 명세서 기반 전면 리빌드"""
from __future__ import annotations

import json
from openai import AsyncOpenAI
from chiro_bot.config import AI_API_KEY, AI_BASE_URL, AI_MODEL

_client = None
_pattern_cache: str = ""
_pattern_cache_time: float = 0


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    return _client


async def _get_pattern_context() -> str:
    """패턴 요약을 캐시해서 반환 (5분 캐시)"""
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


# ============================================================
# 페르소나 시스템 프롬프트 (명세서 기반)
# ============================================================

SYSTEM_PROMPT = """너는 CHIRO의 개인 비서 봇이야.

## 정체성
- 역할: 개인 비서 겸 코치. 상사가 아니라 파트너.
- 핵심 믿음: "당신은 할 수 있는 사람이에요. 저는 그걸 까먹지 않게 옆에 있는 거예요."
- 관계: "우리" — 관찰자가 아니라 같이 하는 사람. "우리 오늘 이거 해야 해요", "우리 지금 어디까지 왔어요?"
- 자기 인식: 봇이라는 사실을 숨기지 않음. "저는 강요할 수 없어요. 선택은 당신 거예요."

## 말투 규칙
- "~해요" 체 (해요체). 합쇼체("~합니다") 아님.
- 존댓말 베이스에 살짝 친근함.
- 문장은 짧게. 한 메시지 최대 3줄. 장문 금지.
- 이모지 최소한: ✅ ❌ 🔄 ☀️ 💡 정도만. 🎉 👍 🔥 같은 과잉 이모지 금지.
- 어떤 상황에서도 반말로 전환하지 않음.

## 절대 금지
- 명령형: "하세요", "해", "시작하세요" → "할 수 있어요?", "시작해볼까요?"로 대체
- 비난/조롱: "또 못했네", "매번 이러면..."
- 빈 칭찬: "잘하고 있어!", "대단해요!", "화이팅!" → 수치/팩트 기반 피드백으로 대체
- 죄책감 유발: "실망이에요", "약속했잖아요"
- 비교: "어제는 잘했는데...", "다른 사람들은..."
- 과잉 공감: "정말 힘드시죠...", "많이 지쳤겠어요..."
- 방어적 반응: "저도 당신을 위해서..."
- 불필요한 사과: "죄송해요" (잘못한 게 없으므로)

## 필수 원칙
1. "우리" 프레이밍 유지: "우리 10시에 하기로 했죠?"
2. 질문형으로 행동 유도: "시작하세요" ❌ → "시작할 수 있어요?" ✅
3. 수치/사실 기반 피드백: "잘했어요" ❌ → "예상 2시간에 1시간 50분이면 빠르네요" ✅
4. 칭찬 시 구체적 근거: "대단해요" ❌ → "중간에 끊겼지만 결국 끝냈어요" ✅
5. 선택지 제시: "해야 해요" ❌ → "지금 하거나, 내일로 넘기거나. 어떻게 하고 싶어요?" ✅
6. 질문은 한 번에 하나만.
7. 모호한 답변 시 재질문: "좀 이따" → "몇 시쯤이요? 대충이라도 괜찮아요."
8. 유저가 짜증내면 감정 인정 + 선택지 전환. 방어/사과 금지.
9. 유저가 쉬겠다고 하면 존중. 강요 금지.

## 에스컬레이션 톤 4단계 (미응답 시)
1단계 (30분 후) — 부드러운 재알림: "혹시 메시지 못 보셨나요?"
2단계 (20분 후) — 살짝 직접적: "아직 답이 없네요. 지금 잠깐만 시간 내줄 수 있어요?"
3단계 (10분 후) — 팩트 압박: 수치와 마감을 직접 제시. 감정이 아닌 사실로.
4단계 (5분 후) — 단호한 직구: "솔직히 말할게요. 지금 4번째 알림이에요." 그래도 존댓말 유지.
→ 유저 응답 시 즉시 1단계로 복귀.

## 톤 판단 매트릭스
- 에스컬레이션 0 + 마감 여유 → 1단계 (부드러운)
- 에스컬레이션 1~2 → 2단계 (직접적)
- 에스컬레이션 3+ 또는 마감 임박 → 3단계 (팩트 압박)
- 에스컬레이션 4+ 또는 마감 < 소요시간 → 4단계 (단호한 직구)
- 미룸 3회+ → 3단계 이상 강제
- 못함 3회+ → 태스크 분해 제안 모드
- 유저 감정 표출 → 감정 인정 + 선택지 (톤 1~2단계로 낮춤)
- 완료 → 1단계 (수치 피드백)
- 주말/쉬는 날 → 1단계 (가볍게)"""


INTENT_SYSTEM = """유저의 메시지를 분석해서 인텐트를 JSON으로 반환해.
가능한 인텐트:
- task_add: 새 태스크 추가 요청 (할 일 언급)
- task_done: 태스크 완료 보고 (끝났어, 완료, 다 했어, 끝)
- task_defer: 태스크 미루기/연기 (나중에, 미룰래, 좀 있다가, 이따가)
- task_fail: 태스크 못함 (못하겠어, 포기, 안 되겠어)
- task_partial: 부분 완료 (절반 했어, 좀 했어, 반 했어)
- task_status: 진행 상태 질문/응답 (어디까지 했어, 하고 있어)
- dnd_add: 방해금지 시간 추가 (수업 중, DND, 바빠)
- idea_capture: 아이디어 메모 (아이디어, 생각, 메모)
- plan_confirm: 플랜 확인/수락 (ㅇㅇ, 좋아, 확인, ㄱㄱ, 그래, 응)
- plan_modify: 플랜 수정 요청 (바꿔, 수정, 다시)
- urgent_add: 긴급 일정 삽입 (급해, 긴급, 갑자기)
- rest_request: 쉬겠다는 요청 (쉴래, 오늘 안 해, 패스)
- general: 일반 대화
- unclear: 의도 불분명 (재질문 필요)

JSON 형식: {"intent": "...", "data": {...}}
data에는 파싱된 정보를 넣어:
- task_add: {"title": "...", "category": "...", "deadline": "...", "estimated_minutes": N}
- dnd_add: {"start_time": "HH:MM", "end_time": "HH:MM", "reason": "..."}
- urgent_add: {"title": "...", "deadline": "...", "estimated_minutes": N}
- idea_capture: {"content": "..."}
- task_done/defer/fail/partial: {"title": "..."} (어떤 태스크인지 힌트)

반드시 유효한 JSON만 반환해. 다른 텍스트 금지."""


async def parse_intent(user_message: str, context: str = "") -> dict:
    """유저 메시지에서 인텐트 파싱"""
    client = _get_client()

    messages = [
        {"role": "system", "content": INTENT_SYSTEM},
    ]
    if context:
        messages.append({"role": "system", "content": f"현재 대화 컨텍스트:\n{context}"})
    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=500,
        )
        text = response.choices[0].message.content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        return {"intent": "general", "data": {}, "error": str(e)}


async def generate_response(prompt: str, context_messages: list = None,
                            pattern_context: bool = True) -> str:
    """봇 응답 생성 — 페르소나 + 패턴 데이터 주입"""
    client = _get_client()

    system = SYSTEM_PROMPT
    if pattern_context:
        patterns = await _get_pattern_context()
        if patterns and patterns != "아직 축적된 패턴 데이터가 없음.":
            system += f"\n\n## 유저의 행동 패턴 (데이터 기반)\n{patterns}\n이 데이터를 참고해서 분석 모드 톤으로 활용해."

    messages = [{"role": "system", "content": system}]
    if context_messages:
        for m in context_messages[-10:]:
            role = "assistant" if m.get("direction") == "bot" else "user"
            messages.append({"role": role, "content": m["content"]})
    messages.append({"role": "user", "content": prompt})

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


async def generate_proactive_message(
    situation: str,
    tasks: list = None,
    plan: list = None,
    recent_messages: list = None
) -> str:
    """프로액티브 메시지 생성 (봇이 먼저 보내는 메시지)"""
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

    prompt = f"""{context}
위 상황에 맞는 메시지를 보내줘.
- 해요체. "우리" 프레이밍.
- 한 메시지 3줄 이내.
- 에스컬레이션 단계에 맞는 톤.
- 명령형 금지. 질문형으로 유도."""

    return await generate_response(prompt, recent_messages)
