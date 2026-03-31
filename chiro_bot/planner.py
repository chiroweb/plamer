"""플랜 생성 로직 — 태스크 기반 일간 스케줄 자동 생성
Phase 3: 긴급 일정 삽입, 중간 태스크 추가 + 우선순위 재계산, 루틴 충돌 체크, 패턴 피드백 반영"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from zoneinfo import ZoneInfo
from chiro_bot.config import TIMEZONE
from chiro_bot import database as db


def _hm_to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _overlaps(start: int, end: int, blocks: List[Tuple[int, int]]) -> bool:
    for bs, be in blocks:
        if start < be and end > bs:
            return True
    return False


def _find_slot(duration: int, earliest: int, blocked: List[Tuple[int, int]], latest: int = 1380) -> Optional[int]:
    """duration분짜리 슬롯을 earliest부터 찾음. blocked를 피해서."""
    cursor = earliest
    while cursor + duration <= latest:
        if not _overlaps(cursor, cursor + duration, blocked):
            return cursor
        jumped = False
        for bs, be in sorted(blocked):
            if bs <= cursor < be:
                cursor = be
                jumped = True
                break
            elif cursor < bs < cursor + duration:
                cursor = be
                jumped = True
                break
        if not jumped:
            cursor += 5
    return None


def _calculate_priority(task: dict, now: datetime, patterns: dict = None) -> float:
    """태스크 우선순위 점수 계산 (낮을수록 먼저)"""
    score = 0.0

    # 1. 마감 긴급도 (가장 중요)
    if task.get("deadline"):
        try:
            dl_str = task["deadline"]
            if len(dl_str) <= 5:
                dl = datetime.combine(now.date(),
                                      datetime.strptime(dl_str, "%H:%M").time(),
                                      tzinfo=ZoneInfo(TIMEZONE))
            else:
                dl = datetime.fromisoformat(dl_str)
                if dl.tzinfo is None:
                    dl = dl.replace(tzinfo=ZoneInfo(TIMEZONE))
            hours_left = (dl - now).total_seconds() / 3600
            if hours_left <= 0:
                score -= 1000  # 마감 초과 → 최우선
            elif hours_left <= 2:
                score -= 500
            elif hours_left <= 6:
                score -= 200
            else:
                score -= 100 / max(hours_left, 1)
        except (ValueError, TypeError):
            pass
    else:
        score += 50  # 마감 없으면 후순위

    # 2. 긴급 태스크 가산
    if task.get("category") == "긴급":
        score -= 800

    # 3. 미룸 횟수가 많으면 우선순위 상승
    score -= task.get("postpone_count", 0) * 30

    # 4. 패턴 반영 (미루는 카테고리는 일찍 배치)
    if patterns and task.get("category"):
        cat_defer_rate = patterns.get(f"category_defer_{task['category']}", 0)
        if cat_defer_rate > 0.5:
            score -= 20  # 잘 미루는 카테고리 → 일찍

    return score


async def generate_plan(
    tasks: List[dict] = None,
    dnd_slots: List[dict] = None,
    routines: List[dict] = None,
    patterns: dict = None,
) -> List[dict]:
    """태스크 목록으로 일간 플랜 생성. 반환: [{task_id, start_time, end_time}, ...]"""
    if tasks is None:
        tasks = await db.get_today_tasks()
    if dnd_slots is None:
        dnd_slots = await db.get_today_dnd()
    if routines is None:
        routines = await db.get_today_routines()

    # 패턴 로드
    if patterns is None:
        patterns = await _load_patterns()

    # pending/deferred 태스크만 플래닝
    plannable = [t for t in tasks if t["status"] in ("pending", "deferred")]
    if not plannable:
        return []

    now = datetime.now(ZoneInfo(TIMEZONE))

    # 우선순위 정렬
    plannable.sort(key=lambda t: _calculate_priority(t, now, patterns))

    # 차단 블록: DND + 루틴
    blocked: List[Tuple[int, int]] = []
    for s in dnd_slots:
        blocked.append((_hm_to_minutes(s["start_time"]), _hm_to_minutes(s["end_time"])))
    for r in routines:
        blocked.append((_hm_to_minutes(r["start_time"]), _hm_to_minutes(r["end_time"])))

    # 현재 시각 이후부터 스케줄 (15분 단위 반올림)
    now_minutes = now.hour * 60 + now.minute
    earliest = ((now_minutes + 14) // 15) * 15

    plan_slots = []
    for t in plannable:
        duration = t.get("estimated_minutes") or 60

        start_from = earliest
        if t.get("preferred_start"):
            pref = _hm_to_minutes(t["preferred_start"])
            if pref >= earliest:
                start_from = pref

        slot_start = _find_slot(duration, start_from, blocked)
        if slot_start is None:
            slot_start = _find_slot(duration, earliest, blocked)
        if slot_start is None:
            continue

        slot_end = slot_start + duration
        plan_slots.append({
            "task_id": t["id"],
            "start_time": _minutes_to_hm(slot_start),
            "end_time": _minutes_to_hm(slot_end),
        })
        blocked.append((slot_start, slot_end + 10))
        earliest = max(earliest, slot_end + 10)

    return plan_slots


async def insert_urgent_task(task_id: int) -> List[dict]:
    """긴급 일정 삽입 — 기존 플랜을 뒤로 밀고 긴급 태스크를 즉시 배치"""
    tasks = await db.get_today_tasks()
    urgent = next((t for t in tasks if t["id"] == task_id), None)
    if not urgent:
        return []

    # 긴급 태스크를 카테고리 "긴급"으로 마킹 (우선순위 계산에서 최우선)
    # generate_plan이 우선순위대로 재배치하므로, 전체 재생성
    dnd_slots = await db.get_today_dnd()
    routines = await db.get_today_routines()

    new_plan = await generate_plan(tasks, dnd_slots, routines)
    if new_plan:
        await db.set_daily_plan(new_plan)
    return new_plan


async def add_task_and_replan(title: str, category: str = None, deadline: str = None,
                              estimated_minutes: int = None, preferred_start: str = None) -> Tuple[int, List[dict]]:
    """중간 태스크 추가 + 플랜 재조정. 반환: (task_id, new_plan)"""
    task_id = await db.add_task(title, category, deadline, estimated_minutes, preferred_start)
    tasks = await db.get_today_tasks()
    dnd_slots = await db.get_today_dnd()
    routines = await db.get_today_routines()

    new_plan = await generate_plan(tasks, dnd_slots, routines)
    if new_plan:
        await db.set_daily_plan(new_plan)
    return task_id, new_plan


def format_plan_text(plan: List[dict], tasks: List[dict]) -> str:
    """플랜을 사람이 읽을 수 있는 텍스트로 변환"""
    task_map = {t["id"]: t for t in tasks}
    lines = ["📋 오늘의 플랜:\n"]
    for i, slot in enumerate(plan, 1):
        task = task_map.get(slot["task_id"], {})
        title = task.get("title", "???")
        deadline = task.get("deadline", "")
        dl_str = f" (마감: {deadline})" if deadline else ""
        est = task.get("estimated_minutes", "")
        est_str = f" [{est}분]" if est else ""
        lines.append(f"{i}. {slot['start_time']}~{slot['end_time']}  {title}{dl_str}{est_str}")

    lines.append("\n이대로 갈까요? 수정할 거 있으면 말해주세요.")
    return "\n".join(lines)


def format_plan_change(old_plan: List[dict], new_plan: List[dict], tasks: List[dict]) -> str:
    """플랜 변경 전/후 비교 텍스트"""
    task_map = {t["id"]: t for t in tasks}
    lines = ["📋 플랜이 재조정됐어!\n"]

    for slot in new_plan:
        task = task_map.get(slot["task_id"], {})
        title = task.get("title", "???")
        # 이전 플랜에서 같은 태스크 찾기
        old_slot = next((s for s in old_plan if s["task_id"] == slot["task_id"]), None)
        if old_slot and old_slot["start_time"] != slot["start_time"]:
            lines.append(f"  🔄 {title}: {old_slot['start_time']} → {slot['start_time']}~{slot['end_time']}")
        elif not old_slot:
            lines.append(f"  🆕 {title}: {slot['start_time']}~{slot['end_time']}")
        else:
            lines.append(f"  ✔️ {title}: {slot['start_time']}~{slot['end_time']} (변동 없음)")

    return "\n".join(lines)


async def _load_patterns() -> dict:
    """패턴 테이블에서 플래너용 데이터 로드"""
    try:
        import aiosqlite
        from chiro_bot.config import DB_PATH
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT pattern_type, pattern_key, pattern_value FROM patterns")
            rows = await cursor.fetchall()
            result = {}
            for r in rows:
                key = f"{r['pattern_type']}_{r['pattern_key']}"
                try:
                    result[key] = float(r["pattern_value"])
                except ValueError:
                    result[key] = r["pattern_value"]
            return result
    except Exception:
        return {}
