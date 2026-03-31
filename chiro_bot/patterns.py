"""패턴 학습 — task_history 기반 행동 패턴 분석 + patterns 테이블 갱신
저녁 리뷰 후 호출되어 누적 데이터에서 패턴을 추출한다."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

import aiosqlite
from chiro_bot.config import DB_PATH

logger = logging.getLogger(__name__)


async def update_patterns():
    """task_history를 분석해서 patterns 테이블 갱신"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # ---- 전체 히스토리 로드 ----
        cursor = await conn.execute(
            "SELECT * FROM task_history ORDER BY date DESC LIMIT 500"
        )
        history = [dict(r) for r in await cursor.fetchall()]

        if not history:
            return

        # ---- 1. 카테고리별 완료율 ----
        cat_total = defaultdict(int)
        cat_done = defaultdict(int)
        cat_defer = defaultdict(int)
        for h in history:
            cat = h.get("category") or "기타"
            cat_total[cat] += 1
            if h["status"] == "done":
                cat_done[cat] += 1
            if h["status"] == "deferred":
                cat_defer[cat] += 1

        for cat in cat_total:
            total = cat_total[cat]
            done_rate = cat_done[cat] / total if total > 0 else 0
            defer_rate = cat_defer[cat] / total if total > 0 else 0
            await _upsert_pattern(conn, "category_completion", cat, f"{done_rate:.2f}")
            await _upsert_pattern(conn, "category_defer", cat, f"{defer_rate:.2f}")

        # ---- 2. 요일별 완료율 ----
        dow_total = defaultdict(int)
        dow_done = defaultdict(int)
        for h in history:
            try:
                d = datetime.fromisoformat(h["date"])
                dow = d.weekday()  # 0=월
                dow_total[dow] += 1
                if h["status"] == "done":
                    dow_done[dow] += 1
            except (ValueError, TypeError):
                pass

        dow_names = ["월", "화", "수", "목", "금", "토", "일"]
        for dow in dow_total:
            rate = dow_done[dow] / dow_total[dow] if dow_total[dow] > 0 else 0
            await _upsert_pattern(conn, "dow_completion", dow_names[dow], f"{rate:.2f}")

        # ---- 3. 평균 미룸 횟수 ----
        total_postpones = sum(h.get("postpone_count", 0) for h in history)
        avg_postpone = total_postpones / len(history) if history else 0
        await _upsert_pattern(conn, "avg", "postpone_per_task", f"{avg_postpone:.2f}")

        # ---- 4. 평균 일간 완료율 ----
        daily_stats = defaultdict(lambda: {"total": 0, "done": 0})
        for h in history:
            d = h.get("date", "unknown")
            daily_stats[d]["total"] += 1
            if h["status"] == "done":
                daily_stats[d]["done"] += 1

        if daily_stats:
            daily_rates = [
                s["done"] / s["total"] for s in daily_stats.values() if s["total"] > 0
            ]
            avg_daily_rate = sum(daily_rates) / len(daily_rates) if daily_rates else 0
            await _upsert_pattern(conn, "avg", "daily_completion_rate", f"{avg_daily_rate:.2f}")

        # ---- 5. 가장 미루는 카테고리 ----
        if cat_defer:
            worst_cat = max(cat_defer, key=lambda c: cat_defer[c] / max(cat_total[c], 1))
            await _upsert_pattern(conn, "worst", "most_deferred_category", worst_cat)

        await conn.commit()
        logger.info("패턴 업데이트 완료")


async def get_pattern_summary() -> str:
    """패턴 요약 텍스트 (AI 프롬프트 주입용)"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM patterns ORDER BY pattern_type, pattern_key")
        rows = [dict(r) for r in await cursor.fetchall()]

    if not rows:
        return "아직 축적된 패턴 데이터가 없음."

    lines = []

    # 카테고리별
    cat_lines = []
    for r in rows:
        if r["pattern_type"] == "category_completion":
            rate = float(r["pattern_value"]) * 100
            cat_lines.append(f"  {r['pattern_key']}: 완료율 {rate:.0f}%")
    if cat_lines:
        lines.append("카테고리별 완료율:")
        lines.extend(cat_lines)

    # 요일별
    dow_lines = []
    for r in rows:
        if r["pattern_type"] == "dow_completion":
            rate = float(r["pattern_value"]) * 100
            dow_lines.append(f"  {r['pattern_key']}: {rate:.0f}%")
    if dow_lines:
        lines.append("요일별 완료율:")
        lines.extend(dow_lines)

    # 평균
    for r in rows:
        if r["pattern_type"] == "avg":
            if r["pattern_key"] == "daily_completion_rate":
                lines.append(f"평균 일간 완료율: {float(r['pattern_value'])*100:.0f}%")
            elif r["pattern_key"] == "postpone_per_task":
                lines.append(f"태스크당 평균 미룸: {r['pattern_value']}회")

    # 최악
    for r in rows:
        if r["pattern_type"] == "worst":
            lines.append(f"가장 많이 미루는 카테고리: {r['pattern_value']}")

    return "\n".join(lines) if lines else "패턴 데이터 수집 중."


async def _upsert_pattern(conn: aiosqlite.Connection, ptype: str, pkey: str, pvalue: str):
    await conn.execute(
        """INSERT INTO patterns (pattern_type, pattern_key, pattern_value, updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(pattern_type, pattern_key)
           DO UPDATE SET pattern_value = ?, updated_at = datetime('now')""",
        (ptype, pkey, pvalue, pvalue)
    )
