# Reminders Feature

## Overview

Add voice-controlled reminders to jarvis-node-setup. Users can set, list, snooze, and delete reminders via voice commands. Reminders fire at the specified time via TTS announcement. Supports one-shot and recurring reminders.

## Problem Statement

Jarvis supports timers (relative duration: "set a timer for 5 minutes") but not reminders (absolute time: "remind me to call mom tomorrow at 3 PM"). Reminders require:
- Absolute datetime targeting (not relative duration)
- Persistence across restarts
- Recurrence (daily, weekly, etc.)
- Background monitoring and TTS announcement when due
- Snooze capability

## Architecture

### Components

```
commands/
  set_reminder_command.py        # IJarvisCommand - create a reminder
  list_reminders_command.py      # IJarvisCommand - list active reminders
  delete_reminder_command.py     # IJarvisCommand - cancel a reminder
  snooze_reminder_command.py     # IJarvisCommand - snooze a fired reminder

services/
  reminder_service.py            # Singleton - CRUD, recurrence, snooze logic

agents/
  reminder_agent.py              # IJarvisAgent - polls for due reminders, fires TTS
```

### Data Flow

```
Voice: "Remind me to call mom tomorrow at 3 PM"
  │
  ▼
Command Center (LLM extracts intent + date keys)
  │
  ▼
set_reminder command
  │  params: text="call mom", date_keys=["tomorrow"], time="15:00"
  │
  ▼
ReminderService.create_reminder()
  │  resolves date_keys → 2026-02-11T15:00:00-05:00
  │  persists to command_data table
  │
  ▼
CommandResponse.success_response(context_data={reminder_id, due_at, text})
  │
  ▼
LLM: "I've set a reminder to call mom tomorrow at 3 PM."
```

```
Background (every 30s):
  │
  ▼
ReminderAgent.run()
  │  queries command_data for due reminders (due_at <= now, announced=false)
  │
  ▼
For each due reminder:
  │  1. TTS: "Reminder: call mom"
  │  2. Mark as announced (one-shot) or advance next_due_at (recurring)
  │  3. Set snooze window (5 min)
  │
  ▼
User (optional): "Jarvis, snooze my reminder"
  │
  ▼
snooze_reminder command
  │  finds most recently announced reminder
  │  updates due_at += snooze_minutes
  │  resets announced=false
```

---

## Data Model

### Storage: `command_data` table (existing)

Reminders are stored in the existing `command_data` table using the `CommandDataRepository`.

| Field | Value |
|-------|-------|
| `command_name` | `"set_reminder"` |
| `data_key` | `"rem_{uuid8}"` (e.g., `"rem_a1b2c3d4"`) |
| `data` | JSON blob (see below) |
| `expires_at` | `NULL` for recurring; far-future for one-shot (cleanup after announced) |

### Reminder JSON Schema

```python
@dataclass
class ReminderData:
    reminder_id: str           # "rem_a1b2c3d4"
    text: str                  # "call mom"
    due_at: str                # ISO 8601 with timezone: "2026-02-11T15:00:00-05:00"
    created_at: str            # ISO 8601
    recurrence: str | None     # None, "daily", "weekly", "weekdays", "monthly"
    announced: bool            # True after TTS fires, False when pending
    snooze_until: str | None   # ISO 8601 - if snoozed, don't re-announce until this time
    announce_count: int        # How many times this reminder has been announced
    last_announced_at: str | None  # ISO 8601 - when last announced (for snooze window)
```

Example:
```json
{
  "reminder_id": "rem_a1b2c3d4",
  "text": "call mom",
  "due_at": "2026-02-11T15:00:00-05:00",
  "created_at": "2026-02-10T14:30:00-05:00",
  "recurrence": null,
  "announced": false,
  "snooze_until": null,
  "announce_count": 0,
  "last_announced_at": null
}
```

### Recurrence Model

When a recurring reminder fires:
1. Mark current occurrence as announced
2. Calculate `next_due_at` based on recurrence type
3. Update `due_at` to `next_due_at`, reset `announced=false`

| Recurrence | Next Due Calculation |
|-----------|---------------------|
| `daily` | `due_at + 1 day` |
| `weekly` | `due_at + 7 days` |
| `weekdays` | Next weekday (skip Sat/Sun) |
| `monthly` | Same day next month (handle month-end) |

---

## Commands

### 1. `set_reminder`

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `text` | string | yes | What to be reminded about |
| `date_keys` | string[] | no | Semantic date keys from date-key adapter (e.g., `["tomorrow", "morning"]`) |
| `time` | string | no | Explicit time in HH:MM format (24h). Used when LLM extracts a specific time. |
| `recurrence` | string | no | One of: `daily`, `weekly`, `weekdays`, `monthly` |

**Date Resolution:**

The `date_keys` parameter receives semantic keys extracted by the date-key adapter (e.g., `["tomorrow", "morning"]`). The command resolves these to an actual datetime using the date context dictionary provided by command-center.

When `date_keys` is empty or absent and `time` is provided, the reminder defaults to today (or tomorrow if the time has already passed today).

When neither `date_keys` nor `time` is provided, the command returns an error asking for clarification.

**Relative Time Support (requires upstream changes):**

Phrases like "in 2 hours" or "in 30 minutes" are NOT currently supported by the date-key adapter. See companion PRDs:
- `jarvis-command-center/prds/reminders-date-support.md`
- `jarvis-llm-proxy-api/prds/reminders-date-support.md`

Until relative time support is added, the `set_reminder` command also accepts a `relative_minutes` parameter as a fallback. The LLM can convert "in 2 hours" to `relative_minutes=120` directly (similar to how `set_timer` handles `duration_seconds`).

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `relative_minutes` | int | no | Minutes from now. Fallback for "in 30 minutes", "in 2 hours". |

**Resolution priority:**
1. `date_keys` + `time` (if both provided)
2. `date_keys` alone (uses default time from key, e.g., "morning" = 07:00)
3. `time` alone (today or tomorrow if past)
4. `relative_minutes` alone
5. Error: "When should I remind you?"

**Rules:**
- Always extract the reminder text from the voice command
- Convert relative time phrases to `relative_minutes` when date_keys don't capture them
- For "remind me in X minutes/hours", use `relative_minutes`
- For "remind me tomorrow at 3", use `date_keys=["tomorrow"]` + `time="15:00"`

**Critical Rules:**
- `text` is always required — if unclear, ask "What should I remind you about?"
- At least one time parameter is required (`date_keys`, `time`, or `relative_minutes`)

**Prompt Examples:**
```
"Remind me to call mom tomorrow at 3 PM"
  → {text: "call mom", date_keys: ["tomorrow"], time: "15:00"}

"Remind me to take out the trash in 30 minutes"
  → {text: "take out the trash", relative_minutes: 30}

"Set a reminder for Monday morning to buy groceries"
  → {text: "buy groceries", date_keys: ["next_monday", "morning"]}

"Remind me every day at 8 AM to take my medicine"
  → {text: "take my medicine", time: "08:00", recurrence: "daily"}

"Remind me to check the oven in 15 minutes"
  → {text: "check the oven", relative_minutes: 15}
```

**Adapter Training Examples (20+):**
```
"Remind me to call mom tomorrow at 3 PM"
  → {text: "call mom", date_keys: ["tomorrow"], time: "15:00"}
"Reminder to pick up dry cleaning on Monday"
  → {text: "pick up dry cleaning", date_keys: ["next_monday"]}
"Remind me in 30 minutes to check the laundry"
  → {text: "check the laundry", relative_minutes: 30}
"Set a reminder for tonight to take out the trash"
  → {text: "take out the trash", date_keys: ["tonight"]}
"Remind me every Monday at 9 AM to submit my timesheet"
  → {text: "submit my timesheet", date_keys: ["next_monday"], time: "09:00", recurrence: "weekly"}
"Remind me at 6 PM to start dinner"
  → {text: "start dinner", time: "18:00"}
"Remind me every day at 8 to take my medicine"
  → {text: "take my medicine", time: "08:00", recurrence: "daily"}
"Set a daily reminder at 7 AM to exercise"
  → {text: "exercise", time: "07:00", recurrence: "daily"}
"Remind me in 2 hours to move the car"
  → {text: "move the car", relative_minutes: 120}
"Remind me next Friday to pay rent"
  → {text: "pay rent", date_keys: ["next_friday"]}
"Set a reminder for tomorrow morning to water the plants"
  → {text: "water the plants", date_keys: ["tomorrow_morning"]}
"Remind me on weekdays at 8:30 to check email"
  → {text: "check email", time: "08:30", recurrence: "weekdays"}
"Remind me in an hour to call the dentist"
  → {text: "call the dentist", relative_minutes: 60}
"Remind me this weekend to clean the garage"
  → {text: "clean the garage", date_keys: ["this_weekend"]}
"Set a reminder for 5 PM today to leave work"
  → {text: "leave work", date_keys: ["today"], time: "17:00"}
"Remind me in 45 minutes to flip the chicken"
  → {text: "flip the chicken", relative_minutes: 45}
"Set a monthly reminder on the 1st to pay bills"
  → {text: "pay bills", recurrence: "monthly"}
"Remind me next Tuesday at noon to pick up the package"
  → {text: "pick up the package", date_keys: ["next_tuesday"], time: "12:00"}
"Remind me every morning to make the bed"
  → {text: "make the bed", date_keys: ["morning"], recurrence: "daily"}
"Remind me tomorrow evening to call grandma"
  → {text: "call grandma", date_keys: ["tomorrow_evening"]}
```

**Antipatterns:**
```
"Set a timer for 5 minutes" → set_timer (not set_reminder)
"What time is it?" → get_time (not set_reminder)
"Add eggs to my shopping list" → shopping_list (not set_reminder)
```

**Response:**
```python
CommandResponse.success_response(
    context_data={
        "reminder_id": "rem_a1b2c3d4",
        "text": "call mom",
        "due_at": "2026-02-11T15:00:00-05:00",
        "due_at_human": "tomorrow at 3:00 PM",
        "recurrence": None,
        "message": "Reminder set to call mom tomorrow at 3:00 PM"
    },
    wait_for_input=False
)
```

### 2. `list_reminders`

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `filter` | string | no | Filter by: `all` (default), `today`, `upcoming`, `recurring` |

**Prompt Examples:**
```
"What reminders do I have?"
  → {filter: "all"}
"Any reminders for today?"
  → {filter: "today"}
"Show my recurring reminders"
  → {filter: "recurring"}
"Do I have any upcoming reminders?"
  → {filter: "upcoming"}
```

**Response:**
```python
CommandResponse.success_response(
    context_data={
        "reminders": [
            {"reminder_id": "rem_a1b2c3d4", "text": "call mom", "due_at_human": "tomorrow at 3:00 PM", "recurrence": None},
            {"reminder_id": "rem_e5f6g7h8", "text": "take medicine", "due_at_human": "daily at 8:00 AM", "recurrence": "daily"},
        ],
        "count": 2,
        "message": "You have 2 reminders: call mom tomorrow at 3 PM, and take medicine daily at 8 AM."
    },
    wait_for_input=False
)
```

### 3. `delete_reminder`

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `reminder_id` | string | no | Specific reminder ID to delete |
| `text` | string | no | Fuzzy match on reminder text (e.g., "the one about mom") |
| `scope` | string | no | `one` (default), `all` — delete one or all reminders |

At least one of `reminder_id` or `text` must be provided (unless `scope=all`).

**Prompt Examples:**
```
"Cancel my reminder to call mom"
  → {text: "call mom"}
"Delete all my reminders"
  → {scope: "all"}
"Cancel the medicine reminder"
  → {text: "medicine"}
```

**Response:**
```python
CommandResponse.success_response(
    context_data={
        "deleted_count": 1,
        "deleted_reminders": [{"reminder_id": "rem_a1b2c3d4", "text": "call mom"}],
        "message": "Cancelled your reminder to call mom."
    },
    wait_for_input=False
)
```

### 4. `snooze_reminder`

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `minutes` | int | no | Snooze duration in minutes (default: 10) |
| `text` | string | no | Fuzzy match to identify which reminder to snooze |

**How snooze targeting works:**

When the user says "snooze my reminder" without specifying which one, the command finds the most recently announced reminder (by `last_announced_at` timestamp, within the snooze window). This works naturally because:
1. Agent announces a reminder → sets `last_announced_at`
2. User immediately says "Jarvis, snooze that"
3. Command finds the reminder with the most recent `last_announced_at`

If multiple reminders fired recently, the user can disambiguate: "snooze the one about mom".

**Prompt Examples:**
```
"Snooze that reminder"
  → {minutes: 10}
"Snooze for 30 minutes"
  → {minutes: 30}
"Snooze the reminder about mom for 15 minutes"
  → {text: "mom", minutes: 15}
```

**Snooze Window:**

A reminder is snoozable for 5 minutes after it fires. After that, the `snooze_reminder` command will respond with "No recently announced reminders to snooze."

**Response:**
```python
CommandResponse.success_response(
    context_data={
        "reminder_id": "rem_a1b2c3d4",
        "text": "call mom",
        "snoozed_until": "2026-02-11T15:10:00-05:00",
        "snoozed_until_human": "3:10 PM",
        "message": "Snoozed your reminder to call mom until 3:10 PM."
    },
    wait_for_input=False
)
```

---

## ReminderService

Singleton service following the `TimerService` pattern. Manages CRUD operations, recurrence logic, and snooze state.

### Interface

```python
class ReminderService:
    """Singleton service for managing reminders."""

    def create_reminder(
        self,
        text: str,
        due_at: datetime,
        recurrence: str | None = None,
    ) -> ReminderData: ...

    def get_reminder(self, reminder_id: str) -> ReminderData | None: ...

    def get_all_reminders(
        self, include_announced: bool = False
    ) -> list[ReminderData]: ...

    def get_due_reminders(self) -> list[ReminderData]:
        """Get reminders where due_at <= now and announced=False and snooze_until <= now."""
        ...

    def mark_announced(self, reminder_id: str) -> None:
        """Mark a one-shot reminder as announced. Advance recurring reminders."""
        ...

    def snooze_reminder(
        self, reminder_id: str, minutes: int = 10
    ) -> ReminderData: ...

    def delete_reminder(self, reminder_id: str) -> bool: ...

    def delete_all_reminders(self) -> int: ...

    def find_by_text(self, text: str) -> ReminderData | None:
        """Fuzzy match reminder by text (case-insensitive partial match)."""
        ...

    def find_most_recently_announced(
        self, window_minutes: int = 5
    ) -> ReminderData | None:
        """Find the most recently announced reminder within the snooze window."""
        ...

    def restore_reminders(self) -> int:
        """Restore reminders from database after restart. Fire any that were missed."""
        ...

    def cleanup_expired(self) -> int:
        """Delete one-shot reminders that have been announced and are past snooze window."""
        ...
```

### Persistence

Uses `CommandDataRepository` with `command_name="set_reminder"`:

```python
REMINDER_COMMAND_NAME = "set_reminder"

def _persist_reminder(self, reminder: ReminderData) -> None:
    with SessionLocal() as session:
        repo = CommandDataRepository(session)
        repo.save(
            command_name=REMINDER_COMMAND_NAME,
            data_key=reminder.reminder_id,
            data=reminder.to_dict(),
            expires_at=None,  # Don't auto-expire; managed by service
        )
```

Note: `expires_at=None` because the service manages lifecycle (recurring reminders never expire, one-shot reminders are cleaned up after announcement + snooze window).

### Thread Safety

- `_reminders: dict[str, ReminderData]` — in-memory cache
- `_reminders_lock: threading.Lock` — protects all reads/writes
- All DB operations happen inside the lock to prevent cache/DB divergence

### Startup Recovery

On startup, `restore_reminders()`:
1. Loads all reminders from `command_data` table
2. For reminders that were missed while down:
   - One-shot: fire TTS immediately, then mark as announced
   - Recurring: advance to next occurrence, skip missed ones
3. Populates in-memory cache

---

## ReminderAgent

Background agent that polls for due reminders and triggers TTS announcements.

### Implementation

The agent does NOT call TTS directly. Instead, it queues announcements for the voice listener to drain. This avoids cross-thread mic contention (see "Inline Listen Support" section for details).

```python
@dataclass
class PendingAnnouncement:
    reminder_id: str
    text: str        # Reminder text (for snooze targeting)
    message: str     # Full TTS message to speak


class ReminderAgent(IJarvisAgent):
    def __init__(self) -> None:
        self._pending_announcements: list[PendingAnnouncement] = []
        self._announcement_lock: threading.Lock = threading.Lock()

    @property
    def name(self) -> str:
        return "reminder_alerts"

    @property
    def description(self) -> str:
        return "Monitors reminders and queues TTS announcements when due"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=30, run_on_startup=True)

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return []  # No external services needed

    @property
    def include_in_context(self) -> bool:
        return False  # Side-effect only, no context injection

    async def run(self) -> None:
        service = get_reminder_service()
        due_reminders = service.get_due_reminders()

        for reminder in due_reminders:
            with self._announcement_lock:
                self._pending_announcements.append(
                    PendingAnnouncement(
                        reminder_id=reminder.reminder_id,
                        text=reminder.text,
                        message=f"Reminder: {reminder.text}. Would you like to snooze?",
                    )
                )
            service.mark_announced(reminder.reminder_id)

    def get_pending_announcements(self) -> list[PendingAnnouncement]:
        """Drain pending announcements. Called by voice listener."""
        with self._announcement_lock:
            announcements = list(self._pending_announcements)
            self._pending_announcements.clear()
            return announcements

    def get_context_data(self) -> dict[str, Any]:
        return {}
```

### Polling Interval

30 seconds. This means reminders fire within 0-30 seconds of their due time. Acceptable for voice reminders (unlike calendar apps that need second-precision).

### TTS Priority

Uses `tts.speak(True, message)` — the `True` flag indicates alert/interrupt priority, same as timer completion.

---

## Inline Listen Support

### Problem: `wait_for_input` is half-wired

The plumbing for multi-turn follow-up exists in `CommandExecutionService`:
- Commands return `CommandResponse(wait_for_input=True)`
- The conversation loop pauses and returns `{"wait_for_input": True, "conversation_id": "..."}`
- `continue_conversation(conversation_id, message)` resumes the conversation

But `voice_listener.py` ignores the signal. After `speak_result(result)`, it goes straight back to wake word detection regardless of `wait_for_input`. This means any command that asks a follow-up question requires the user to say the wake word again to respond — breaking the conversational flow.

### Solution: Inline listen in voice_listener.py

**This is a general-purpose enhancement, not reminder-specific.** Any command or agent that uses `wait_for_input=True` benefits from this.

#### Changes to `voice_listener.py`

After `command_service.speak_result(result)`, check the result:

```python
result = command_service.process_voice_command(transcription, validation_handler)
command_service.speak_result(result)

# NEW: If the command wants follow-up input, listen without wake word
while result and result.get("wait_for_input"):
    logger.info("Command requested follow-up, listening inline",
                conversation_id=result.get("conversation_id"))

    # Listen for audio (no wake word required)
    follow_up_audio = listen()
    follow_up_text = stt_provider.transcribe(follow_up_audio)

    if not follow_up_text:
        logger.warning("Failed to transcribe follow-up, ending conversation")
        break

    logger.info("Follow-up received", text=follow_up_text)

    # Continue the existing conversation
    result = command_service.continue_conversation(
        result["conversation_id"],
        follow_up_text,
        validation_handler,
    )
    command_service.speak_result(result)
```

This is a tight loop: speak → listen → continue → speak → ... until `wait_for_input=False`.

#### Timeout

The `listen()` function already has a silence-based timeout (stops recording after N seconds of silence). This naturally handles the case where the user doesn't respond — the inline listen times out, transcription returns empty/garbage, and the loop exits.

For additional safety, add a max inline listen duration (e.g., 15 seconds) to prevent the voice listener from blocking indefinitely.

### Agent-Initiated Inline Listen

The `ReminderAgent` runs in a background thread and needs to trigger TTS + inline listen on the main thread (which owns the mic). This requires cross-thread coordination.

#### Mechanism: Pending Announcement Queue

```python
# In reminder_agent.py
class ReminderAgent(IJarvisAgent):
    _pending_announcements: list[PendingAnnouncement] = []
    _announcement_lock: threading.Lock = threading.Lock()

    async def run(self) -> None:
        service = get_reminder_service()
        due_reminders = service.get_due_reminders()

        for reminder in due_reminders:
            with self._announcement_lock:
                self._pending_announcements.append(
                    PendingAnnouncement(
                        reminder_id=reminder.reminder_id,
                        text=reminder.text,
                        message=f"Reminder: {reminder.text}. Would you like to snooze?",
                    )
                )
            service.mark_announced(reminder.reminder_id)

    def get_pending_announcements(self) -> list[PendingAnnouncement]:
        """Called by voice listener to drain pending announcements."""
        with self._announcement_lock:
            announcements = list(self._pending_announcements)
            self._pending_announcements.clear()
            return announcements
```

```python
@dataclass
class PendingAnnouncement:
    reminder_id: str
    text: str        # Reminder text for context
    message: str     # TTS message to speak
```

#### Voice Listener Integration

The voice listener checks for pending announcements in its main loop, between wake word detection cycles:

```python
# In voice_listener.py main loop
while True:
    # Check for pending agent announcements
    announcements = reminder_agent.get_pending_announcements()
    for announcement in announcements:
        # Pause wake word detection
        close_audio(audio_stream)

        # Speak the reminder
        tts_provider.speak(True, announcement.message)

        # Inline listen for snooze response (with timeout)
        follow_up_audio = listen(max_duration_seconds=8)
        follow_up_text = stt_provider.transcribe(follow_up_audio)

        if follow_up_text:
            # Route through snooze command via command execution
            result = command_service.process_voice_command(
                follow_up_text,
                validation_handler,
            )
            command_service.speak_result(result)

        # Resume wake word detection
        audio_stream = create_audio_stream()

    # Normal wake word detection
    raw_data = audio_stream.read(...)
    # ... existing porcupine logic
```

This keeps the voice listener as the single owner of the mic. The agent queues announcements; the voice listener drains them at safe points in its loop.

#### Why a Queue (Not Direct TTS)

If the agent calls `tts.speak()` directly while the voice listener has the mic open, there's a race:
- Audio stream is recording → TTS plays through speaker → mic picks up the TTS audio
- The agent can't pause/resume the audio stream (wrong thread)

By queuing announcements and letting the voice listener handle them, we ensure:
1. Audio stream is closed before TTS plays
2. Inline listen starts after TTS finishes
3. Audio stream reopens after the interaction is complete
4. No cross-thread mic contention

### Snooze Flow (End-to-End)

```
ReminderAgent.run() detects due reminder
  │
  ▼
Queues PendingAnnouncement("Reminder: call mom. Would you like to snooze?")
  │
  ▼
Voice listener drains queue (between wake word cycles)
  │  pauses audio stream
  │
  ▼
TTS: "Reminder: call mom. Would you like to snooze?"
  │
  ▼
Inline listen (no wake word, ~8 second timeout)
  │
  ├─ User says "snooze" or "snooze for 20 minutes"
  │    ▼
  │  process_voice_command("snooze for 20 minutes")
  │    ▼
  │  LLM routes to snooze_reminder command
  │    ▼
  │  ReminderService.snooze_reminder(id, 20)
  │    ▼
  │  TTS: "Snoozed your reminder to call mom until 3:20 PM"
  │
  ├─ User says "dismiss" or "got it" or "thanks"
  │    ▼
  │  process_voice_command("dismiss")
  │    ▼
  │  LLM: "Got it!" (no tool call needed, reminder already announced)
  │
  └─ Silence (timeout)
       ▼
     No action, resume wake word detection
```

### Fallback: Wake-Word Snooze

Even with inline listen, wake-word snooze still works. If the user misses the inline window, they can say "Jarvis, snooze my reminder" within the 5-minute snooze window. The `snooze_reminder` command finds the most recently announced reminder regardless of how it's triggered.

---

## Integration Points

### main.py Startup

```python
# After timer service init:
reminder_service = initialize_reminder_service()
restored = reminder_service.restore_reminders()
logger.info("Restored reminders", count=restored)

# Agent scheduler handles ReminderAgent automatically (auto-discovered)
```

### Date Context

The `set_reminder` command needs access to the date context dictionary to resolve date keys. This is available via `CommandExecutionService` which passes it during tool registration.

The command's `run()` method receives resolved parameters from the LLM. Date keys are resolved by command-center before the tool call reaches the node. The command receives a resolved `date_keys` array that it maps to datetimes using the same date context dictionary.

**Important:** The command must access the date context to resolve date keys to actual datetimes. This is injected via `RequestInformation` or fetched from the command center client. See implementation details in the code.

### Timer vs Reminder Disambiguation

The `set_timer` command currently has "remind" and "reminder" in its keywords, which could cause confusion. Update:

1. Add antipatterns to `set_timer`:
   ```python
   @property
   def antipatterns(self) -> list[str]:
       return ["remind me tomorrow", "reminder for Monday", "remind me at 3 PM"]
   ```

2. Add antipatterns to `set_reminder`:
   ```python
   @property
   def antipatterns(self) -> list[str]:
       return ["set a timer for 5 minutes", "timer for 30 seconds", "countdown"]
   ```

3. Remove "remind" and "reminder" from timer keywords.

**Disambiguation rule:** If the user specifies a duration ("in 5 minutes"), it's a timer. If they specify an absolute time or date ("at 3 PM", "tomorrow"), it's a reminder. Edge case: "remind me in 30 minutes" — this is a reminder (uses `relative_minutes`), not a timer. Timers are for cooking/exercise/short countdowns. Reminders are for "don't forget to do X."

---

## TDD Test Plan

All development follows RED → GREEN → REFACTOR.

### Phase 1: ReminderService (unit tests)

Test file: `tests/services/test_reminder_service.py`

```
RED → GREEN for each:

1. test_create_reminder_basic
   - Create a reminder with text and due_at
   - Assert returns ReminderData with correct fields
   - Assert persisted to database

2. test_create_reminder_with_recurrence
   - Create with recurrence="daily"
   - Assert recurrence stored correctly

3. test_get_reminder_by_id
   - Create, then get by ID
   - Assert matches

4. test_get_reminder_not_found
   - Get nonexistent ID
   - Assert returns None

5. test_get_all_reminders
   - Create 3 reminders
   - Assert returns all 3

6. test_get_all_excludes_announced_by_default
   - Create 2 reminders, mark 1 announced
   - Assert returns only 1

7. test_get_due_reminders
   - Create reminder with due_at in the past
   - Assert appears in get_due_reminders()

8. test_get_due_reminders_excludes_future
   - Create reminder with due_at in the future
   - Assert NOT in get_due_reminders()

9. test_get_due_reminders_respects_snooze
   - Create reminder with due_at in past, snooze_until in future
   - Assert NOT in get_due_reminders()

10. test_mark_announced_one_shot
    - Create one-shot, mark announced
    - Assert announced=True, last_announced_at set

11. test_mark_announced_recurring_daily
    - Create daily reminder, mark announced
    - Assert due_at advanced by 1 day, announced=False

12. test_mark_announced_recurring_weekly
    - Create weekly reminder, mark announced
    - Assert due_at advanced by 7 days

13. test_mark_announced_recurring_weekdays
    - Create weekday reminder on Friday, mark announced
    - Assert due_at advances to Monday (skips weekend)

14. test_mark_announced_recurring_monthly
    - Create monthly reminder, mark announced
    - Assert due_at advanced by 1 month

15. test_snooze_reminder
    - Create, announce, snooze for 10 min
    - Assert snooze_until set, announced=False

16. test_snooze_reminder_custom_duration
    - Snooze for 30 min
    - Assert snooze_until = now + 30 min

17. test_delete_reminder
    - Create, delete
    - Assert removed from DB and memory

18. test_delete_all_reminders
    - Create 3, delete all
    - Assert all gone

19. test_find_by_text_exact
    - Create "call mom", find by "call mom"
    - Assert found

20. test_find_by_text_partial
    - Create "call mom", find by "mom"
    - Assert found

21. test_find_by_text_case_insensitive
    - Create "Call Mom", find by "call mom"
    - Assert found

22. test_find_most_recently_announced
    - Announce 2 reminders at different times
    - Assert returns the more recent one

23. test_find_most_recently_announced_outside_window
    - Announce reminder 10 min ago, window is 5 min
    - Assert returns None

24. test_restore_reminders_active
    - Persist to DB, create fresh service, restore
    - Assert reminders loaded into memory

25. test_restore_reminders_missed_one_shot
    - Persist one-shot with past due_at, restore
    - Assert TTS callback fires

26. test_restore_reminders_missed_recurring
    - Persist recurring with past due_at, restore
    - Assert due_at advanced to next future occurrence

27. test_cleanup_expired
    - Create announced one-shot past snooze window
    - Assert cleanup removes it

28. test_thread_safety
    - Concurrent create/read operations
    - Assert no race conditions
```

### Phase 2: Commands (unit tests)

Test file: `tests/commands/test_set_reminder_command.py`

```
1. test_set_reminder_with_date_keys_and_time
2. test_set_reminder_with_relative_minutes
3. test_set_reminder_with_time_only_future_today
4. test_set_reminder_with_time_only_past_today (should set tomorrow)
5. test_set_reminder_with_recurrence
6. test_set_reminder_missing_text_returns_error
7. test_set_reminder_missing_time_returns_error
8. test_set_reminder_prompt_examples_valid
9. test_set_reminder_adapter_examples_valid
```

Test file: `tests/commands/test_list_reminders_command.py`

```
1. test_list_all_reminders
2. test_list_today_reminders
3. test_list_recurring_reminders
4. test_list_no_reminders
5. test_list_prompt_examples_valid
```

Test file: `tests/commands/test_delete_reminder_command.py`

```
1. test_delete_by_id
2. test_delete_by_text
3. test_delete_all
4. test_delete_not_found
5. test_delete_prompt_examples_valid
```

Test file: `tests/commands/test_snooze_reminder_command.py`

```
1. test_snooze_default_10_minutes
2. test_snooze_custom_duration
3. test_snooze_by_text
4. test_snooze_no_recent_reminder
5. test_snooze_prompt_examples_valid
```

### Phase 3: Agent + Announcement Queue (unit tests)

Test file: `tests/agents/test_reminder_agent.py`

```
1. test_agent_properties (name, schedule, include_in_context=False)
2. test_run_no_due_reminders (no announcements queued)
3. test_run_queues_due_reminder
   - Due reminder exists
   - Assert PendingAnnouncement added to queue
4. test_run_marks_announced_after_queuing
5. test_run_advances_recurring_after_queuing
6. test_run_multiple_due_reminders (all queued)
7. test_get_pending_announcements_drains_queue
   - Queue 2 announcements, call get_pending_announcements()
   - Assert returns both, queue is now empty
8. test_get_pending_announcements_empty
   - No announcements queued
   - Assert returns empty list
9. test_announcement_queue_thread_safety
   - Concurrent run() and get_pending_announcements()
   - Assert no race conditions
```

### Phase 4: Inline Listen (unit tests)

Test file: `tests/scripts/test_voice_listener_inline.py`

These tests mock TTS, STT, and CommandExecutionService to test the inline listen logic in isolation.

```
1. test_inline_listen_on_wait_for_input
   - process_voice_command returns {wait_for_input: True, conversation_id: "abc"}
   - Mock listen() returns audio, transcribe returns "yes"
   - Assert continue_conversation("abc", "yes") is called
   - Assert speak_result called for follow-up response

2. test_inline_listen_exits_on_wait_for_input_false
   - First result has wait_for_input=True
   - continue_conversation returns wait_for_input=False
   - Assert loop exits after one follow-up

3. test_inline_listen_exits_on_empty_transcription
   - wait_for_input=True but transcribe returns None
   - Assert loop exits, no continue_conversation call

4. test_inline_listen_multi_turn
   - Two rounds of wait_for_input=True, third returns False
   - Assert continue_conversation called twice

5. test_agent_announcement_drain
   - reminder_agent has 1 pending announcement
   - Assert audio stream closed before TTS
   - Assert TTS speaks announcement message
   - Assert listen() called for inline response
   - Assert audio stream reopened after

6. test_agent_announcement_snooze_response
   - Pending announcement exists
   - Inline listen transcribes "snooze for 20 minutes"
   - Assert process_voice_command("snooze for 20 minutes") called
   - Assert speak_result called with snooze confirmation

7. test_agent_announcement_silence_timeout
   - Pending announcement exists
   - listen() returns audio, transcribe returns None (silence/garbage)
   - Assert no command processed, audio stream reopened

8. test_agent_announcement_dismiss_response
   - Inline listen transcribes "got it"
   - Assert process_voice_command("got it") called
   - Assert no snooze_reminder invoked

9. test_no_announcements_skips_drain
   - get_pending_announcements() returns empty
   - Assert no TTS, no listen, normal wake word flow
```

### Phase 5: Integration

Test file: `tests/integration/test_reminders_integration.py`

```
1. test_full_lifecycle_create_fire_cleanup
2. test_recurring_lifecycle_create_fire_advance_fire
3. test_snooze_lifecycle_create_fire_snooze_refire
4. test_inline_snooze_flow (mock TTS/STT, verify queue → announce → snooze)
5. test_restore_after_restart
```

---

## Upstream Dependencies

### Date Key Support for Relative Times

The current date-key adapter supports semantic keys (`tomorrow`, `morning`, `next_monday`) but NOT relative time expressions (`in 30 minutes`, `in 2 hours`).

**Workaround:** The `relative_minutes` parameter lets the LLM handle conversion directly (same pattern as `set_timer` with `duration_seconds`). This is a temporary shim — once relative time keys land, remove `relative_minutes` entirely.

**Upstream work:** Add relative time keys to the date-key adapter. See companion PRDs:
- `jarvis-command-center/prds/reminders-date-support.md`
- `jarvis-llm-proxy-api/prds/reminders-date-support.md`

### Date Context Access in Commands

Commands currently receive resolved tool parameters from the LLM, but date key resolution happens in command-center. The node-side command receives `date_keys` as a string array and must resolve them locally using the date context dictionary.

The date context is already fetched during `CommandExecutionService.register_tools_for_conversation()`. It needs to be made accessible to commands via `RequestInformation` or a service method.

---

## Implementation Order

1. **ReminderService** — core logic, fully unit tested
2. **SetReminderCommand** — create reminders (with `relative_minutes` fallback)
3. **ListRemindersCommand** — query reminders
4. **DeleteReminderCommand** — cancel reminders
5. **SnoozeReminderCommand** — snooze fired reminders
6. **ReminderAgent** — background polling + announcement queue
7. **Inline listen (voice_listener.py)** — `wait_for_input` loop + agent announcement drain
8. **main.py integration** — startup init + restore
9. **Timer keyword cleanup** — remove "remind"/"reminder" from timer

Steps 6 and 7 are tightly coupled and should be developed together. The inline listen support is a general-purpose enhancement — once wired up, any command or agent can use it.

---

## Out of Scope (Future)

- **Location-based reminders** ("remind me when I get home")
- **Shared reminders** across nodes
- **Calendar integration** (sync reminders with Google Calendar, etc.)
- **Natural language deletion** ("cancel the reminder I just set") — could be added as a multi-turn flow
