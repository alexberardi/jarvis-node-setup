"""Export shopping list command for Jarvis.

Sends the household shopping list to the speaker's phone as a rich inbox
item with a push notification. The mobile app renders the list with
checkboxes; the user picks items and taps "export", which fires the
``export_selected`` callback back on this command. The callback builds a
provider-specific result — a tappable Walmart add-to-cart deep link, or a
copyable plain-text list for the "notes" provider. Export is the trip
boundary: exported one-offs are removed from the list and exported
regulars are reset to unchecked, so the next list starts pre-populated
with the household staples. Deselected items are never touched.

Two storages are involved:
- ``shopping_list`` — the live list (owned by ShoppingListCommand; this
  command reads it and applies the trip-boundary reset after export).
- ``walmart_items`` — the staples map ``{item, walmart_item_id}`` keyed by
  ``_item_key(item)``. Unmapped items are pre-seeded here on export (only
  for the walmart provider) so the user can fill in the IDs via the mobile
  data browser (PATCH only — the browser has no create operation).

Records are household-shared: no ``user_id`` field, every household member
sees the same map.
"""

import re
from typing import Any, Callable, Dict, List

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
    callback,
)
from commands.shopping_list_command import _item_key
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation

logger = JarvisLogger(service="jarvis-node")

SHOPPING_STORAGE_NAME = "shopping_list"
MAP_STORAGE_NAME = "walmart_items"

# Contract constants shared with command-center + mobile. Changing any of
# these breaks the mobile inbox renderer — see the PRD shared contract.
INBOX_CATEGORY = "shopping_list_export"
CALLBACK_NAME = "export_selected"

DEFAULT_PROVIDER = "walmart"

_shopping_storage: JarvisStorage | None = None
_map_storage: JarvisStorage | None = None


def _get_shopping_storage() -> JarvisStorage:
    global _shopping_storage
    if _shopping_storage is None:
        _shopping_storage = JarvisStorage(SHOPPING_STORAGE_NAME)
    return _shopping_storage


def _get_map_storage() -> JarvisStorage:
    global _map_storage
    if _map_storage is None:
        _map_storage = JarvisStorage(MAP_STORAGE_NAME)
    return _map_storage


# ── Provider abstraction (intentionally minimal) ──────────────────────────
# A future provider (e.g. instacart) is one result-builder function plus one
# entry here plus one enum value on EXPORT_PROVIDER. No classes.
#
# Builders take resolved entries [{item, walmart_item_id, quantity}] and
# return the provider-specific part of the callback result context_data:
# walmart sets "url", notes sets "text" — exactly one of the two per the
# shared contract.


def _build_walmart_result(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Walmart affiliate add-to-cart deep link from mapped entries.

    Entries with a falsy ``walmart_item_id`` are skipped — never put an
    empty ID in the cart URL.
    """
    items_param = ",".join(
        f"{entry['walmart_item_id']}|{entry['quantity']}"
        for entry in entries
        if entry.get("walmart_item_id")
    )
    return {"url": f"https://affil.walmart.com/cart/addToCart?items={items_param}"}


def _build_notes_result(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Copyable plain-text list — every entry exports, no IDs needed."""
    lines = ["Shopping list:"]
    for entry in entries:
        quantity = entry.get("quantity") or 1
        suffix = f" x{quantity}" if quantity > 1 else ""
        lines.append(f"- {entry['item']}{suffix}")
    return {"text": "\n".join(lines)}


class ProviderError(Exception):
    """A provider builder could not produce a result.

    str(exc) is the message TTS/mobile should surface. Raised BEFORE the
    trip-boundary reset is applied, so a failed export never mutates the
    shopping list.
    """


PROVIDERS: Dict[str, Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = {
    "walmart": _build_walmart_result,
    "notes": _build_notes_result,
}


def _clamp_quantity(value: Any) -> int:
    """Coerce a callback-supplied quantity to a sane 1-99 range."""
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(quantity, 99))


def _get_provider_name() -> str:
    """Resolve the configured export provider, defaulting to walmart."""
    provider = _get_map_storage().get_secret("EXPORT_PROVIDER", scope="integration")
    provider = (provider or "").strip().lower()
    if provider in PROVIDERS:
        return provider
    return DEFAULT_PROVIDER


# ── Pre-route pattern ──────────────────────────────────────────────────────
# Anchored both ends so "send a text to mom" / "export my contacts" can
# never be stolen. The optional destination tail only matches known
# destinations (walmart/the store → walmart, notes/my notes → notes).

_EXPORT_RE = re.compile(
    r"^\s*(?:send|export)\s+(?:my\s+|the\s+|our\s+)?"
    r"(?:shopping|grocery|groceries)\s+list"
    r"(?:\s+to\s+(?P<dest>walmart|the\s+store|notes|my\s+notes))?\s*[?.!]*$",
    re.IGNORECASE,
)

# Destination phrase → provider name for the fast-path handler.
_DEST_TO_PROVIDER = {
    "walmart": "walmart",
    "the store": "walmart",
    "notes": "notes",
    "my notes": "notes",
}


def _provider_from_utterance(voice_command: str) -> str | None:
    """Deterministic spoken-destination detection for the LLM-routed path.

    The export destination is configuration (EXPORT_PROVIDER secret); the
    only thing that overrides it is the user naming a destination out loud.
    """
    said = (voice_command or "").lower()
    if "note" in said:
        return "notes"
    if "walmart" in said or "store" in said:
        return "walmart"
    return None


class ExportShoppingListCommand(IJarvisCommand):
    """Send the shopping list to the configured store as a tappable cart link."""

    @property
    def command_name(self) -> str:
        return "export_shopping_list"

    @property
    def data_browser_storage_name(self) -> str:
        # The browsable records are the staples map, not the live list
        # (the list itself is browsed under shopping_list).
        return MAP_STORAGE_NAME

    @property
    def description(self) -> str:
        return (
            "Send the shopping list to the configured store (e.g. Walmart) as "
            "a tappable cart link on your phone. Use for 'send my shopping "
            "list to walmart', 'export the shopping list'."
        )

    @property
    def keywords(self) -> List[str]:
        return ["export", "send", "walmart", "cart", "shopping list"]

    @property
    def parameters(self) -> List[JarvisParameter]:
        # Deliberately empty. The export destination is configuration
        # (EXPORT_PROVIDER secret) with a spoken override — never an
        # LLM-extracted value. The override is derived deterministically
        # from the utterance in post_process_tool_call; exposing it as an
        # enum parameter taught the LLM to fill it spuriously, silently
        # overriding the user's configured store.
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "EXPORT_PROVIDER",
                "Where 'send my shopping list' exports to",
                "integration",
                "string",
                required=False,
                is_sensitive=False,
                friendly_name="Export store",
                enum_values=sorted(PROVIDERS.keys()),
            ),
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="shopping_list",
                description=(
                    "Adding/reading/checking off items is the shopping_list "
                    "command — this one only sends the list to the store."
                ),
            ),
        ]

    # ── Mobile command-data browser (the walmart_items staples map) ──────

    def editable_fields(self) -> List[FieldSpec]:
        return [
            # Read-only: renaming would desync the display name from the
            # storage key the export resolves against.
            FieldSpec("item", "string", label="Item", editable=False, required=True),
            FieldSpec(
                "walmart_item_id",
                "string",
                label="Walmart item ID",
                placeholder="e.g. 10450114",
            ),
        ]

    def display_summary(self, record: dict) -> RecordSummary:
        item = str(record.get("item") or "Item")
        walmart_id = str(record.get("walmart_item_id") or "").strip()
        subtitle = f"Walmart ID: {walmart_id}" if walmart_id else "Needs Walmart ID"
        return RecordSummary(title=item, subtitle=subtitle, icon="cart-arrow-right")

    # ── Examples ──────────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample("Send my shopping list to Walmart", {}, is_primary=True),
            CommandExample("Export the shopping list", {}),
            CommandExample("Send the grocery list to the store", {}),
            CommandExample("Send my shopping list to my notes", {}),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        phrases: List[tuple[str, Dict[str, Any], bool]] = [
            ("Send my shopping list to Walmart", {}, True),
            ("Export the shopping list", {}, False),
            ("Send the shopping list to the store", {}, False),
            ("Export my grocery list", {}, False),
            ("Send our grocery list to Walmart", {}, False),
            ("Send the groceries list", {}, False),
            ("Export our shopping list to Walmart", {}, False),
            ("Send my grocery list to the store", {}, False),
            ("Export the grocery list to Walmart", {}, False),
            ("Send the shopping list", {}, False),
            ("Send my shopping list to my notes", {}, False),
            ("Export the grocery list to notes", {}, False),
        ]
        return [
            CommandExample(voice, args, is_primary)
            for voice, args, is_primary in phrases
        ]

    # ── Pre-route ─────────────────────────────────────────────────────────

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="export_shopping_list.export",
                description=(
                    "Bypass LLM for 'send/export the shopping list "
                    "(to walmart/notes)'"
                ),
                example="send my shopping list to walmart",
                regex=_EXPORT_RE.pattern,
                handler="_fp_export",
            ),
        ]

    def _fp_export(self, match, voice_command: str) -> PreRouteResult | None:
        dest = re.sub(r"\s+", " ", (match.group("dest") or "").strip().lower())
        provider = _DEST_TO_PROVIDER.get(dest)
        if provider is None:
            # No destination spoken — run() falls back to the configured
            # EXPORT_PROVIDER secret.
            return PreRouteResult(arguments={})
        return PreRouteResult(arguments={"provider": provider})

    def post_process_tool_call(
        self, args: Dict[str, Any], voice_command: str
    ) -> Dict[str, Any]:
        # provider is not a declared parameter, but models sometimes invent
        # args — anything LLM-supplied is discarded unconditionally. The
        # only provider channel besides the EXPORT_PROVIDER secret is the
        # user actually saying a destination, detected here.
        args.pop("provider", None)
        spoken = _provider_from_utterance(voice_command)
        if spoken is not None:
            args["provider"] = spoken
        return args

    # ── Main execution ────────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        provider = kwargs.get("provider")
        if provider not in PROVIDERS:
            provider = _get_provider_name()

        speaker_user_id = request_info.user_id if request_info is not None else None
        if speaker_user_id is None:
            message = (
                "I'm not sure who's asking — I need to know whose phone to "
                "send the list to. Try training your voice in the app."
            )
            logger.warning("export_shopping_list refused: no speaker_user_id")
            return CommandResponse.error_response(
                error_details="Unknown speaker — cannot target the export push.",
                context_data={"error": "unknown_speaker", "message": message},
            )

        # Unchecked items only. Filter on `checked`, NOT `checked_at` —
        # re-added items keep a stale checked_at from their last trip.
        open_records = [
            r for r in _get_shopping_storage().get_all()
            if r.get("item") and not r.get("checked")
        ]
        if not open_records:
            message = "Your shopping list is empty — nothing to export."
            return CommandResponse.success_response(
                context_data={"message": message, "item_count": 0},
                wait_for_input=False,
            )

        # Notes households shouldn't accumulate walmart_items map noise —
        # only the walmart provider pre-seeds unmatched rows.
        sections = self._build_sections(
            open_records, seed_unmatched=provider == "walmart"
        )
        all_items = sections["regulars"] + sections["one_offs"]
        item_count = len(all_items)
        unmatched_count = sum(1 for i in all_items if not i["walmart_item_id"])

        title = (
            f"Shopping list ready — {item_count} "
            f"{'item' if item_count == 1 else 'items'}"
        )
        payload = {
            "title": title,
            "summary": self._build_summary(item_count, unmatched_count, provider),
            "body": self._build_body(all_items, provider),
            "category": INBOX_CATEGORY,
            "metadata": {
                "type": INBOX_CATEGORY,
                "provider": provider,
                "sections": sections,
                "item_count": item_count,
            },
            "user_id": speaker_user_id,
            "create_push_notification": True,
            "target_type": "user",
        }

        post_result = self._post_inbox_item(payload)
        if post_result != "ok":
            return self._post_failure_response(post_result, speaker_user_id)

        if provider != "walmart":
            # No Walmart-match tail — every non-walmart row is exportable.
            message = (
                f"I sent your shopping list to your phone — {item_count} "
                f"{'item' if item_count == 1 else 'items'}."
            )
        else:
            message = (
                f"I sent your shopping list to your phone — {item_count} "
                f"{'item' if item_count == 1 else 'items'} ready"
            )
            if unmatched_count:
                message += (
                    f", {unmatched_count} still "
                    f"{'needs' if unmatched_count == 1 else 'need'} a Walmart match."
                )
            else:
                message += "."

        logger.info(
            "export_shopping_list sent",
            user_id=speaker_user_id,
            provider=provider,
            item_count=item_count,
            unmatched=unmatched_count,
        )
        return CommandResponse.success_response(
            context_data={
                "message": message,
                "item_count": item_count,
                "unmatched_count": unmatched_count,
            },
            wait_for_input=False,
        )

    # ── Payload helpers ───────────────────────────────────────────────────

    def _build_sections(
        self, open_records: List[Dict[str, Any]], *, seed_unmatched: bool
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Resolve open list records against the staples map.

        When ``seed_unmatched`` is True (walmart provider), unmatched items
        are pre-seeded into the map with an empty walmart_item_id so the
        user has rows to fill in via the mobile data browser (PATCH only —
        there is no create op in the browser). Notes exports don't need
        IDs, so they never seed.
        """
        map_storage = _get_map_storage()
        sections: Dict[str, List[Dict[str, Any]]] = {"regulars": [], "one_offs": []}

        for record in open_records:
            item = str(record.get("item"))
            key = str(record.get("_data_key") or _item_key(item))
            map_record = map_storage.get(key)
            if map_record is None:
                if seed_unmatched:
                    map_storage.save(key, {"item": item, "walmart_item_id": ""})
                walmart_item_id = None
            else:
                walmart_item_id = (
                    str(map_record.get("walmart_item_id") or "").strip() or None
                )
            entry = {
                "key": key,
                "item": item,
                "walmart_item_id": walmart_item_id,
                # Standing default from the list record; the mobile stepper
                # overrides it per-trip without writing back.
                "quantity": _clamp_quantity(record.get("quantity") or 1),
            }
            section = "regulars" if record.get("regular") else "one_offs"
            sections[section].append(entry)

        return sections

    @staticmethod
    def _build_summary(item_count: int, unmatched_count: int, provider: str) -> str:
        if provider == "notes":
            return (
                f"{item_count} {'item' if item_count == 1 else 'items'} "
                "ready to copy to your notes."
            )
        summary = (
            f"{item_count} {'item' if item_count == 1 else 'items'} "
            "ready to send to your cart."
        )
        if unmatched_count:
            summary += (
                f" {unmatched_count} still "
                f"{'needs' if unmatched_count == 1 else 'need'} a Walmart ID."
            )
        return summary

    @staticmethod
    def _build_body(items: List[Dict[str, Any]], provider: str) -> str:
        """Markdown bulleted fallback list for clients without the rich renderer."""
        lines = []
        for entry in items:
            quantity = entry.get("quantity") or 1
            qty_str = f" × {quantity}" if quantity > 1 else ""
            needs_id = provider == "walmart" and not entry["walmart_item_id"]
            suffix = " _(needs Walmart ID)_" if needs_id else ""
            lines.append(f"- {entry['item']}{qty_str}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _post_inbox_item(payload: Dict[str, Any]) -> str:
        """Return ``"ok"`` on success, or a short failure reason string.

        Discriminated tags (not bool) so run() can map each failure mode to
        a distinct voice response — same lesson as send_link.
        """
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.error("export_shopping_list import failed", error=str(e))
            return "import_error"

        cc_url = get_command_center_url()
        if not cc_url:
            logger.error("export_shopping_list: get_command_center_url returned empty")
            return "no_cc_url"

        result = RestClient.post(
            f"{cc_url}/api/v0/node/inbox-item",
            data=payload,
            timeout=5,
        )
        if result is None:
            return "http_error"
        return "ok"

    def _post_failure_response(self, post_result: str, user_id: int) -> CommandResponse:
        if post_result == "no_cc_url":
            message = "I can't find the server to send your shopping list."
            details = "service_discovery returned no command-center URL"
        elif post_result == "import_error":
            message = "I can't export the list — the send module isn't loaded."
            details = (
                "ImportError in _post_inbox_item — clients.rest_client or "
                "utils.service_discovery missing"
            )
        else:
            message = "I couldn't reach the server right now."
            details = "RestClient.post returned None (HTTP error, timeout, or unreachable)"

        logger.error(
            "export_shopping_list failed", reason=post_result, user_id=user_id
        )
        return CommandResponse.error_response(
            error_details=details,
            context_data={"message": message},
        )

    # ── Callback: mobile picked items, build the provider result ─────────

    @callback(CALLBACK_NAME)
    def export_selected(
        self, data: dict, request_info: RequestInformation
    ) -> CommandResponse:
        # {selected: [{key, quantity}], provider} — quantity comes from the
        # mobile stepper (seeded from the record default, overridable per
        # trip); provider echoes metadata.provider so the result matches
        # what the export screen rendered.
        provider = str(data.get("provider") or "").strip().lower()
        if provider not in PROVIDERS:
            provider = _get_provider_name()

        selected: List[tuple[str, int]] = []
        for entry in data.get("selected") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            if key:
                selected.append((key, _clamp_quantity(entry.get("quantity"))))
        if not selected:
            message = "Nothing was selected to export."
            return CommandResponse.error_response(
                error_details="export_selected called with no valid selected entries",
                context_data={"message": message},
            )

        shopping_storage = _get_shopping_storage()
        map_storage = _get_map_storage()

        entries: List[Dict[str, Any]] = []
        for key, quantity in selected:
            shopping_record = shopping_storage.get(key)
            map_record = map_storage.get(key)
            entries.append({
                "key": key,
                "item": str(
                    (shopping_record or {}).get("item")
                    or (map_record or {}).get("item")
                    or key
                ),
                "walmart_item_id": (
                    str((map_record or {}).get("walmart_item_id") or "").strip()
                    or None
                ),
                "quantity": quantity,
            })

        if provider == "walmart":
            # Unmapped entries are skipped — never put an empty ID in the
            # cart URL — and stay unchecked so they survive on the list.
            exportable = [e for e in entries if e["walmart_item_id"]]
            if not exportable:
                message = "None of the selected items have a Walmart ID yet."
                return CommandResponse.error_response(
                    error_details="export_selected: zero resolvable walmart_item_ids",
                    context_data={"message": message},
                )
        else:
            # Notes needs no IDs — every selected entry is exportable.
            exportable = entries

        # Build the provider result FIRST: if it fails (e.g. a provider
        # API is unreachable), the list must be left exactly as it was.
        try:
            result = PROVIDERS[provider](exportable)
        except ProviderError as e:
            return CommandResponse.error_response(
                error_details=f"export_selected provider '{provider}' failed: {e}",
                context_data={"message": str(e)},
            )

        # Export IS the trip boundary: exported one-offs leave the list
        # (they're in the cart), exported regulars go straight back on it
        # unchecked for next time. Deselected items are untouched — a
        # partial export must never delete what the user chose to keep.
        exported: List[str] = []
        regulars_reset = 0
        for entry in exportable:
            exported.append(entry["item"])
            shopping_record = shopping_storage.get(entry["key"])
            if shopping_record is None:
                continue
            if shopping_record.get("regular"):
                # Meta-field hygiene per the _normalize_record pattern:
                # never write repository meta fields back into the record.
                shopping_record.pop("_data_key", None)
                shopping_record.pop("_expires_at", None)
                shopping_record.pop("data_key", None)
                shopping_record["checked"] = False
                shopping_record["checked_at"] = None
                shopping_storage.save(entry["key"], shopping_record)
                regulars_reset += 1
            else:
                shopping_storage.delete(entry["key"])

        if provider == "notes":
            message = (
                f"Your list is ready to copy — {len(exported)} "
                f"{'item' if len(exported) == 1 else 'items'}."
            )
        else:
            message = (
                f"Exported {len(exported)} "
                f"{'item' if len(exported) == 1 else 'items'} to your Walmart cart."
            )
        if regulars_reset:
            message += (
                f" Your {regulars_reset} "
                f"{'regular is' if regulars_reset == 1 else 'regulars are'} "
                "back on the list."
            )
        logger.info(
            "export_shopping_list export_selected",
            provider=provider,
            exported_count=len(exported),
            selected_count=len(selected),
        )
        return CommandResponse.success_response(
            context_data={**result, "exported": exported, "message": message},
            wait_for_input=False,
        )
