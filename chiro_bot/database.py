from __future__ import annotations

import aiosqlite
import json
from datetime import datetime, date
from typing import List, Dict, Optional
from chiro_bot.config import DB_PATH, TIMEZONE

# --- Schema ---

SCHEMA_SQL = """
-- 고정 데이터: 반복 루틴 (수업 시간표 등)
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,          -- 0=월 ~ 6=일
    start_time TEXT NOT NULL,              -- "HH:MM"
    end_time TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 고정 데이터: 기본 DND 슬롯 (매일 적용)
CREATE TABLE IF NOT EXISTS dnd_defaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER,                   -- NULL이면 매일
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    label TEXT
);

-- 일간 데이터: 오늘의 태스크
CREATE TABLE IF NOT EXISTS daily_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT (date('now')),
    title TEXT NOT NULL,
    category TEXT,                          -- 학업, 과제, 개인 등
    deadline TEXT,                          -- "YYYY-MM-DD HH:MM" or NULL
    estimated_minutes INTEGER,             -- 예상 소요 시간(분)
    preferred_start TEXT,                  -- 유저가 원하는 시작 시간 "HH:MM"
    status TEXT NOT NULL DEFAULT 'pending', -- pending/in_progress/done/deferred/partial/failed
    postpone_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- 일간 데이터: 오늘의 플랜 슬롯
CREATE TABLE IF NOT EXISTS daily_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT (date('now')),
    task_id INTEGER NOT NULL,
    start_time TEXT NOT NULL,              -- "HH:MM"
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/active/done/skipped
    FOREIGN KEY (task_id) REFERENCES daily_tasks(id)
);

-- 일간 데이터: 오늘의 DND 슬롯
CREATE TABLE IF NOT EXISTS dnd_today (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT (date('now')),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    reason TEXT
);

-- 메시지 로그
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,               -- 'bot' or 'user'
    content TEXT NOT NULL,
    intent TEXT,                            -- 파싱된 인텐트
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 누적 데이터: 태스크 히스토리
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    status TEXT NOT NULL,
    postpone_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    estimated_minutes INTEGER,
    actual_minutes INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 누적 데이터: 일간 리포트
CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_tasks INTEGER,
    completed_tasks INTEGER,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 누적 데이터: 행동 패턴
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,             -- 'procrastination_hour', 'category_completion_rate' 등
    pattern_key TEXT NOT NULL,
    pattern_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pattern_type, pattern_key)
);

-- 아이디어 볼트
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    converted_to_task_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 유저 상태 (싱글 유저이므로 1행)
CREATE TABLE IF NOT EXISTS user_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_flow TEXT,                     -- NULL, 'task_collect', 'plan_confirm' 등
    flow_step INTEGER DEFAULT 0,
    flow_data TEXT,                         -- JSON
    last_bot_message_id INTEGER,
    last_bot_message_at TEXT,              -- 마지막 봇 메시지 발송 시각
    no_response_count INTEGER DEFAULT 0,
    escalation_level INTEGER DEFAULT 0,    -- 0=정상, 1=부드러운재촉, 2=강한재촉, 3=압박
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 초기 user_state 삽입
INSERT OR IGNORE INTO user_state (id, updated_at) VALUES (1, datetime('now'));

-- 컨디션/기분 로그
CREATE TABLE IF NOT EXISTS condition_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT (date('now')),
    time TEXT NOT NULL,                        -- "HH:MM"
    energy_level INTEGER,                      -- 1~5
    mood TEXT,                                 -- 기분 키워드
    activity TEXT,                             -- 지금 뭐하고 있는지
    reason TEXT,                               -- 이유/맥락
    note TEXT,                                 -- AI 또는 유저 메모
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 헬스체크 로그
CREATE TABLE IF NOT EXISTS healthcheck_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db():
    """DB 초기화 — 테이블 생성"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    """DB 커넥션 반환"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


# ==================== daily_tasks ====================

async def add_task(title: str, category: str = None, deadline: str = None,
                   estimated_minutes: int = None, preferred_start: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            """INSERT INTO daily_tasks (date, title, category, deadline, estimated_minutes, preferred_start)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (today, title, category, deadline, estimated_minutes, preferred_start)
        )
        await db.commit()
        return cursor.lastrowid


async def get_today_tasks() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            "SELECT * FROM daily_tasks WHERE date = ? ORDER BY deadline ASC NULLS LAST, id ASC",
            (today,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_task_status(task_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        completed_at = datetime.now().isoformat() if status == "done" else None
        await db.execute(
            "UPDATE daily_tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, task_id)
        )
        await db.commit()


async def increment_postpone(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE daily_tasks SET postpone_count = postpone_count + 1, status = 'deferred' WHERE id = ?",
            (task_id,)
        )
        await db.commit()


async def increment_fail(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE daily_tasks SET fail_count = fail_count + 1, status = 'failed' WHERE id = ?",
            (task_id,)
        )
        await db.commit()


# ==================== daily_plan ====================

async def set_daily_plan(plan_slots: list[dict]):
    """오늘 플랜 교체. plan_slots: [{task_id, start_time, end_time}, ...]"""
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        await db.execute("DELETE FROM daily_plan WHERE date = ?", (today,))
        for slot in plan_slots:
            await db.execute(
                "INSERT INTO daily_plan (date, task_id, start_time, end_time) VALUES (?, ?, ?, ?)",
                (today, slot["task_id"], slot["start_time"], slot["end_time"])
            )
        await db.commit()


async def get_today_plan() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            """SELECT dp.*, dt.title, dt.category, dt.status as task_status
               FROM daily_plan dp
               JOIN daily_tasks dt ON dp.task_id = dt.id
               WHERE dp.date = ?
               ORDER BY dp.start_time ASC""",
            (today,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ==================== dnd_today ====================

async def add_dnd_slot(start_time: str, end_time: str, reason: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        await db.execute(
            "INSERT INTO dnd_today (date, start_time, end_time, reason) VALUES (?, ?, ?, ?)",
            (today, start_time, end_time, reason)
        )
        await db.commit()


async def get_today_dnd() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            "SELECT * FROM dnd_today WHERE date = ? ORDER BY start_time ASC",
            (today,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def is_dnd_now() -> bool:
    """현재 시각이 DND 시간대인지 확인"""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(TIMEZONE))
    now_hm = now.strftime("%H:%M")
    dnd_slots = await get_today_dnd()
    for slot in dnd_slots:
        if slot["start_time"] <= now_hm <= slot["end_time"]:
            return True
    return False


# ==================== messages ====================

async def log_message(direction: str, content: str, intent: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (direction, content, intent) VALUES (?, ?, ?)",
            (direction, content, intent)
        )
        await db.commit()


async def get_recent_messages(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]


# ==================== user_state ====================

async def get_user_state() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM user_state WHERE id = 1")
        row = await cursor.fetchone()
        if row:
            state = dict(row)
            if state.get("flow_data"):
                state["flow_data"] = json.loads(state["flow_data"])
            return state
        return {}


async def update_user_state(**kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k == "flow_data" and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = datetime('now')")
        vals.append(1)
        await db.execute(
            f"UPDATE user_state SET {', '.join(sets)} WHERE id = ?",
            vals
        )
        await db.commit()


# ==================== dnd_defaults → dnd_today 로드 ====================

async def load_dnd_defaults_for_today():
    """고정 DND 기본값을 오늘의 dnd_today로 복사 (아침에 1회 호출)"""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(TIMEZONE))
    dow = now.weekday()  # 0=월
    today = date.today().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # 이미 로드했는지 체크 (기본값에서 온 것)
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM dnd_today WHERE date = ? AND reason LIKE '[기본]%'",
            (today,)
        )
        row = await cursor.fetchone()
        if row and row["cnt"] > 0:
            return  # 이미 로드됨

        # 매일 적용(day_of_week IS NULL) + 오늘 요일
        cursor = await db.execute(
            "SELECT * FROM dnd_defaults WHERE day_of_week IS NULL OR day_of_week = ?",
            (dow,)
        )
        defaults = await cursor.fetchall()
        for d in defaults:
            label = f"[기본] {d['label'] or ''}"
            await db.execute(
                "INSERT INTO dnd_today (date, start_time, end_time, reason) VALUES (?, ?, ?, ?)",
                (today, d["start_time"], d["end_time"], label)
            )
        await db.commit()


# ==================== routines ====================

async def get_today_routines() -> list:
    """오늘 요일의 루틴 반환"""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(TIMEZONE))
    dow = now.weekday()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM routines WHERE day_of_week = ? ORDER BY start_time",
            (dow,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_routine(day_of_week: int, start_time: str, end_time: str, label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO routines (day_of_week, start_time, end_time, label) VALUES (?, ?, ?, ?)",
            (day_of_week, start_time, end_time, label)
        )
        await db.commit()


async def add_dnd_default(start_time: str, end_time: str, label: str = None, day_of_week: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO dnd_defaults (day_of_week, start_time, end_time, label) VALUES (?, ?, ?, ?)",
            (day_of_week, start_time, end_time, label)
        )
        await db.commit()


# ==================== 에스컬레이션 ====================

async def get_last_bot_message_time() -> Optional[datetime]:
    """마지막 봇 메시지 발송 시각"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT created_at FROM messages WHERE direction = 'bot' ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return datetime.fromisoformat(row["created_at"])
        return None


async def get_last_user_message_time() -> Optional[datetime]:
    """마지막 유저 메시지 시각"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT created_at FROM messages WHERE direction = 'user' ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return datetime.fromisoformat(row["created_at"])
        return None


async def reset_no_response():
    """유저 응답 수신 시 미응답 카운트 리셋"""
    await update_user_state(no_response_count=0, escalation_level=0)


async def bump_no_response():
    """미응답 카운트 + 에스컬레이션 레벨 증가"""
    state = await get_user_state()
    count = (state.get("no_response_count") or 0) + 1
    level = min(count, 3)  # 최대 3
    await update_user_state(no_response_count=count, escalation_level=level)
    return count, level


# ==================== task_history (아카이브) ====================

async def archive_today():
    """오늘의 태스크를 task_history로 아카이브"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            "SELECT * FROM daily_tasks WHERE date = ?", (today,)
        )
        tasks = await cursor.fetchall()
        for t in tasks:
            await db.execute(
                """INSERT INTO task_history (date, title, category, status, postpone_count, fail_count, estimated_minutes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (t["date"], t["title"], t["category"], t["status"],
                 t["postpone_count"], t["fail_count"], t["estimated_minutes"])
            )
        await db.commit()


# ==================== daily_reports ====================

async def save_daily_report(total: int, completed: int, summary: str):
    """일간 리포트 저장"""
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        await db.execute(
            """INSERT OR REPLACE INTO daily_reports (date, total_tasks, completed_tasks, summary)
               VALUES (?, ?, ?, ?)""",
            (today, total, completed, summary)
        )
        await db.commit()


async def get_task_stats_today() -> dict:
    """오늘의 태스크 통계"""
    tasks = await get_today_tasks()
    total = len(tasks)
    done = sum(1 for t in tasks if t["status"] == "done")
    deferred = sum(1 for t in tasks if t["status"] == "deferred")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    partial = sum(1 for t in tasks if t["status"] == "partial")
    in_progress = sum(1 for t in tasks if t["status"] == "in_progress")
    pending = sum(1 for t in tasks if t["status"] == "pending")
    total_postpones = sum(t["postpone_count"] for t in tasks)
    total_fails = sum(t["fail_count"] for t in tasks)
    return {
        "total": total, "done": done, "deferred": deferred,
        "failed": failed, "partial": partial, "in_progress": in_progress,
        "pending": pending, "total_postpones": total_postpones,
        "total_fails": total_fails, "tasks": tasks,
    }


# ==================== healthcheck ====================

async def log_healthcheck():
    """헬스체크 발송 기록"""
    async with aiosqlite.connect(DB_PATH) as db:
        today = date.today().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO healthcheck_log (date, sent_at) VALUES (?, datetime('now'))",
            (today,)
        )
        await db.commit()


async def is_healthcheck_sent_today() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM healthcheck_log WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()
        return row and row["cnt"] > 0


# ==================== condition_log ====================

async def log_condition(time_hm: str, energy_level: int = None, mood: str = None,
                        activity: str = None, reason: str = None, note: str = None):
    async with aiosqlite.connect(DB_PATH) as conn:
        today = date.today().isoformat()
        await conn.execute(
            """INSERT INTO condition_log (date, time, energy_level, mood, activity, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, time_hm, energy_level, mood, activity, reason, note)
        )
        await conn.commit()


async def get_today_conditions() -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        today = date.today().isoformat()
        cursor = await conn.execute(
            "SELECT * FROM condition_log WHERE date = ? ORDER BY time ASC", (today,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_condition_history(days: int = 14) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM condition_log ORDER BY date DESC, time DESC LIMIT ?",
            (days * 10,)
        )
        return [dict(r) for r in await cursor.fetchall()]


# ==================== DB 백업 ====================

async def backup_db():
    """SQLite DB를 백업 디렉토리로 복사"""
    import shutil
    import os
    backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    today = date.today().isoformat()
    backup_path = os.path.join(backup_dir, f"chiro_{today}.db")
    shutil.copy2(DB_PATH, backup_path)
    # 7일 이상 된 백업 삭제
    for f in os.listdir(backup_dir):
        fpath = os.path.join(backup_dir, f)
        if os.path.isfile(fpath):
            age = (date.today() - date.fromisoformat(f.replace("chiro_", "").replace(".db", ""))).days
            if age > 7:
                os.remove(fpath)
