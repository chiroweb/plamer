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
