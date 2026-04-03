from __future__ import annotations

import aiosqlite
import json
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo
from chiro_bot.config import DB_PATH, TIMEZONE
from chiro_bot.architecture.models import ProactiveState, ConversationState


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
    master_task_id INTEGER,
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

-- 마스터 태스크: 단건/기간/반복 일정의 원본
CREATE TABLE IF NOT EXISTS master_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT,
    task_type TEXT NOT NULL DEFAULT 'one_off', -- one_off/span/recurring
    start_date TEXT NOT NULL,
    end_date TEXT,
    deadline TEXT,
    estimated_minutes INTEGER,
    preferred_start TEXT,
    recurrence_days TEXT,                     -- JSON array [0..6]
    status TEXT NOT NULL DEFAULT 'active',    -- active/completed/cancelled
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

-- 장기 메모리: 원문 대화가 아니라 정규화된 사실 저장
CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,               -- routine / preference / pattern / profile / schedule
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT,                           -- user_message / derived / manual
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(namespace, key)
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

-- 명시적 대화 상태 (신규 런타임 계층)
CREATE TABLE IF NOT EXISTS conversation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'idle',
    waiting_for TEXT,
    subject_type TEXT,
    subject_ref TEXT,
    last_bot_prompt_purpose TEXT,
    last_user_intent TEXT,
    expires_at TEXT,
    payload TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO conversation_state (id, updated_at) VALUES (1, datetime('now'));

-- 프로액티브 정책 상태 (신규 런타임 계층)
CREATE TABLE IF NOT EXISTS proactive_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    escalation_level INTEGER NOT NULL DEFAULT 0,
    last_prompt_requiring_response_at TEXT,
    last_prompt_purpose TEXT,
    last_user_response_at TEXT,
    last_morning_at TEXT,
    last_evening_review_at TEXT,
    last_bedtime_at TEXT,
    healthcheck_sent_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO proactive_state (id, updated_at) VALUES (1, datetime('now'));

-- 리마인더 (알림만. 태스크가 아님.)
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL DEFAULT (date('now')),
    time TEXT NOT NULL,                        -- "HH:MM" 알림 시각
    message TEXT NOT NULL,                     -- 알림 내용
    sent INTEGER NOT NULL DEFAULT 0,           -- 0=미발송, 1=발송완료
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
        await _run_migrations(db)
        await _backfill_master_tasks(db)
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    """DB 커넥션 반환"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def _local_today() -> date:
    return _local_now().date()


def _parse_stored_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(ZoneInfo(TIMEZONE))
    if "T" in value:
        return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return parsed.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(TIMEZONE))


# ==================== daily_tasks ====================


def _today_str() -> str:
    return _local_today().isoformat()


def _extract_date(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10] if len(value) >= 10 and value[4:5] == "-" else None


def _normalize_task_type(
    task_type: str | None,
    start_date: str | None,
    end_date: str | None,
    deadline: str | None,
    recurrence_days: list[int] | None,
) -> str:
    if task_type in {"one_off", "span", "recurring"}:
        return task_type
    if recurrence_days:
        return "recurring"
    if start_date and end_date and start_date != end_date:
        return "span"
    deadline_date = _extract_date(deadline)
    if start_date and deadline_date and start_date != deadline_date:
        return "span"
    return "one_off"


def _resolve_task_window(
    task_type: str,
    start_date: str | None,
    end_date: str | None,
    deadline: str | None,
    recurrence_days: list[int] | None,
) -> tuple[str, str | None, list[int] | None]:
    today = _today_str()
    deadline_date = _extract_date(deadline)

    if task_type == "recurring":
        resolved_start = start_date or today
        resolved_end = end_date
        resolved_days = recurrence_days or [datetime.fromisoformat(resolved_start).weekday()]
        return resolved_start, resolved_end, resolved_days

    if task_type == "span":
        resolved_start = start_date or today
        resolved_end = end_date or deadline_date or resolved_start
        if resolved_end < resolved_start:
            resolved_end = resolved_start
        return resolved_start, resolved_end, None

    resolved_start = start_date or deadline_date or today
    return resolved_start, resolved_start, None


def _task_active_on(task: dict, target_date: str) -> bool:
    if task.get("status") != "active":
        return False
    task_type = task.get("task_type", "one_off")
    start_date = task.get("start_date")
    end_date = task.get("end_date") or start_date

    if task_type == "recurring":
        if start_date and target_date < start_date:
            return False
        if end_date and target_date > end_date:
            return False
        weekday = datetime.fromisoformat(target_date).weekday()
        recurrence_days = json.loads(task.get("recurrence_days") or "[]")
        return weekday in recurrence_days

    if not start_date:
        return False
    return start_date <= target_date <= (end_date or start_date)


async def _run_migrations(db: aiosqlite.Connection):
    cursor = await db.execute("PRAGMA table_info(daily_tasks)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "master_task_id" not in columns:
        await db.execute("ALTER TABLE daily_tasks ADD COLUMN master_task_id INTEGER")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_tasks_date_master ON daily_tasks(date, master_task_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_routines_slot ON routines(day_of_week, start_time, end_time, label)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_master_tasks_active_window ON master_tasks(status, start_date, end_date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_facts_namespace_key ON memory_facts(namespace, key)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_tasks_date ON daily_tasks(date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_plan_date ON daily_plan(date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dnd_today_date ON dnd_today(date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_condition_log_date ON condition_log(date)"
    )


async def _backfill_master_tasks(db: aiosqlite.Connection):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM daily_tasks WHERE master_task_id IS NULL ORDER BY id ASC"
    )
    rows = await cursor.fetchall()
    for row in rows:
        task_type = "one_off"
        master_status = "completed" if row["status"] == "done" else "active"
        master_cursor = await db.execute(
            """INSERT INTO master_tasks
               (title, category, task_type, start_date, end_date, deadline, estimated_minutes,
                preferred_start, recurrence_days, status, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["title"],
                row["category"],
                task_type,
                row["date"],
                row["date"],
                row["deadline"],
                row["estimated_minutes"],
                row["preferred_start"],
                None,
                master_status,
                row["completed_at"],
            ),
        )
        await db.execute(
            "UPDATE daily_tasks SET master_task_id = ? WHERE id = ?",
            (master_cursor.lastrowid, row["id"]),
        )


async def _materialize_tasks_for_date(target_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM master_tasks WHERE status = 'active'"
        )
        master_tasks = [dict(row) for row in await cursor.fetchall()]

        for task in master_tasks:
            if not _task_active_on(task, target_date):
                continue

            cursor = await db.execute(
                "SELECT id FROM daily_tasks WHERE date = ? AND master_task_id = ? LIMIT 1",
                (target_date, task["id"]),
            )
            existing = await cursor.fetchone()
            if existing:
                continue

            await db.execute(
                """INSERT INTO daily_tasks
                   (date, master_task_id, title, category, deadline, estimated_minutes, preferred_start, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    target_date,
                    task["id"],
                    task["title"],
                    task["category"],
                    task["deadline"],
                    task["estimated_minutes"],
                    task["preferred_start"],
                ),
            )
        await db.commit()

async def add_task(title: str, category: str = None, deadline: str = None,
                   estimated_minutes: int = None, preferred_start: str = None,
                   task_type: str = None, start_date: str = None,
                   end_date: str = None, recurrence_days: list[int] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        normalized_type = _normalize_task_type(
            task_type, start_date, end_date, deadline, recurrence_days
        )
        resolved_start, resolved_end, resolved_days = _resolve_task_window(
            normalized_type, start_date, end_date, deadline, recurrence_days
        )
        cursor = await db.execute(
            """INSERT INTO master_tasks
               (title, category, task_type, start_date, end_date, deadline, estimated_minutes,
                preferred_start, recurrence_days)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                category,
                normalized_type,
                resolved_start,
                resolved_end,
                deadline,
                estimated_minutes,
                preferred_start,
                json.dumps(resolved_days, ensure_ascii=False) if resolved_days is not None else None,
            ),
        )
        master_task_id = cursor.lastrowid
        await db.commit()
    await _materialize_tasks_for_date(_today_str())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM daily_tasks WHERE date = ? AND master_task_id = ? LIMIT 1",
            (_today_str(), master_task_id),
        )
        row = await cursor.fetchone()
        return row["id"] if row else master_task_id


async def get_today_tasks() -> list[dict]:
    today = _today_str()
    await _materialize_tasks_for_date(today)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM daily_tasks WHERE date = ? ORDER BY deadline ASC NULLS LAST, id ASC",
            (today,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_upcoming_master_tasks(limit: int = 10) -> list[dict]:
    today = _today_str()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM master_tasks
               WHERE status = 'active'
                 AND (
                    (task_type IN ('one_off', 'span') AND start_date > ?)
                    OR (task_type = 'recurring')
                 )
               ORDER BY start_date ASC, id ASC
            """,
            (today,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    upcoming = []
    for row in rows:
        recurrence_days = json.loads(row.get("recurrence_days") or "[]")
        row["recurrence_days"] = recurrence_days
        if row["task_type"] == "recurring":
            start_bound = max(today, row["start_date"])
            next_date = None
            for offset in range(1, 15):
                candidate = date.fromisoformat(start_bound) + timedelta(days=offset)
                candidate_str = candidate.isoformat()
                if row.get("end_date") and candidate_str > row["end_date"]:
                    break
                if candidate.weekday() in recurrence_days:
                    next_date = candidate_str
                    break
            if not next_date:
                continue
            row["start_date"] = next_date
        upcoming.append(row)
    upcoming.sort(key=lambda task: (task["start_date"], task["id"]))
    return upcoming[:limit]


async def update_task_status(task_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        completed_at = _local_now().isoformat() if status == "done" else None
        cursor = await db.execute(
            "SELECT master_task_id FROM daily_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        await db.execute(
            "UPDATE daily_tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, task_id)
        )
        if row and row["master_task_id"] and status == "done":
            cursor = await db.execute(
                "SELECT task_type FROM master_tasks WHERE id = ?",
                (row["master_task_id"],),
            )
            master = await cursor.fetchone()
            if master and master["task_type"] in ("one_off", "span"):
                await db.execute(
                    "UPDATE master_tasks SET status = 'completed', completed_at = ? WHERE id = ?",
                    (completed_at, row["master_task_id"]),
                )
        await db.commit()


async def get_master_task(master_task_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM master_tasks WHERE id = ?",
            (master_task_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_task_instance_and_master(
    task_id: int,
    *,
    title: str | None = None,
    deadline: str | None = None,
    estimated_minutes: int | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT master_task_id FROM daily_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()

        updates = []
        vals = []
        if title is not None:
            updates.append("title = ?")
            vals.append(title)
        if deadline is not None:
            updates.append("deadline = ?")
            vals.append(deadline)
        if estimated_minutes is not None:
            updates.append("estimated_minutes = ?")
            vals.append(estimated_minutes)
        if updates:
            vals.append(task_id)
            await db.execute(
                f"UPDATE daily_tasks SET {', '.join(updates)} WHERE id = ?",
                vals,
            )

        if row and row["master_task_id"] and updates:
            master_updates = []
            master_vals = []
            if title is not None:
                master_updates.append("title = ?")
                master_vals.append(title)
            if deadline is not None:
                master_updates.append("deadline = ?")
                master_vals.append(deadline)
            if estimated_minutes is not None:
                master_updates.append("estimated_minutes = ?")
                master_vals.append(estimated_minutes)
            master_vals.append(row["master_task_id"])
            await db.execute(
                f"UPDATE master_tasks SET {', '.join(master_updates)} WHERE id = ?",
                master_vals,
            )
        await db.commit()


async def cancel_task(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT master_task_id FROM daily_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        await db.execute("DELETE FROM daily_tasks WHERE id = ?", (task_id,))
        await db.execute("DELETE FROM daily_plan WHERE task_id = ?", (task_id,))
        if row and row["master_task_id"]:
            await db.execute(
                "UPDATE master_tasks SET status = 'cancelled' WHERE id = ?",
                (row["master_task_id"],),
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
        today = _today_str()
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
        today = _today_str()
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
        today = _today_str()
        await db.execute(
            "INSERT INTO dnd_today (date, start_time, end_time, reason) VALUES (?, ?, ?, ?)",
            (today, start_time, end_time, reason)
        )
        await db.commit()


async def get_today_dnd() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = _today_str()
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
            "INSERT INTO messages (direction, content, intent, created_at) VALUES (?, ?, ?, ?)",
            (direction, content, intent, _local_now().isoformat())
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
        sets.append("updated_at = ?")
        vals.append(_local_now().isoformat())
        vals.append(1)
        await db.execute(
            f"UPDATE user_state SET {', '.join(sets)} WHERE id = ?",
            vals
        )
        await db.commit()


# ==================== conversation_state ====================

async def get_conversation_state() -> ConversationState:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM conversation_state WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return ConversationState()
        state_dict = dict(row)
        if state_dict.get("payload"):
            state_dict["payload"] = json.loads(state_dict["payload"])
        
        expires_at = _parse_stored_datetime(state_dict.get("expires_at"))

        return ConversationState(
            mode=state_dict.get("mode", "idle"),
            waiting_for=state_dict.get("waiting_for"),
            subject_type=state_dict.get("subject_type"),
            subject_ref=state_dict.get("subject_ref"),
            last_bot_prompt_purpose=state_dict.get("last_bot_prompt_purpose"),
            last_user_intent=state_dict.get("last_user_intent"),
            expires_at=expires_at,
            payload=state_dict.get("payload"),
        )


async def update_conversation_state(**kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k == "payload" and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = ?")
        vals.append(_local_now().isoformat())
        vals.append(1)
        await db.execute(
            f"UPDATE conversation_state SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()


async def reset_conversation_state():
    await update_conversation_state(
        mode="idle",
        waiting_for=None,
        subject_type=None,
        subject_ref=None,
        last_bot_prompt_purpose=None,
        last_user_intent=None,
        expires_at=None,
        payload=None,
    )


# ==================== proactive_state ====================

async def get_proactive_state() -> ProactiveState:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM proactive_state WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return ProactiveState()
        
        state_dict = dict(row)
        return ProactiveState(
            escalation_level=state_dict.get("escalation_level", 0),
            last_prompt_requiring_response_at=_parse_stored_datetime(state_dict.get("last_prompt_requiring_response_at")),
            last_prompt_purpose=state_dict.get("last_prompt_purpose"),
            last_user_response_at=_parse_stored_datetime(state_dict.get("last_user_response_at")),
            last_morning_at=_parse_stored_datetime(state_dict.get("last_morning_at")),
            last_evening_review_at=_parse_stored_datetime(state_dict.get("last_evening_review_at")),
            last_bedtime_at=_parse_stored_datetime(state_dict.get("last_bedtime_at")),
            healthcheck_sent_at=_parse_stored_datetime(state_dict.get("healthcheck_sent_at")),
        )


async def update_proactive_state(**kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = ?")
        vals.append(_local_now().isoformat())
        vals.append(1)
        await db.execute(
            f"UPDATE proactive_state SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()


async def reset_proactive_state():
    await update_proactive_state(
        escalation_level=0,
        last_prompt_requiring_response_at=None,
        last_prompt_purpose=None,
    )


# ==================== memory_facts ====================

async def upsert_memory_fact(
    namespace: str,
    key: str,
    value: str,
    *,
    confidence: float = 1.0,
    source: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
):
    now = _local_now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO memory_facts
               (namespace, key, value, confidence, source, valid_from, valid_to, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(namespace, key)
               DO UPDATE SET
                 value = excluded.value,
                 confidence = excluded.confidence,
                 source = excluded.source,
                 valid_from = excluded.valid_from,
                 valid_to = excluded.valid_to,
                 updated_at = excluded.updated_at
            """,
            (namespace, key, value, confidence, source, valid_from, valid_to, now, now),
        )
        await db.commit()


async def get_memory_fact(namespace: str, key: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM memory_facts WHERE namespace = ? AND key = ? LIMIT 1",
            (namespace, key),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def list_memory_facts(namespace: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if namespace:
            cursor = await db.execute(
                "SELECT * FROM memory_facts WHERE namespace = ? ORDER BY key ASC",
                (namespace,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM memory_facts ORDER BY namespace ASC, key ASC"
            )
        return [dict(row) for row in await cursor.fetchall()]


async def delete_memory_fact(namespace: str, key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM memory_facts WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        await db.commit()


# ==================== dnd_defaults → dnd_today 로드 ====================

async def load_dnd_defaults_for_today():
    """고정 DND 기본값을 오늘의 dnd_today로 복사 (아침에 1회 호출)"""
    now = _local_now()
    dow = now.weekday()  # 0=월
    today = _today_str()

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
    now = _local_now()
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
            "INSERT OR IGNORE INTO routines (day_of_week, start_time, end_time, label) VALUES (?, ?, ?, ?)",
            (day_of_week, start_time, end_time, label)
        )
        await db.commit()


async def replace_routine_label(label: str, routine_slots: list[dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM routines WHERE label = ?", (label,))
        for slot in routine_slots:
            await db.execute(
                "INSERT INTO routines (day_of_week, start_time, end_time, label) VALUES (?, ?, ?, ?)",
                (slot["day_of_week"], slot["start_time"], slot["end_time"], label),
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
            return _parse_stored_datetime(row["created_at"])
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
            return _parse_stored_datetime(row["created_at"])
        return None


async def reset_no_response():
    """유저 응답 수신 시 미응답 카운트 리셋"""
    await update_user_state(no_response_count=0, escalation_level=0)
    await update_proactive_state(
        escalation_level=0,
        last_user_response_at=_local_now().isoformat(),
    )


async def bump_no_response():
    """미응답 카운트 + 에스컬레이션 레벨 증가"""
    state = await get_user_state()
    count = (state.get("no_response_count") or 0) + 1
    level = min(count, 3)  # 최대 3
    await update_user_state(no_response_count=count, escalation_level=level)
    await update_proactive_state(escalation_level=level)
    return count, level


# ==================== task_history (아카이브) ====================

async def archive_today():
    """오늘의 태스크를 task_history로 아카이브"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = _today_str()
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
        today = _today_str()
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
        today = _today_str()
        await db.execute(
            "INSERT OR REPLACE INTO healthcheck_log (date, sent_at) VALUES (?, ?)",
            (today, _local_now().isoformat())
        )
        await db.commit()


async def is_healthcheck_sent_today() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        today = _today_str()
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM healthcheck_log WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()
        return row and row["cnt"] > 0


# ==================== reminders ====================

async def add_reminder(time_hm: str, message: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        today = _today_str()
        cursor = await conn.execute(
            "INSERT INTO reminders (date, time, message) VALUES (?, ?, ?)",
            (today, time_hm, message)
        )
        await conn.commit()
        return cursor.lastrowid


async def get_pending_reminders() -> list:
    """아직 발송 안 된 오늘 리마인더"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        today = _today_str()
        cursor = await conn.execute(
            "SELECT * FROM reminders WHERE date = ? AND sent = 0 ORDER BY time ASC",
            (today,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def mark_reminder_sent(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        await conn.commit()


# ==================== condition_log ====================

async def log_condition(time_hm: str, energy_level: int = None, mood: str = None,
                        activity: str = None, reason: str = None, note: str = None):
    async with aiosqlite.connect(DB_PATH) as conn:
        today = _today_str()
        await conn.execute(
            """INSERT INTO condition_log (date, time, energy_level, mood, activity, reason, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (today, time_hm, energy_level, mood, activity, reason, note)
        )
        await conn.commit()


async def get_today_conditions() -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        today = _today_str()
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


# ==================== retention / maintenance ====================

async def cleanup_old_messages(retention_days: int = 3) -> int:
    cutoff = (_local_now() - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "DELETE FROM messages WHERE created_at < ?",
            (cutoff,),
        )
        await conn.commit()
        return cursor.rowcount or 0


async def cleanup_old_operational_data(retention_days: int = 3) -> dict[str, int]:
    cutoff_date = (_local_today() - timedelta(days=retention_days)).isoformat()
    result: dict[str, int] = {}
    async with aiosqlite.connect(DB_PATH) as conn:
        tables = {
            "daily_tasks": "date",
            "daily_plan": "date",
            "dnd_today": "date",
            "reminders": "date",
            "condition_log": "date",
        }
        for table, column in tables.items():
            cursor = await conn.execute(
                f"DELETE FROM {table} WHERE {column} < ?",
                (cutoff_date,),
            )
            result[table] = cursor.rowcount or 0
        await conn.commit()
    return result


async def run_maintenance(
    *,
    message_retention_days: int = 3,
    operational_retention_days: int = 3,
) -> dict[str, object]:
    deleted_messages = await cleanup_old_messages(message_retention_days)
    deleted_operational = await cleanup_old_operational_data(operational_retention_days)
    return {
        "deleted_messages": deleted_messages,
        "deleted_operational": deleted_operational,
    }


# ==================== DB 백업 ====================

async def backup_db():
    """SQLite DB를 백업 디렉토리로 복사"""
    import shutil
    import os
    backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    today = _today_str()
    backup_path = os.path.join(backup_dir, f"chiro_{today}.db")
    shutil.copy2(DB_PATH, backup_path)
    # 7일 이상 된 백업 삭제
    for f in os.listdir(backup_dir):
        fpath = os.path.join(backup_dir, f)
        if os.path.isfile(fpath):
            age = (_local_today() - date.fromisoformat(f.replace("chiro_", "").replace(".db", ""))).days
            if age > 7:
                os.remove(fpath)
