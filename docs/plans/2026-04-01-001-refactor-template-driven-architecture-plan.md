---
title: "refactor: AI 자율 판단 → 코드 판단 + 템플릿 메시지 아키텍처 전환"
type: refactor
status: active
date: 2026-04-01
origin: docs/REBUILD_GUIDE.md
---

# AI 자율 판단 → 코드 판단 + 템플릿 메시지 아키텍처 전환

## Overview

현재 봇은 AI에게 판단과 메시지 생성을 모두 위임하는 구조.
AI가 같은 말 반복, 마감 오판, 이모지 과다, 명령형 사용 등 페르소나를 일관되게 지키지 못함.
**코드가 판단하고, 템플릿이 말하게** 전환한다.

## Problem Frame

GPT-4o-mini는 시스템 프롬프트의 "규칙"을 일관되게 따르지 않는다. 부정형 지시("~하지 마")를 특히 잘 안 따른다.
이건 프롬프트를 아무리 잘 써도 해결되지 않는 모델 자체의 한계다.
(see origin: docs/REBUILD_GUIDE.md 섹션 0)

## Requirements Trace

- R1. 모든 봇 메시지가 페르소나 규칙(해요체, 3줄, 이모지 최소, 명령형 금지)을 100% 지킬 것
- R2. AI API 호출을 최소화할 것 (인텐트 파싱 + 아침 확언 + 저녁 코멘트만)
- R3. 프로액티브 루프에서 AI 호출 0 (아침 확언 제외)
- R4. 리마인더와 태스크가 명확히 분리될 것
- R5. 에스컬레이션 간격이 기획서 기준(30→20→10→5분)으로 통일될 것
- R6. 아침/저녁/취침 메시지가 각각 1일 1회만 발송될 것

## Scope Boundaries

- database.py: 구조 변경 없음 (reminders 테이블은 이미 존재)
- planner.py: 변경 없음
- patterns.py: 변경 없음
- config.py: BEDTIME_HOUR 이미 존재, 변경 없음
- tools.py: execute_tool() 유지, TOOL_SCHEMAS는 더 이상 AI에 전달 안 함

## Key Technical Decisions

- **AI function calling 제거**: 현재 chat()의 tools/tool_choice 파라미터 삭제. AI는 JSON 텍스트만 반환.
- **templates.py 신규**: 모든 유저 대면 메시지가 여기서 생성됨. 페르소나가 코드에 박힘.
- **handlers.py**: AI 응답 생성 대신, 인텐트 파싱 → 코드 라우팅 → 템플릿 응답으로 전환.
- **proactive.py**: generate_proactive_message() 전면 제거. 모든 메시지를 templates에서 생성.
- **일반 대화(chat intent)만 AI 자유 응답 허용**: 이것이 유일한 예외.

## Open Questions

### Resolved During Planning

- AI function calling vs 인텐트 파싱: 인텐트 파싱으로 결정. function calling은 AI가 도구 실행 시점을 판단하므로 예측 불가능. 인텐트 파싱은 코드가 실행을 결정.
- 일반 대화 시 AI 응답 허용 범위: 페르소나 프롬프트를 축소한 chat-only 프롬프트로 별도 관리.

### Deferred to Implementation

- 모호한 인텐트 fallback 전략: "unclear" 인텐트 시 재질문 vs AI chat 모드 전환은 실제 테스트에서 결정.
- 태스크 수집 5단계의 상태 머신 세부 구현: handlers.py에서 flow_step 로직 구현 시 결정.

## Implementation Units

- [ ] **Unit 1: templates.py 생성**

**Goal:** 모든 유저 대면 메시지를 생성하는 템플릿 엔진. 페르소나 규칙이 코드에 하드코딩됨.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Create: `chiro_bot/templates.py`

**Approach:**
- REBUILD_GUIDE.md 섹션 2의 전체 코드를 기반으로 생성
- 모든 함수가 순수 문자열 반환 (AI 호출 없음)
- "신님" 호칭은 templates에 넣지 않음 — AI가 일반대화에서 부르므로, 프로액티브 템플릿에서만 선택적 사용

**Verification:**
- 모든 템플릿 출력이 해요체, 3줄 이내, 질문형 종결인지 확인

---

- [ ] **Unit 2: ai_client.py 재설계 — 인텐트 파싱 전용**

**Goal:** AI 역할을 인텐트 파싱 + 아침 확언 + 저녁 코멘트 + 일반대화 응답 4가지로 축소.

**Requirements:** R2

**Dependencies:** None

**Files:**
- Modify: `chiro_bot/ai_client.py`

**Approach:**
- `chat()` 함수 삭제
- `parse_intent(user_message, current_state, last_bot_question)` → JSON dict 반환
- `generate_affirmation()` → 한 줄 확언
- `generate_evening_comment(stats_data)` → 패턴 기반 한 줄 코멘트
- `generate_chat_response(user_message, history)` → 일반대화 전용 (페르소나 축소 프롬프트)
- function calling 관련 코드 전면 삭제 (TOOL_SCHEMAS import, tools 파라미터 등)
- 인텐트 파싱: temperature=0.0, 나머지: 목적별 temperature
- `_build_context()` 유지 — 인텐트 파싱 시 current_state에 활용

**Verification:**
- parse_intent가 항상 유효한 JSON dict를 반환하는지 확인
- function calling 관련 코드가 완전히 제거되었는지 확인

---

- [ ] **Unit 3: handlers.py 재설계 — 코드 라우팅**

**Goal:** AI 응답 생성 대신, 인텐트 파싱 → 코드 분기 → 도구 실행 → 템플릿 응답 구조로 전환.

**Requirements:** R1, R4

**Dependencies:** Unit 1 (templates.py), Unit 2 (ai_client.py)

**Files:**
- Modify: `chiro_bot/handlers.py`

**Approach:**
- handle_message: parse_intent → intent별 if/elif → execute_tool → templates.xxx()
- 태스크 수집 5단계 상태 머신: user_state.current_flow + flow_step으로 관리
- "confirm" 인텐트: last_bot_question_type에 따라 분기 (플랜 확인, 시작 확인 등)
- "chat" 인텐트: generate_chat_response()로 AI 자유 응답 (유일한 예외)
- "set_reminder" 인텐트: execute_tool("set_reminder") → templates.reminder_set()
- "frustrated" / "want_rest" 인텐트: templates 직접 호출
- 에러 핸들링: try/except로 감싸서 크래시 방지

**Verification:**
- "점심에 알려줘" → set_reminder 도구 실행 + templates.reminder_set() 응답
- "과제 해야 해" → add_task 도구 실행 + templates.task_added() 응답
- "끝났어" → complete_task + templates.task_completed() (수치 포함)

---

- [ ] **Unit 4: proactive.py 재설계 — AI 호출 제거**

**Goal:** 프로액티브 루프의 모든 메시지를 templates.py에서 생성. generate_proactive_message() 전면 제거.

**Requirements:** R3, R5, R6

**Dependencies:** Unit 1 (templates.py)

**Files:**
- Modify: `chiro_bot/proactive.py`

**Approach:**
- 7단계 캐스케이드 순서: 헬스체크 → 리마인더 → 아침(1회) → 저녁리뷰(1회) → 취침(1회) → DND → 미응답 에스컬레이션 → 마감 알림 → 시작 리마인더 → 진행 확인
- 아침/저녁/취침: "오늘 발송 완료" 플래그로 1일 1회 보장 (user_state 또는 별도 플래그)
- generate_proactive_message() import 및 호출 전면 삭제
- 아침 확언만 generate_affirmation() 사용 (AI 유일한 개입)
- 에스컬레이션 간격: {0: 30, 1: 20, 2: 10, 3: 5}
- 쿨다운 15분 유지 (마감 임박만 예외)
- 리마인더 체크: reminders 테이블에서 time <= now_hm AND sent=0 찾아서 발송

**Verification:**
- 아침 메시지 1일 1회만 발송되는지
- 프로액티브에서 AI API 호출이 아침 확언 외 0인지
- 에스컬레이션 간격이 30→20→10→5분인지

---

- [ ] **Unit 5: 정리 및 검증**

**Goal:** 불필요한 코드 제거, 전체 검증.

**Requirements:** R1~R6

**Dependencies:** Unit 1~4

**Files:**
- Modify: `chiro_bot/tools.py` (TOOL_SCHEMAS에서 AI 전달용 스키마 정리 — execute_tool은 유지)
- Delete import: `generate_proactive_message` 잔여 참조 제거

**Approach:**
- tools.py: TOOL_SCHEMAS는 더 이상 AI에 전달 안 하므로 정리 가능. execute_tool()은 handlers.py에서 직접 호출.
- REBUILD_GUIDE.md 섹션 9 체크리스트 전체 검증
- 서버 배포 + 텔레그램 실제 테스트

**Verification:**
- REBUILD_GUIDE.md 체크리스트 9.1, 9.2, 9.3 전체 통과

## System-Wide Impact

- **AI API 호출**: 현재 매 메시지마다 1~5회 → 변경 후 인텐트 파싱 1회 (+ 일반대화 시 1회). 프로액티브는 아침 1회만.
- **메시지 일관성**: 템플릿으로 100% 보장. AI 할루시네이션 불가.
- **응답 속도**: AI 다중 호출 제거로 응답 시간 50%+ 단축 예상.
- **비용**: API 호출 70~80% 감소 예상.

## Risks & Dependencies

- **일반대화(chat) 품질**: AI 자유 응답 허용 시 여전히 페르소나 이탈 가능. chat-only 프롬프트를 최소화하고, 길이 제한(max_tokens=150)으로 방어.
- **인텐트 파싱 정확도**: GPT-4o-mini가 인텐트를 잘못 파싱하면 엉뚱한 분기 탈 수 있음. "unclear" fallback과 재질문으로 방어.
- **태스크 수집 상태 머신 복잡도**: 5단계 질문 흐름을 handlers.py에서 관리해야 함. user_state.flow_step으로 구현하되, 복잡해지면 별도 파일 분리.

## Sources & References

- **Origin document:** [docs/REBUILD_GUIDE.md](docs/REBUILD_GUIDE.md)
- 기획서: CHIRO_Bot_상세기획서_v2.md
- 현재 코드: chiro_bot/ (2300줄)
