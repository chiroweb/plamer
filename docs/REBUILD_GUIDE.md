# CHIRO Bot 전면 재설계 가이드

> **이 문서의 목적**: 빌더에게 전달할 재설계 명세.
> 현재 코드의 근본적인 구조 결함을 수정하고, 기획서와 다이어그램대로 작동하는 봇을 만든다.
> 기획서(`CHIRO_Bot_상세기획서_v2.docx`)와 페르소나(`CHIRO_Bot_Persona.md`)를 반드시 함께 읽을 것.

---

## 0. 왜 재설계하는가

### 현재 구조의 근본 결함

현재 봇은 **AI에게 판단과 메시지 생성을 모두 위임**하는 구조다.

```
상황 → AI("알아서 판단해서 메시지 만들어") → 유저
```

이 구조에서 발생한 실제 문제들:
- 같은 메시지를 15분 안에 4번 반복 발송
- "마감이 오늘 16:50"인 태스크를 12시에 "마감이 지났다"고 알림
- "점심시간이야"(시작)를 "점심 완료! 🎉"로 해석
- 존재하지 않는 날짜(10월 30일)를 할루시네이션
- 시스템 프롬프트에 "이모지 최소한", "3줄 이내", "명령형 금지"라고 명시했지만 전부 무시
- 유저가 말하지 않은 시점에 갑자기 컨디션 질문

**원인**: GPT-4o-mini(또는 어떤 LLM이든)는 시스템 프롬프트의 "규칙"을 일관되게 따르지 않는다. 특히 부정형 지시("~하지 마")를 잘 안 따른다. 이건 프롬프트를 아무리 잘 써도 해결되지 않는 모델 자체의 한계다.

### 재설계 핵심 원칙

```
상황 → 코드(판단) → 템플릿(메시지 생성) → 유저
         ↑                    ↑
    다이어그램대로          페르소나대로
    확정된 로직            확정된 문장 패턴
```

**AI는 판단하지 않는다. 코드가 판단한다.**
**AI는 메시지를 만들지 않는다. 템플릿이 만든다.**
**AI가 개입하는 유일한 지점: 아침 확언 생성, 저녁 보고서 코멘트, 자연어 인텐트 파싱(유저 메시지 → 구조화된 데이터 변환).**

---

## 1. 아키텍처 변경

### 1.1 현재 vs 변경 후

```
[현재]
유저 메시지 → AI(자유 판단 + 자유 생성) → 유저

프로액티브 → AI("상황이야, 메시지 만들어") → 유저

[변경 후]
유저 메시지 → AI(인텐트 파싱만, JSON 반환) → 코드(라우팅) → 템플릿(메시지) → 유저

프로액티브 → 코드(7단계 캐스케이드) → 코드(메시지 유형 결정) → 템플릿(메시지) → 유저
```

### 1.2 파일 구조 변경

```
chiro_bot/
├── ai_client.py          # AI 호출 (인텐트 파싱 전용으로 축소)
├── config.py             # 설정 (변경 없음)
├── database.py           # DB (변경 없음)
├── handlers.py           # 리액티브 핸들러 (라우팅 로직 추가)
├── proactive.py          # 프로액티브 루프 (7단계 캐스케이드, AI 호출 제거)
├── planner.py            # 플래너 (변경 없음)
├── patterns.py           # 패턴 (변경 없음)
├── tools.py              # 도구 실행 (변경 없음)
├── templates.py          # [신규] 메시지 템플릿 엔진
├── escalation.py         # [신규] 에스컬레이션 톤 관리
└── morning_ai.py         # [신규] AI가 개입하는 유일한 지점 (확언, 보고서 코멘트)
```

---

## 2. 신규 파일: `templates.py` — 메시지 템플릿 엔진

이 파일이 봇의 "입"이다. 모든 유저 대면 메시지는 여기서 나온다.
AI가 아니라 코드가 메시지를 만든다.

### 2.1 설계 원칙

- 모든 메시지는 **변수 삽입형 템플릿**
- 페르소나 규칙이 템플릿 자체에 박혀 있으므로 AI가 위반할 수 없음
- "우리" 프레이밍, 질문형 종결, 수치 피드백 — 전부 템플릿에 하드코딩

### 2.2 구현 명세

```python
"""templates.py — 모든 유저 대면 메시지를 생성하는 엔진.
AI가 아닌 코드가 메시지를 만든다. 페르소나 규칙은 여기에 하드코딩된다."""

import random
from datetime import datetime

# ============================================================
# 아침
# ============================================================

def morning_greeting(affirmation: str) -> str:
    """아침 시작 메시지. affirmation은 AI가 생성한 오늘의 확언."""
    return f"좋은 아침이에요. 오늘의 한마디: {affirmation}\n오늘 뭐 하실 거예요?"

# ============================================================
# 태스크 수집 질문 (한 번에 하나만)
# ============================================================

def ask_duration(task_title: str) -> str:
    return f"{task_title} 얼마나 걸려요?"

def ask_deadline(task_title: str) -> str:
    return f"언제까지 해야 해요?"

def ask_start_time() -> str:
    return "언제 시작할 수 있어요?"

def ask_dnd() -> str:
    return "못 보는 시간대 있어요?"

def ask_clarify_vague_time() -> str:
    return "몇 시쯤이요? 대충이라도 괜찮아요."

def ask_clarify_vague_duration() -> str:
    return "대충 몇 시간 정도요?"

def ask_clarify_vague_deadline() -> str:
    return "오늘이요? 내일이요? 이번 주요?"

# ============================================================
# 플랜
# ============================================================

def show_plan(plan_slots: list) -> str:
    """플랜 공유. plan_slots = [{"start": "10:00", "end": "12:00", "title": "과제", "deadline": "오늘 자정"}, ...]"""
    lines = ["알겠어요. 오늘 플랜:"]
    for s in plan_slots:
        dl = f" (마감: {s['deadline']})" if s.get("deadline") else ""
        lines.append(f"{s['start']}~{s['end']} {s['title']}{dl}")
    lines.append("이대로 갈까요?")
    return "\n".join(lines)

def plan_confirmed() -> str:
    return "플랜 확정했어요."

# ============================================================
# 리마인더
# ============================================================

def start_reminder(task_title: str, planned_time: str) -> str:
    return f"{task_title} 시작할 시간이에요. 우리 {planned_time}에 하기로 했죠? 시작할 수 있어요?"

def start_reminder_advance(task_title: str, minutes: int) -> str:
    return f"{task_title} {minutes}분 후에 시작이에요. 준비됐어요?"

def progress_check() -> str:
    return "지금 어디까지 했어요? 대충이라도 괜찮아요."

def progress_check_clarify() -> str:
    return "거의 다가 몇 % 정도예요?"

# ============================================================
# 완료
# ============================================================

def task_completed(task_title: str, estimated_min: int, actual_min: int, next_task: str = None, next_time: str = None) -> str:
    """완료 피드백. 빈 칭찬 없이 수치만."""
    if actual_min < estimated_min:
        diff = f"예상 {estimated_min}분에 {actual_min}분이면 빠르네요."
    elif actual_min > estimated_min:
        diff = f"예상보다 {actual_min - estimated_min}분 더 걸렸어요."
    else:
        diff = f"예상 {estimated_min}분에 딱 맞췄어요."

    msg = f"{task_title} 완료. {diff}"
    if next_task and next_time:
        msg += f"\n다음은 {next_task}, {next_time} 시작이에요."
    elif not next_task:
        msg += "\n오늘 태스크 전부 끝났어요. 저녁에 리뷰 보내드릴게요."
    return msg

# ============================================================
# 미룸
# ============================================================

def postpone_response(count: int) -> str:
    """미룸 응답. count = 현재까지 미룬 횟수."""
    if count < 3:
        return "알겠어요. 그러면 몇 시에 시작할 수 있어요?"
    else:
        return None  # 3회 이상은 escalation.py가 처리

def postpone_pressure(count: int, task_title: str, deadline: str, remaining_hours: float, estimated_hours: float) -> str:
    """미룸 3회 이상 — 팩트 압박."""
    return (
        f"벌써 {count}번째 미룬 거예요. "
        f"{task_title} 마감까지 {remaining_hours:.0f}시간 남았고, "
        f"{estimated_hours:.0f}시간짜리 작업이에요. "
        f"지금 안 하면 시간이 없어요. 지금 시작할 수 있어요?"
    )

# ============================================================
# 못함
# ============================================================

def fail_response(count: int) -> str:
    if count < 3:
        return "알겠어요. 언제 다시 시도할 수 있어요?"
    else:
        return None  # 3회 이상은 태스크 분해 제안

def fail_decompose(task_title: str, sub_tasks: list) -> str:
    """못함 3회 이상 — 태스크 분해 제안. sub_tasks = [{"title": "...", "minutes": 30}, ...]"""
    lines = [f"이 태스크에서 3번 연속 못 한 거예요. 혹시 너무 큰 건 아닌가요? 이렇게 쪼개보면 어때요:"]
    for i, st in enumerate(sub_tasks, 1):
        lines.append(f"{i}) {st['title']} — {st['minutes']}분")
    lines.append(f"\n1번 '{sub_tasks[0]['title']}'부터 시작해볼까요? {sub_tasks[0]['minutes']}분이면 부담 적잖아요.")
    return "\n".join(lines)

# ============================================================
# 부분완료
# ============================================================

def partial_complete(progress: int, remaining_minutes: int) -> str:
    return f"{progress}% 기록할게요. 남은 부분은 약 {remaining_minutes}분이에요. 언제 다시 할 수 있어요?"

# ============================================================
# 에스컬레이션 (미응답)
# ============================================================

def escalation_message(level: int, task_context: str = None) -> str:
    """미응답 에스컬레이션. level = 0~3+"""
    if level == 0:
        return "혹시 메시지 못 보셨나요? 오늘 계획 같이 잡아요."
    elif level == 1:
        return "아직 답이 없네요. 지금 잠깐만 시간 내줄 수 있어요?"
    elif level == 2:
        if task_context:
            return f"지금 {task_context} 솔직히 말할게요, 지금 안 잡으면 나중에 더 몰려요."
        return "아직 답이 없어요. 1분만 써서 답 주세요."
    else:  # 3+
        return "솔직히 말할게요. 지금 4번째 알림이에요. 오늘 이걸 안 하면 내일은 더 힘들어져요. 지금 1분만 써서 답 주세요."

# ============================================================
# 마감 임박
# ============================================================

def deadline_urgent(task_title: str, remaining_min: int, estimated_min: int) -> str:
    if remaining_min <= 0:
        return f"{task_title} 마감이 이미 지났어요. 지금이라도 끝낼 수 있어요?"
    elif remaining_min <= estimated_min:
        return f"{task_title} 마감까지 {remaining_min}분 남았는데, 예상 소요가 {estimated_min}분이에요. 시간이 빠듯해요. 바로 시작할 수 있어요?"
    else:
        return f"{task_title} 마감까지 {remaining_min}분 남았어요. 지금 어디까지 했어요?"

# ============================================================
# DND
# ============================================================

def dnd_ask_end_time() -> str:
    return "알겠어요. 몇 시까지예요? 그때까지 조용히 할게요."

def dnd_ended(next_task: str = None) -> str:
    if next_task:
        return f"방해금지 시간 끝났어요. 다음은 {next_task}이에요. 시작할 수 있어요?"
    return "방해금지 시간 끝났어요."

# ============================================================
# 긴급 일정 삽입
# ============================================================

def emergency_insert(new_plan_slots: list) -> str:
    lines = ["알겠어요. 플랜 재조정했어요:"]
    for s in new_plan_slots:
        dl = f" (마감: {s['deadline']})" if s.get("deadline") else ""
        lines.append(f"{s['start']}~{s['end']} {s['title']}{dl}")
    lines.append("이대로 갈까요?")
    return "\n".join(lines)

# ============================================================
# 아이디어 캡처
# ============================================================

def idea_prompt() -> str:
    return "말해주세요, 적어둘게요."

def idea_saved(summary: str) -> str:
    return f"아이디어 저장했어요: '{summary}'. 나중에 태스크로 전환할 수 있어요. 더 있어요?"

# ============================================================
# 유저 감정 대응
# ============================================================

def user_frustrated(option_a: str, option_b: str) -> str:
    return f"제가 잔소리처럼 느껴질 수 있어요. 근데 저는 강요하는 게 아니라 선택지를 보여드리는 거예요. {option_a} 하거나, {option_b} 하거나. 어떻게 하고 싶어요?"

def user_wants_rest() -> str:
    return "알겠어요. 오늘은 플랜 없이 갈게요. 혹시 중간에 할 거 생기면 말해주세요."

def user_wants_break(rest_min: int, resume_time: str, end_time: str) -> str:
    return f"그럼요. {rest_min}분 쉬고 {resume_time} 시작이면 {end_time}에 끝나요. 그렇게 할까요?"

# ============================================================
# 저녁 리뷰
# ============================================================

def evening_review(tasks_summary: list, completion_rate: int, pattern_comment: str = None, ideas_count: int = 0) -> str:
    """저녁 리뷰. tasks_summary = [{"title": "...", "status": "done"/"failed"/..., "extra": "미룸 2회"}, ...]"""
    status_emoji = {"done": "✅", "failed": "❌", "partial": "🔄", "deferred": "⏸️", "pending": "⬜"}
    lines = ["오늘 리뷰예요."]
    for t in tasks_summary:
        emoji = status_emoji.get(t["status"], "⬜")
        extra = f" ({t['extra']})" if t.get("extra") else ""
        lines.append(f"{emoji} {t['title']} — {t['status_label']}{extra}")
    if ideas_count > 0:
        lines.append(f"💡 아이디어 {ideas_count}개 저장됨")
    lines.append(f"완료율: {completion_rate}%.")
    if pattern_comment:
        lines.append(pattern_comment)
    lines.append("내일 할 거 있으면 말해주세요.")
    return "\n".join(lines)

# ============================================================
# 취침
# ============================================================

def bedtime() -> str:
    return "잘 시간이에요. 핸드폰 끄고 푹 쉬세요."

# ============================================================
# 태스크 등록 확인 (간결하게)
# ============================================================

def task_added(task_title: str, deadline: str = None) -> str:
    if deadline:
        return f"{task_title} 잡았어요. 마감 {deadline}."
    return f"{task_title} 잡았어요."

def routine_added(label: str, time_range: str) -> str:
    return f"{label} 루틴 등록했어요. {time_range}."

def reminder_set(time: str, message: str) -> str:
    return f"{time}에 알려드릴게요."

# ============================================================
# 주말/빈 날
# ============================================================

def weekend_morning(day_name: str) -> str:
    return f"좋은 아침이에요. 오늘 {day_name}인데, 뭐 할 거 있어요? 없으면 없다고 해도 괜찮아요."

# ============================================================
# 헬스체크
# ============================================================

def healthcheck() -> str:
    return "💚 정상 가동 중."
```

---

## 3. `ai_client.py` 재설계 — AI 역할 축소

### 3.1 AI가 하는 것 (딱 3가지만)

1. **인텐트 파싱**: 유저 메시지 → 구조화된 JSON (어떤 의도인지만 판단)
2. **아침 확언 생성**: 매일 다른 한 줄 확언
3. **저녁 보고서 코멘트**: 패턴 기반 한 줄 코멘트

### 3.2 인텐트 파싱 프롬프트 (핵심)

```python
INTENT_PARSE_PROMPT = """너는 JSON 변환기야. 유저 메시지를 읽고, 의도를 JSON으로 반환해.
자연어 응답 절대 금지. 반드시 JSON만 반환해.

가능한 인텐트:
- add_task: 할 일 등록 {"intent": "add_task", "title": "...", "deadline": "...", "estimated_minutes": ...}
- complete_task: 완료 {"intent": "complete_task", "hint": "..."}
- defer_task: 미루기 {"intent": "defer_task", "hint": "...", "new_time": "..."}
- fail_task: 못함 {"intent": "fail_task", "hint": "..."}
- partial_complete: 부분완료 {"intent": "partial_complete", "hint": "...", "progress": 50}
- set_reminder: 알림 요청 {"intent": "set_reminder", "time": "HH:MM", "message": "..."}
- add_routine: 루틴 등록 {"intent": "add_routine", "routines": [...]}
- add_dnd: 방해금지 {"intent": "add_dnd", "start": "HH:MM", "end": "HH:MM", "reason": "..."}
- save_idea: 아이디어 {"intent": "save_idea", "content": "..."}
- ask_status: 현재 상태 질문 {"intent": "ask_status"}
- answer_question: 수집 질문에 대한 답변 {"intent": "answer_question", "field": "duration/deadline/start_time/dnd", "value": "..."}
- confirm: 확인/동의 {"intent": "confirm"}
- reject: 거부/수정 요청 {"intent": "reject", "reason": "..."}
- frustrated: 짜증/불만 {"intent": "frustrated"}
- want_rest: 쉬겠다 {"intent": "want_rest"}
- emergency: 긴급 일정 {"intent": "emergency", "title": "...", "duration_minutes": ...}
- chat: 일반 대화 {"intent": "chat", "topic": "..."}
- unclear: 판단 불가 {"intent": "unclear"}

현재 대화 상태: {current_state}
마지막 봇 질문: {last_bot_question}

유저 메시지를 보고 JSON 하나만 반환해. 설명 금지."""
```

이렇게 하면 AI는 "무슨 말인지"만 판단하고, "뭐라고 답할지"는 코드가 결정한다.

### 3.3 temperature

- 인텐트 파싱: `temperature=0.0` (창의성 불필요, 정확성만)
- 아침 확언: `temperature=0.8` (매일 다른 문장)
- 저녁 코멘트: `temperature=0.3` (사실 기반이되 약간의 변형)

### 3.4 아침 확언 프롬프트

```python
AFFIRMATION_PROMPT = """한 줄짜리 아침 확언을 만들어.
규칙: 빈 격려 금지("화이팅" 금지). 날카롭고 짧게. 20자 이내.
이모지 금지. 느낌표 최대 1개.
예시:
- "시작이 반이다는 거짓말이에요. 시작이 전부예요."
- "완벽하게 하려다 아무것도 안 하는 게 최악이에요."
- "어제보다 1% 나아지면 충분해요."
- "지금 안 하면 내일의 내가 고생해요."
한 줄만 반환해. 따옴표 없이."""
```

---

## 4. `handlers.py` 재설계 — 코드가 라우팅

### 4.1 흐름

```
유저 메시지 수신
    → AI 인텐트 파싱 (JSON 반환)
    → intent별 분기 (코드)
        → 도구 실행 (add_task, complete_task 등)
        → 템플릿으로 응답 메시지 생성
    → 텔레그램 발송
```

### 4.2 핵심 변경

```python
async def handle_message(update, context):
    user_text = update.message.text.strip()
    await db.reset_no_response()
    await db.log_message("user", user_text)

    # 1. AI에게 인텐트 파싱만 요청
    intent_json = await parse_intent(user_text)

    # 2. 인텐트별 코드 라우팅
    intent = intent_json.get("intent", "unclear")

    if intent == "add_task":
        result = await execute_tool("add_task", intent_json)
        reply = templates.task_added(intent_json["title"], intent_json.get("deadline"))

    elif intent == "complete_task":
        result = await execute_tool("complete_task", intent_json)
        # 수치 계산은 코드가 한다
        task = find_task(intent_json.get("hint"))
        next_task, next_time = get_next_task()
        reply = templates.task_completed(
            task["title"],
            task["estimated_minutes"],
            calculate_actual_minutes(task),
            next_task, next_time
        )

    elif intent == "defer_task":
        count = increment_postpone(intent_json.get("hint"))
        if count >= 3:
            reply = templates.postpone_pressure(count, task_title, deadline, remaining, estimated)
        else:
            reply = templates.postpone_response(count)

    elif intent == "confirm":
        # 마지막 봇 질문이 뭐였는지에 따라 분기
        last_q = get_last_bot_question_type()
        if last_q == "plan_confirm":
            await execute_tool("confirm_plan", {})
            reply = templates.plan_confirmed()
        elif last_q == "start_reminder":
            await db.update_task_status(current_task, "in_progress")
            reply = "좋아요."
        # ...

    elif intent == "frustrated":
        reply = templates.user_frustrated("지금 하", "내일로 넘기")

    elif intent == "want_rest":
        reply = templates.user_wants_rest()

    elif intent == "set_reminder":
        await execute_tool("set_reminder", intent_json)
        reply = templates.reminder_set(intent_json["time"], intent_json["message"])

    elif intent == "chat":
        # 일반 대화만 AI에게 자유 응답 허용 (유일한 예외)
        reply = await generate_chat_response(user_text)

    else:
        reply = "다시 말해줄 수 있어요?"

    await db.log_message("bot", reply)
    await update.message.reply_text(reply)
```

---

## 5. `proactive.py` 재설계 — 코드가 전부 판단

### 5.1 핵심 변경

- `generate_proactive_message()` 호출 전면 제거
- 모든 메시지는 `templates.py`에서 생성
- AI 호출 = 0 (아침 확언 제외)

### 5.2 7단계 캐스케이드 (기획서 기준, 순서 엄수)

```python
async def proactive_check():
    now = get_now()

    # Step 1: 헬스체크 (07:00 1회)
    if is_healthcheck_time(now):
        await send(templates.healthcheck())
        return

    # Step 2: 아침 시작 (MORNING_HOUR, 1회)
    if is_morning(now) and not morning_done_today():
        affirmation = await generate_affirmation()  # AI 유일한 개입
        await send(templates.morning_greeting(affirmation))
        mark_morning_done()
        return

    # Step 3: 저녁 리뷰 (EVENING_HOUR, 1회)
    if is_evening(now) and not evening_done_today():
        stats = await get_today_stats()
        await send(templates.evening_review(...))
        await archive_today()
        mark_evening_done()
        return

    # Step 3.5: 취침 (BEDTIME_HOUR, 1회)
    if is_bedtime(now) and not bedtime_done_today():
        await send(templates.bedtime())
        mark_bedtime_done()
        return

    # Step 4: DND 체크
    if await is_dnd_now():
        return  # 조용히 대기

    # Step 5: 미응답 에스컬레이션
    if has_unanswered_message():
        level = get_escalation_level()
        interval = get_interval_for_level(level)  # 30→20→10→5분
        if minutes_since_last_bot_message() >= interval:
            await send(templates.escalation_message(level))
            increment_escalation()
            return

    # Step 6: 마감 임박 체크
    urgent_task = get_most_urgent_task()
    if urgent_task:
        remaining = minutes_until_deadline(urgent_task)
        estimated = urgent_task["estimated_minutes"]
        await send(templates.deadline_urgent(
            urgent_task["title"], remaining, estimated
        ))
        return

    # Step 7: 시작 리마인더
    starting_task = get_task_starting_now()
    if starting_task:
        await send(templates.start_reminder(
            starting_task["title"], starting_task["planned_start"]
        ))
        return

    # Step 7.5: 진행 확인
    active_task = get_active_task()
    if active_task and should_check_progress(active_task):
        await send(templates.progress_check())
        return

    # 할 것 없음 → 아무것도 안 보냄
    return
```

### 5.3 절대 규칙

- **AI 호출 없음** (아침 확언 제외)
- **쿨다운 15분** 유지 (마감 임박만 예외)
- **중복 방지**: 같은 템플릿+같은 변수 = 같은 메시지 → 보내지 않음
- **컨디션 체크 없음**: Step 6.5 삭제. 유저가 직접 말할 때만 기록.

---

## 6. 리마인더 시스템 변경

### 6.1 현재 문제

리마인더가 태스크와 혼동됨. "11시 50분에 점심이라고 알려줘" → 태스크로 등록됨 → "점심 완료!" 같은 엉뚱한 처리 발생.

### 6.2 변경

- 리마인더 = 단순 알림. 별도 테이블 (`reminders`)
- 태스크 = 실행해야 할 일. 기존 `daily_tasks` 테이블
- 프로액티브 루프에서 리마인더 체크 추가:

```python
# Step 1.5: 리마인더 체크 (헬스체크 바로 다음)
due_reminders = get_due_reminders(now)
for r in due_reminders:
    await send(r["message"])  # 유저가 설정한 메시지 그대로
    mark_reminder_done(r["id"])
```

---

## 7. proactive.py 내부 프롬프트 전면 교체

현재 `proactive.py`에서 AI에게 보내는 모든 프롬프트를 제거한다.

### 삭제 대상 (전부)

```python
# 이것들 전부 삭제:
generate_proactive_message("시작하자!")
generate_proactive_message("지금 바로 끝내.")
generate_proactive_message("더 이상 미루면 안 돼.")
generate_proactive_message("집중해서 끝내자!")
# ... 등등
```

### 대체

전부 `templates.py`의 함수 호출로 대체. AI 개입 없음.

---

## 8. 에스컬레이션 간격 통일

기획서 기준으로 통일:

```python
ESCALATION_INTERVALS = {
    0: 30,   # 1단계: 부드러운 재알림
    1: 20,   # 2단계: 살짝 직접적
    2: 10,   # 3단계: 팩트 압박
    3: 5,    # 4단계: 단호한 직구
}
```

---

## 9. 체크리스트 — 빌드 완료 시 검증

빌드 후 아래 시나리오를 실제로 돌려서 검증할 것.

### 9.1 메시지 품질 체크

- [ ] 모든 봇 메시지에 😊 🎉 🍽️ 🌙 없는가?
- [ ] 모든 봇 메시지가 3줄 이내인가? (플랜 공유, 리뷰 제외)
- [ ] "등록되었어요!", "완료했습니다!", "도와드릴게요!" 같은 기계적 문구가 없는가?
- [ ] "잘했어요", "대단해요", "화이팅" 같은 빈 칭찬이 없는가?
- [ ] 모든 행동 유도가 질문형("~할 수 있어요?")인가?
- [ ] 합쇼체("~습니다")가 섞이지 않았는가?

### 9.2 로직 체크

- [ ] "점심에 알려줘" → 리마인더로 처리되는가? (태스크 아님)
- [ ] "끝났어" → 완료 처리 시 수치 피드백이 나오는가?
- [ ] "나중에 할게" → 미룸 카운터 증가 + 구체적 시간 재질문?
- [ ] 미룸 3회 → 팩트 압박 (마감/소요시간 수치 포함)?
- [ ] DND 시간대에 봇이 조용한가?
- [ ] 같은 메시지가 15분 안에 2번 나가지 않는가?
- [ ] 마감이 오늘 16:50인 태스크를 12시에 "마감 지났다"고 하지 않는가?

### 9.3 프로액티브 체크

- [ ] 아침 메시지가 정확히 1회만 나가는가?
- [ ] 저녁 리뷰가 정확히 1회만 나가는가?
- [ ] 유저 응답 후 에스컬레이션 카운터가 리셋되는가?
- [ ] 할 일 없을 때 봇이 아무것도 보내지 않는가?

---

## 10. 요약 — 한 문장

**AI에게 "알아서 해"라고 하지 말고, 다이어그램대로 코드가 판단하고, 페르소나대로 템플릿이 말하게 하라.**
