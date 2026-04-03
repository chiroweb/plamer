from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Any

from chiro_bot.config import TIMEZONE

# --- Setup sys.path to allow imports from the project root ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- Configure logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
# Quieten noisy libraries
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# --- Mock Telegram and other external dependencies ---
class MockBot:
    def __init__(self, message_handler: Callable[[str], None]):
        self.message_handler = message_handler

    async def send_message(self, chat_id: Any, text: str):
        self.message_handler(text)
        await asyncio.sleep(0.01)

class MockUpdate:
    def __init__(self, text: str):
        self.message = self
        self.text = text

    async def reply_text(self, text: str):
        # In this simulation, the reply is handled by the mock bot's handler
        await asyncio.sleep(0.01)

class MockContext:
    DEFAULT_TYPE = None

# --- Main simulation components ---
@dataclass
class VirtualChiroBot:
    """A simulated environment for the bot, with a virtual clock and in-memory DB."""
    now: datetime
    db_path: str
    bot_responses: list[str] = field(default_factory=list)

    def __post_init__(self):
        # Ensure db path is unique for this instance
        self.db_path = f":memory:_{self.now.strftime('%Y%m%d%H%M%S%f')}"

    def get_time(self) -> datetime:
        return self.now

    def advance_time(self, minutes: int):
        self.now += timedelta(minutes=minutes)

    def log_bot_response(self, text: str):
        self.bot_responses.append(f"[{self.now.strftime('%H:%M')}] BOT: {text}")

@dataclass
class Event:
    """A single action in a scenario, like a user message or a proactive check."""
    at_time: str  # "HH:MM"
    action: Callable[..., Any]
    args: list[Any] = field(default_factory=list)
    expected_responses: list[str] = field(default_factory=list)

@dataclass
class Scenario:
    name: str
    initial_state: Callable[[VirtualChiroBot], Awaitable[None]] | None = None
    events: list[Event] = field(default_factory=list)
    assertions: Callable[[VirtualChiroBot, list[str]], None] | None = None

async def run_scenario(scenario: Scenario) -> tuple[bool, list[str]]:
    """Executes a single simulation scenario."""
    from datetime import timedelta
    from chiro_bot import database as db
    from chiro_bot import handlers, proactive

    # --- Setup virtual environment ---
    start_time = datetime(2026, 4, 2, 0, 0, 0, tzinfo=ZoneInfo(TIMEZONE))
    sim_bot = VirtualChiroBot(now=start_time, db_path=":memory:")
    
    # Override the real DB path and time functions with virtual ones
    db.DB_PATH = sim_bot.db_path
    await db.init_db()

    def get_sim_time():
        return sim_bot.get_time()

    proactive.datetime.now = get_sim_time
    # More mocking might be needed here for other external dependencies
    
    conversation_log = [f"--- Running Scenario: {scenario.name} ---"]

    if scenario.initial_state:
        await scenario.initial_state(sim_bot)

    # --- Event loop ---
    for event in scenario.events:
        hour, minute = map(int, event.at_time.split(":"))
        event_time = sim_bot.now.replace(hour=hour, minute=minute)
        
        # Advance time to the event
        if event_time < sim_bot.now:
            # Assumes events are on the next day if time is earlier
            event_time += timedelta(days=1)
        sim_bot.now = event_time
        
        log_entry = f"\n[{sim_bot.now.strftime('%H:%M')}] Running event: {event.action.__name__}"
        conversation_log.append(log_entry)

        if event.action == handlers.handle_message:
            user_text = event.args[0]
            conversation_log.append(f"[{sim_bot.now.strftime('%H:%M')}] USER: {user_text}")
            update = MockUpdate(user_text)
            context = MockContext()
            
            # Temporarily patch the bot's send_message
            original_send = proactive._send
            async def mock_send(text):
                sim_bot.log_bot_response(text)
            proactive._send = mock_send

            await event.action(update, context)
            
            # Restore original function
            proactive._send = original_send

        elif event.action == proactive.proactive_check:
            # This action logs its own responses
            await event.action()
    
    # --- Assertions ---
    passed = True
    try:
        if scenario.assertions:
            scenario.assertions(sim_bot, conversation_log)
        conversation_log.append("\n--- Assertions PASSED ---")
    except AssertionError as e:
        passed = False
        conversation_log.append(f"\n--- Assertions FAILED: {e} ---")
        
    return passed, conversation_log
