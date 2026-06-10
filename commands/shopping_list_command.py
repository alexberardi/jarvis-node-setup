"""Shopping list command for Jarvis.

Household shopping list: add items, read the list, check items off,
remove items, or clear the list. Items persist as one record per item so
the mobile data browser renders them with a check-off toggle.

Records are intentionally household-shared: they carry no `user_id`
field, so the command-data browser shows the same list to every
household member (`added_by` records who added an item without scoping
visibility).
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    FieldSpec,
    IJarvisCommand,
    JarvisStorage,
    PreRouteResult,
    RecordSummary,
)
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation

logger = JarvisLogger(service="jarvis-node")

STORAGE_NAME = "shopping_list"

_ALL_ACTIONS = ["add", "read", "check_off", "remove", "clear"]

_storage: JarvisStorage | None = None


def _get_storage() -> JarvisStorage:
    global _storage
    if _storage is None:
        _storage = JarvisStorage(STORAGE_NAME)
    return _storage


# Leading filler that voice transcripts attach to item names ("some milk",
# "a thing of butter"). Stripped per item, not per utterance.
_LEADING_FILLER_RE = re.compile(
    r"^(?:some|a\s+few|a\s+couple(?:\s+of)?|a\s+thing\s+of|a|an|the)\s+",
    re.IGNORECASE,
)


def _split_items(text: str) -> List[str]:
    """Split a spoken item phrase into individual items.

    'milk, eggs, and butter' -> ['milk', 'eggs', 'butter']
    """
    parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", text, flags=re.IGNORECASE)
    items: List[str] = []
    for part in parts:
        cleaned = _LEADING_FILLER_RE.sub("", part.strip()).strip()
        if cleaned:
            items.append(cleaned)
    return items


def _item_key(name: str) -> str:
    """Stable storage key for an item name ('Almond Butter' -> 'almond_butter')."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _spoken_join(items: List[str]) -> str:
    """Join item names for speech: 'milk', 'milk and eggs', 'milk, eggs, and butter'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# --- Pre-route patterns ---
# Every pattern requires the literal shopping/grocery-list noun so these
# can never steal reminder ("remind me to buy milk") or music ("add X to
# my playlist") utterances.

_LIST_NOUN = r"(?:my\s+|the\s+|our\s+)?(?:shopping|grocery|groceries)\s+list"

_ADD_RE = re.compile(
    rf"^\s*(?:add|put)\s+(?P<items>.+?)\s+(?:to|on(?:to)?)\s+{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

_READ_RE = re.compile(
    rf"^\s*(?:what(?:'s|\s+is)\s+on|read(?:\s+me)?|show(?:\s+me)?|list)\s+{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

_CHECK_OFF_RE = re.compile(
    rf"^\s*(?:check(?:\s+off)?|cross(?:\s+off|\s+out)?)\s+(?P<items>.+?)"
    rf"\s+(?:off\s+)?(?:(?:of|from|on)\s+)?{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

_REMOVE_RE = re.compile(
    rf"^\s*(?:remove|take|delete)\s+(?P<items>.+?)\s+(?:off|from)\s+(?:of\s+)?{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

_CLEAR_RE = re.compile(
    rf"^\s*(?:clear|empty|wipe)\s+{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)


class ShoppingListCommand(IJarvisCommand):
    """Manage the household shopping list."""

    @property
    def command_name(self) -> str:
        return "shopping_list"

    @property
    def description(self) -> str:
        return (
            "Manage the household shopping list: add items, read the list, "
            "check items off, remove items, or clear it. "
            "Use for 'add milk to the shopping list', 'what's on the grocery list'. "
            "NOT for time-based reminders ('remind me to buy milk tomorrow') or playlists."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "shopping list", "grocery list", "groceries", "shopping",
            "add to list", "grocery", "check off", "need from the store",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=True,
                description=(
                    "add=put items on the list, read=say what's on the list, "
                    "check_off=mark items as bought, remove=delete items, clear=delete everything"
                ),
                enum_values=_ALL_ACTIONS,
            ),
            JarvisParameter(
                "items", "array<string>", required=False,
                description=(
                    "Item names. Always a list, even for one item "
                    "(required for add, check_off, and remove)"
                ),
            ),
            JarvisParameter(
                "regular", "bool", required=False,
                description=(
                    "Set true ONLY when the user says the items are regulars/"
                    "staples/always-buy items ('add milk as a regular', "
                    "'make diapers a regular')"
                ),
            ),
            JarvisParameter(
                "scope", "string", required=False,
                description=(
                    "For clear: 'trip' (default) removes one-off items and puts "
                    "regulars back on the list unchecked; 'everything' wipes the "
                    "whole list including regulars"
                ),
                enum_values=["trip", "everything"],
            ),
        ]

    @property
    def required_secrets(self) -> List["IJarvisSecret"]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "items is ALWAYS an array of strings, even for a single item",
            "Split compound requests: 'add milk, eggs, and butter' -> items=['milk', 'eggs', 'butter']",
            "Default action is 'add' when items are mentioned, 'read' when just asking about the list",
            "Strip quantities and filler into the item name itself ('2 gallons of milk' -> 'milk' unless the quantity matters)",
            "Regulars are items the household buys every trip — set regular=true only when the user calls them regulars/staples",
            "'clear the shopping list' = scope 'trip' (regulars come back); only use scope='everything' when the user explicitly includes regulars",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "items is required for add, check_off, and remove — if unclear, ask which items",
            "This manages a shopping list, NOT time-based reminders — 'remind me to buy milk' belongs to the reminder command",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="reminder",
                description=(
                    "Time-based 'remind me to buy milk tomorrow' / 'remind me to "
                    "pick up eggs at 5' — those are reminders, not list edits."
                ),
            ),
        ]

    # ── Mobile command-data browser ───────────────────────────────────────

    def editable_fields(self) -> List[FieldSpec]:
        return [
            # Read-only on mobile: renaming would desync the display name
            # from the storage key voice add/check-off resolve against.
            FieldSpec("item", "string", label="Item", required=True, editable=False),
            FieldSpec("checked", "bool", label="Checked off"),
            FieldSpec("regular", "bool", label="Regular item"),
            FieldSpec("quantity", "int", label="Quantity"),
            FieldSpec("added_by", "user_ref", label="Added by", editable=False),
            FieldSpec("added_at", "datetime", label="Added", editable=False),
            FieldSpec("checked_at", "datetime", label="Checked", editable=False),
        ]

    def display_summary(self, record: dict) -> RecordSummary:
        item = str(record.get("item") or "Item")
        checked = bool(record.get("checked"))
        title = f"✓ {item}" if checked else item
        parts = []
        if checked:
            parts.append("Checked off")
        if record.get("regular"):
            parts.append("Regular")
        quantity = int(record.get("quantity") or 1)
        if quantity > 1:
            parts.append(f"× {quantity}")
        subtitle = " • ".join(parts) if parts else None
        return RecordSummary(title=title, subtitle=subtitle, icon="cart")

    # ── Examples ──────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                "Add milk and eggs to the shopping list",
                {"action": "add", "items": ["milk", "eggs"]},
                is_primary=True,
            ),
            CommandExample(
                "Put almond butter on the grocery list",
                {"action": "add", "items": ["almond butter"]},
            ),
            CommandExample(
                "What's on my shopping list?",
                {"action": "read"},
            ),
            CommandExample(
                "Check off the milk",
                {"action": "check_off", "items": ["milk"]},
            ),
            CommandExample(
                "Remove eggs from the grocery list",
                {"action": "remove", "items": ["eggs"]},
            ),
            CommandExample(
                "Add diapers, wipes, and bananas to the list",
                {"action": "add", "items": ["diapers", "wipes", "bananas"]},
            ),
            CommandExample(
                "Add milk to the shopping list as a regular",
                {"action": "add", "items": ["milk"], "regular": True},
            ),
            CommandExample(
                "Clear the shopping list",
                {"action": "clear"},
            ),
            CommandExample(
                "Clear the shopping list including the regulars",
                {"action": "clear", "scope": "everything"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items: list[tuple[str, dict[str, Any], bool]] = [
            # Add — single
            ("Add milk to the shopping list", {"action": "add", "items": ["milk"]}, True),
            ("Put bananas on the grocery list", {"action": "add", "items": ["bananas"]}, False),
            ("Add paper towels to my shopping list", {"action": "add", "items": ["paper towels"]}, False),
            ("Can you add chicken to the grocery list", {"action": "add", "items": ["chicken"]}, False),
            ("Put dish soap on our shopping list", {"action": "add", "items": ["dish soap"]}, False),
            ("Add diapers to the list", {"action": "add", "items": ["diapers"]}, False),
            # Add — compound
            ("Add milk, eggs, and butter to the shopping list", {"action": "add", "items": ["milk", "eggs", "butter"]}, False),
            ("Put apples and oranges on the grocery list", {"action": "add", "items": ["apples", "oranges"]}, False),
            ("Add coffee, sugar, and creamer to my list", {"action": "add", "items": ["coffee", "sugar", "creamer"]}, False),
            # Read
            ("What's on my shopping list?", {"action": "read"}, False),
            ("Read me the grocery list", {"action": "read"}, False),
            ("What do we need from the store?", {"action": "read"}, False),
            ("Show me the shopping list", {"action": "read"}, False),
            # Check off
            ("Check off the milk", {"action": "check_off", "items": ["milk"]}, False),
            ("Check eggs off the shopping list", {"action": "check_off", "items": ["eggs"]}, False),
            ("Cross bananas off the grocery list", {"action": "check_off", "items": ["bananas"]}, False),
            ("Mark the bread as bought", {"action": "check_off", "items": ["bread"]}, False),
            # Remove
            ("Remove eggs from the shopping list", {"action": "remove", "items": ["eggs"]}, False),
            ("Take chicken off the grocery list", {"action": "remove", "items": ["chicken"]}, False),
            # Regulars
            ("Add milk to the shopping list as a regular", {"action": "add", "items": ["milk"], "regular": True}, False),
            ("Make diapers a regular on the shopping list", {"action": "add", "items": ["diapers"], "regular": True}, False),
            ("Add coffee as a staple to the grocery list", {"action": "add", "items": ["coffee"], "regular": True}, False),
            # Clear
            ("Clear the shopping list", {"action": "clear"}, False),
            ("Empty my grocery list", {"action": "clear"}, False),
            ("Clear the whole shopping list including regulars", {"action": "clear", "scope": "everything"}, False),
        ]
        return [
            CommandExample(voice, params, is_primary)
            for voice, params, is_primary in items
        ]

    # ── Pre-route ─────────────────────────────────────────────────────
    # Every shopping-list verb is deterministic when the utterance names
    # the list explicitly, so all five actions get fast-paths. Fuzzy
    # shapes ("we're out of milk") fall through to the LLM.

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="shopping_list.add",
                description="Bypass LLM for 'add X to the shopping list'",
                example="add milk to the shopping list",
                regex=_ADD_RE.pattern,
                handler="_fp_add",
            ),
            FastPathPattern(
                id="shopping_list.read",
                description="Bypass LLM for 'what's on the shopping list'",
                example="what's on the shopping list",
                regex=_READ_RE.pattern,
                handler="_fp_read",
            ),
            FastPathPattern(
                id="shopping_list.check_off",
                description="Bypass LLM for 'check X off the shopping list'",
                example="check milk off the shopping list",
                regex=_CHECK_OFF_RE.pattern,
                handler="_fp_check_off",
            ),
            FastPathPattern(
                id="shopping_list.remove",
                description="Bypass LLM for 'remove X from the shopping list'",
                example="remove milk from the shopping list",
                regex=_REMOVE_RE.pattern,
                handler="_fp_remove",
            ),
            FastPathPattern(
                id="shopping_list.clear",
                description="Bypass LLM for 'clear the shopping list'",
                example="clear the shopping list",
                regex=_CLEAR_RE.pattern,
                handler="_fp_clear",
            ),
        ]

    def _fp_add(self, match, voice_command: str) -> PreRouteResult | None:
        items = _split_items(match.group("items"))
        if not items:
            return None
        return PreRouteResult(arguments={"action": "add", "items": items})

    def _fp_read(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "read"})

    def _fp_check_off(self, match, voice_command: str) -> PreRouteResult | None:
        items = _split_items(match.group("items"))
        if not items:
            return None
        return PreRouteResult(arguments={"action": "check_off", "items": items})

    def _fp_remove(self, match, voice_command: str) -> PreRouteResult | None:
        items = _split_items(match.group("items"))
        if not items:
            return None
        return PreRouteResult(arguments={"action": "remove", "items": items})

    def _fp_clear(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "clear"})

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        items = args.get("items")
        if isinstance(items, str):
            args["items"] = _split_items(items)
        elif isinstance(items, list):
            # Respect the LLM's segmentation — re-splitting array entries
            # would shred compound names like "mac and cheese".
            args["items"] = [str(entry).strip() for entry in items if str(entry).strip()]
        if not args.get("action"):
            args["action"] = "add" if args.get("items") else "read"
        # Small models emit booleans as strings; only honor an explicit true.
        regular = args.get("regular")
        if isinstance(regular, str):
            args["regular"] = regular.strip().lower() in ("true", "yes", "1")
        scope = args.get("scope")
        if scope is not None and scope not in ("trip", "everything"):
            args.pop("scope", None)
        return args

    # ── Main execution ────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "read")
        raw_items = kwargs.get("items") or []
        items = [str(i).strip() for i in raw_items if str(i).strip()]

        if action in ("add", "check_off", "remove") and not items:
            question = "Which items?"
            return CommandResponse.error_response(
                error_details=question,
                context_data={"error": "missing_items", "message": question},
                wait_for_input=True,
            )

        added_by = request_info.user_id if request_info is not None else None

        if action == "add":
            return self._run_add(items, added_by, regular=bool(kwargs.get("regular")))
        elif action == "read":
            return self._run_read()
        elif action == "check_off":
            return self._run_check_off(items)
        elif action == "remove":
            return self._run_remove(items)
        elif action == "clear":
            return self._run_clear(scope=kwargs.get("scope") or "trip")
        else:
            return CommandResponse.error_response(
                error_details=f"Unknown shopping list action: {action}",
                context_data={"message": f"I don't know the shopping list action '{action}'."},
            )

    # ── Add ───────────────────────────────────────────────────────────

    def _run_add(
        self, items: List[str], added_by: int | None, regular: bool = False
    ) -> CommandResponse:
        storage = _get_storage()
        now = datetime.now(timezone.utc).isoformat()

        added: List[str] = []
        promoted: List[str] = []
        already: List[str] = []
        for item in items:
            key = _item_key(item)
            if not key:
                continue
            existing = storage.get(key)
            if existing and not existing.get("checked"):
                # Already on the list — promote to regular if asked, else no-op.
                if regular and not existing.get("regular"):
                    existing["regular"] = True
                    storage.save(key, existing)
                    promoted.append(existing.get("item") or item)
                else:
                    already.append(existing.get("item") or item)
                continue
            # Re-adding a checked item preserves its regular status and
            # standing quantity (the default the export screen seeds from).
            was_regular = bool(existing.get("regular")) if existing else False
            was_quantity = int(existing.get("quantity") or 1) if existing else 1
            record: Dict[str, Any] = {
                "item": item,
                "checked": False,
                "regular": regular or was_regular,
                "quantity": was_quantity,
                "added_at": now,
                "checked_at": None,
            }
            if added_by is not None:
                record["added_by"] = added_by
            storage.save(key, record)
            added.append(item)

        sentences: List[str] = []
        if added:
            suffix = " as regulars" if regular and len(added) > 1 else (
                " as a regular" if regular else ""
            )
            sentences.append(f"Added {_spoken_join(added)} to your shopping list{suffix}.")
        if promoted:
            sentences.append(
                f"Made {_spoken_join(promoted)} "
                f"{'a regular' if len(promoted) == 1 else 'regulars'}."
            )
        if already:
            sentences.append(
                f"{_spoken_join(already).capitalize()} "
                f"{'is' if len(already) == 1 else 'are'} already on it."
            )
        if not sentences:
            return CommandResponse.error_response(
                error_details="I couldn't make out any items to add.",
                context_data={"message": "I couldn't make out any items to add."},
            )

        message = " ".join(sentences)
        logger.info(
            f"Shopping list add: {added} (promoted: {promoted}, already present: {already})"
        )
        return CommandResponse.success_response(
            context_data={
                "message": message,
                "added": added,
                "promoted_to_regular": promoted,
                "already_on_list": already,
            },
            wait_for_input=False,
        )

    # ── Read ──────────────────────────────────────────────────────────

    def _run_read(self) -> CommandResponse:
        records = _get_storage().get_all()
        open_records = [r for r in records if r.get("item") and not r.get("checked")]
        open_records.sort(key=lambda r: str(r.get("item")).lower())
        open_items = []
        for r in open_records:
            quantity = int(r.get("quantity") or 1)
            name = str(r.get("item"))
            open_items.append(f"{name} times {quantity}" if quantity > 1 else name)

        if not open_items:
            message = "Your shopping list is empty."
        elif len(open_items) == 1:
            message = f"You have one item on your shopping list: {open_items[0]}."
        else:
            message = (
                f"You have {len(open_items)} items on your shopping list: "
                f"{_spoken_join(open_items)}."
            )

        return CommandResponse.success_response(
            context_data={"message": message, "items": open_items},
            wait_for_input=False,
        )

    # ── Check off / remove ────────────────────────────────────────────

    @staticmethod
    def _normalize_record(record: Dict[str, Any], key: str) -> Dict[str, Any]:
        """Strip repository meta fields and attach the real storage key."""
        record.pop("_data_key", None)
        record.pop("_expires_at", None)
        record["data_key"] = key
        return record

    def _find_record(self, item: str) -> Dict[str, Any] | None:
        """Find a record by exact key, falling back to substring match.

        The fallback iterates in key order so a fuzzy match is deterministic
        when several items contain the needle.
        """
        storage = _get_storage()
        key = _item_key(item)
        exact = storage.get(key)
        if exact is not None:
            return self._normalize_record(exact, key)
        needle = item.lower().strip()
        if not needle:
            return None
        candidates = sorted(storage.get_all(), key=lambda r: str(r.get("_data_key", "")))
        for record in candidates:
            name = str(record.get("item") or "").lower()
            if needle in name or name in needle:
                return self._normalize_record(record, str(record.get("_data_key") or _item_key(name)))
        return None

    def _run_check_off(self, items: List[str]) -> CommandResponse:
        storage = _get_storage()
        now = datetime.now(timezone.utc).isoformat()

        checked: List[str] = []
        missing: List[str] = []
        for item in items:
            record = self._find_record(item)
            if record is None:
                missing.append(item)
                continue
            record["checked"] = True
            record["checked_at"] = now
            key = record.pop("data_key", None) or _item_key(item)
            storage.save(key, record)
            checked.append(str(record.get("item") or item))

        if checked and missing:
            message = (
                f"Checked off {_spoken_join(checked)}. "
                f"I couldn't find {_spoken_join(missing)} on the list."
            )
        elif checked:
            message = f"Checked off {_spoken_join(checked)}."
        else:
            message = f"I couldn't find {_spoken_join(missing)} on your shopping list."

        return CommandResponse.success_response(
            context_data={"message": message, "checked": checked, "not_found": missing},
            wait_for_input=False,
        )

    def _run_remove(self, items: List[str]) -> CommandResponse:
        storage = _get_storage()

        removed: List[str] = []
        missing: List[str] = []
        for item in items:
            record = self._find_record(item)
            if record is None:
                missing.append(item)
                continue
            key = record.get("data_key") or _item_key(item)
            if storage.delete(key):
                removed.append(str(record.get("item") or item))
            else:
                missing.append(item)

        if removed and missing:
            message = (
                f"Removed {_spoken_join(removed)} from your shopping list. "
                f"I couldn't find {_spoken_join(missing)}."
            )
        elif removed:
            message = f"Removed {_spoken_join(removed)} from your shopping list."
        else:
            message = f"I couldn't find {_spoken_join(missing)} on your shopping list."

        return CommandResponse.success_response(
            context_data={"message": message, "removed": removed, "not_found": missing},
            wait_for_input=False,
        )

    # ── Clear ─────────────────────────────────────────────────────────

    def _run_clear(self, scope: str = "trip") -> CommandResponse:
        storage = _get_storage()

        if scope == "everything":
            count = storage.delete_all()
            if count == 0:
                message = "Your shopping list is already empty."
            else:
                noun = "one item" if count == 1 else f"{count} items"
                message = f"Cleared your entire shopping list — removed {noun}, including regulars."
            logger.info(f"Shopping list cleared (everything): {count} items removed")
            return CommandResponse.success_response(
                context_data={"message": message, "removed_count": count, "scope": scope},
                wait_for_input=False,
            )

        # Trip clear: one-offs are deleted, regulars reset to unchecked —
        # the next list starts pre-populated with the things you always buy.
        removed = 0
        regulars = 0
        for raw in storage.get_all():
            key = str(raw.get("_data_key") or "")
            if not key:
                continue
            record = self._normalize_record(raw, key)
            if record.get("regular"):
                regulars += 1
                if record.get("checked"):
                    record["checked"] = False
                    record["checked_at"] = None
                    record.pop("data_key", None)
                    storage.save(key, record)
            else:
                storage.delete(key)
                removed += 1

        if removed == 0 and regulars == 0:
            message = "Your shopping list is already empty."
        elif regulars:
            reg_noun = "regular is" if regulars == 1 else "regulars are"
            if removed:
                message = (
                    f"Cleared your shopping list — removed {removed} "
                    f"{'item' if removed == 1 else 'items'}. "
                    f"Your {regulars} {reg_noun} back on it."
                )
            else:
                message = f"Cleared your shopping list — your {regulars} {reg_noun} back on it."
        else:
            message = (
                f"Cleared your shopping list — removed {removed} "
                f"{'item' if removed == 1 else 'items'}."
            )

        logger.info(
            f"Shopping list cleared (trip): {removed} one-offs removed, {regulars} regulars reset"
        )
        return CommandResponse.success_response(
            context_data={
                "message": message,
                "removed_count": removed,
                "regulars_reset": regulars,
                "scope": scope,
            },
            wait_for_input=False,
        )
