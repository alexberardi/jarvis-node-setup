"""act_on_items — the generic follow-up tool for "act on what I was just shown".

This command exists almost entirely to contribute a tool *schema* to the
conversation's ``client_tools`` (so the LLM can call it). Its actual execution
is intercepted by :class:`CommandExecutionService` in ``_execute_tools`` —
which is the only place that holds the per-conversation ``ref_id -> owner``
map and can dispatch to the owning command's ``@callback`` handlers. ``run()``
here is only a graceful fallback for the (unexpected) case where dispatch is
not intercepted.

The flow:

1. A list-surfacing command/agent returns ``CommandResponse.with_items(...)``.
2. The node remembers ``ref_id -> (owner, attrs, actions)`` and the
   command-center re-injects a numbered "RECENTLY SHOWN" block into the prompt.
3. On a follow-up ("mark those as read", "send me #3"), the LLM resolves the
   reference to ``ref_id``\\(s) and calls ``act_on_items(action, ref_ids)``.
4. CC has no server tool by this name, so it forwards the call to the node as
   a normal client tool call; ``_execute_tools`` dispatches each ``ref_id`` to
   its owning command's ``@callback`` named ``action``.

See ``CommandExecutionService._dispatch_act_on_items`` for the dispatch.
"""

from typing import Any, List

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    IJarvisCommand,
    IJarvisParameter,
    IJarvisSecret,
    JarvisParameter,
    RequestInformation,
)


class ActOnItemsCommand(IJarvisCommand):
    """Generic "act on the items you were just shown" tool (dispatch is intercepted)."""

    @property
    def command_name(self) -> str:
        return "act_on_items"

    @property
    def description(self) -> str:
        return (
            "Act on one or more items the user was just shown in a 'RECENTLY SHOWN' "
            "list — for example mark emails read, draft a reply, archive, or send an "
            "article. Use this ONLY when the user refers back to items they were just "
            "shown ('those', 'them', 'all of them', 'the first one', 'number 3', "
            "'the one from ABC'). Set 'action' to the verb (e.g. 'mark_read') and "
            "'ref_ids' to the ids of the referenced items, taken verbatim from the "
            "bracketed [id] next to each item in the RECENTLY SHOWN block. Never "
            "invent ids — only use ids that appear in that list, and only actions "
            "that are listed as valid for those items."
        )

    @property
    def keywords(self) -> List[str]:
        # Intentionally empty: this tool is selected by the LLM from the
        # RECENTLY SHOWN context, never fuzzy-matched or pre-routed.
        return []

    @property
    def allow_direct_answer(self) -> bool:
        return False

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                name="action",
                param_type="string",
                required=True,
                description=(
                    "The action verb to perform, e.g. 'mark_read', 'draft_reply', "
                    "'send_full_article'. Must be one of the actions listed as valid "
                    "for the referenced items in the RECENTLY SHOWN block."
                ),
            ),
            JarvisParameter(
                name="ref_ids",
                param_type="array<string>",
                required=True,
                description=(
                    "The ids of the items to act on, taken verbatim from the bracketed "
                    "[id] next to each item in the RECENTLY SHOWN list. For 'those' / "
                    "'all of them', include every id in the list."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="mark those as read",
                expected_parameters={"action": "mark_read", "ref_ids": ["eml_a1b2", "eml_c3d4"]},
                is_primary=True,
            ),
            CommandExample(
                voice_command="send me the full article for 3",
                expected_parameters={"action": "send_full_article", "ref_ids": ["art_c9"]},
            ),
            CommandExample(
                voice_command="draft a reply to the one from ABC",
                expected_parameters={"action": "draft_reply", "ref_ids": ["eml_a1b2"]},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="mark them all as read",
                expected_parameters={"action": "mark_read", "ref_ids": ["eml_a1b2", "eml_c3d4", "eml_e5f6"]},
            ),
            CommandExample(
                voice_command="mark the first one read",
                expected_parameters={"action": "mark_read", "ref_ids": ["eml_a1b2"]},
            ),
            CommandExample(
                voice_command="archive number two",
                expected_parameters={"action": "archive", "ref_ids": ["eml_c3d4"]},
            ),
            CommandExample(
                voice_command="send me article one",
                expected_parameters={"action": "send_full_article", "ref_ids": ["art_a1"]},
            ),
        ]

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        # Dispatch is normally intercepted in CommandExecutionService._execute_tools,
        # which owns the per-conversation ref_id->owner map. Reaching run() means
        # there was nothing to act on (or an older runtime) — fail soft.
        return CommandResponse.final_response(
            context_data={"message": "I don't have anything shown to act on right now."}
        )
