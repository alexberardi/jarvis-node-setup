import random
import re
from typing import Any, Dict, List, Optional

from jarvis_command_sdk import CommandExample, IJarvisCommand
from core.command_response import CommandResponse
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging
    class JarvisLogger:  # type: ignore[no-redef]
        def __init__(self, **kw): self._log = logging.getLogger(kw.get("service", __name__))
        def info(self, msg, **kw): self._log.info(msg)
        def warning(self, msg, **kw): self._log.warning(msg)
        def error(self, msg, **kw): self._log.error(msg)
        def debug(self, msg, **kw): self._log.debug(msg)

logger = JarvisLogger(service="cmd.tell_joke")

# Style anchors — chosen because each demonstrates a *generalizable structure*
# the LLM can riff on for any topic:
#   - Q&A pun: "Why don't X verb? Because Y"
#   - "What do you call a [X with property]? [pun]"
#   - Observational one-liner with a double meaning
#   - Knock-knock template
#   - "I told … so I …" wordplay
# Tags drive topic-aware sampling (loose substring match).
_JOKE_BANK: List[Dict[str, Any]] = [
    # Q&A pun — the workhorse structure
    {"joke": "Why don't scientists trust atoms? Because they make up everything.", "tags": ["qa", "science", "general"]},
    {"joke": "Why don't skeletons fight each other? They don't have the guts.", "tags": ["qa", "general"]},
    {"joke": "Why did the scarecrow win an award? Because he was outstanding in his field.", "tags": ["qa", "general", "work"]},
    {"joke": "Why don't oysters share their pearls? Because they're shellfish.", "tags": ["qa", "animals", "ocean"]},
    {"joke": "Why did the bicycle fall over? Because it was two tired.", "tags": ["qa", "general", "wordplay"]},
    {"joke": "Why did the math book look sad? Because it had too many problems.", "tags": ["qa", "math", "school"]},
    {"joke": "Why don't programmers like nature? It has too many bugs.", "tags": ["qa", "technology", "tech", "programming", "nature"]},
    {"joke": "Why did the cookie go to the doctor? Because it was feeling crummy.", "tags": ["qa", "food"]},
    {"joke": "Why did the golfer wear two pairs of pants? In case he got a hole in one.", "tags": ["qa", "sports"]},
    {"joke": "Why are baseball games at night? Because the bats sleep during the day.", "tags": ["qa", "sports", "animals"]},

    # "What do you call a..." template — endlessly riffable
    {"joke": "What do you call a fish with no eyes? A fsh.", "tags": ["what-do-you-call", "animals", "wordplay"]},
    {"joke": "What do you call a bear with no teeth? A gummy bear.", "tags": ["what-do-you-call", "animals"]},
    {"joke": "What do you call a fake noodle? An impasta.", "tags": ["what-do-you-call", "food"]},
    {"joke": "What do you call cheese that isn't yours? Nacho cheese.", "tags": ["what-do-you-call", "food"]},
    {"joke": "What do you call a belt made of watches? A waist of time.", "tags": ["what-do-you-call", "wordplay", "general"]},
    {"joke": "What do you call a sleeping bull? A bulldozer.", "tags": ["what-do-you-call", "animals"]},

    # Observational one-liner with a twist — pure wordplay, topic-agnostic
    {"joke": "I told my wife she was drawing her eyebrows too high. She looked surprised.", "tags": ["one-liner", "general", "dad"]},
    {"joke": "I'm reading a book about anti-gravity. It's impossible to put down.", "tags": ["one-liner", "general", "science", "books"]},
    {"joke": "I used to play piano by ear. Now I use my hands.", "tags": ["one-liner", "music", "general"]},
    {"joke": "I tried to catch some fog yesterday. Mist.", "tags": ["one-liner", "weather", "general"]},
    {"joke": "I would tell you a construction joke, but I'm still working on it.", "tags": ["one-liner", "work", "general"]},
    {"joke": "Parallel lines have so much in common. It's a shame they'll never meet.", "tags": ["one-liner", "math", "general"]},
    {"joke": "Time flies like an arrow. Fruit flies like a banana.", "tags": ["one-liner", "wordplay", "general"]},
    {"joke": "I'm on a seafood diet. I see food and I eat it.", "tags": ["one-liner", "food", "general"]},

    # Knock-knock template — distinct structure the LLM needs an exemplar for
    {"joke": "Knock knock. Who's there? Lettuce. Lettuce who? Lettuce in, it's cold out here.", "tags": ["knock-knock", "knock knock"]},
    {"joke": "Knock knock. Who's there? Boo. Boo who? Don't cry, it's just a joke.", "tags": ["knock-knock", "knock knock"]},
    {"joke": "Knock knock. Who's there? Tank. Tank who? You're welcome.", "tags": ["knock-knock", "knock knock"]},

    # Dad joke / "I told the … so I …" template
    {"joke": "I don't trust stairs. They're always up to something.", "tags": ["dad", "dad jokes", "one-liner", "general"]},
    {"joke": "Did you hear about the restaurant on the moon? Great food, no atmosphere.", "tags": ["dad", "dad jokes", "food", "space"]},
    {"joke": "I'm afraid for the calendar. Its days are numbered.", "tags": ["dad", "dad jokes", "general"]},
    {"joke": "What did the ocean say to the shore? Nothing, it just waved.", "tags": ["dad", "dad jokes", "qa", "nature", "ocean"]},
]

# Strip common LLM preambles like "Sure! Here's a joke: ..."
_PREAMBLE_RE = re.compile(
    r"^(here'?s\s+(a|another|one|your)\s+(clean\s+|family[-\s]?friendly\s+|fresh\s+|new\s+)?joke[:\-\.,]?\s*|"
    r"sure[!,\.]?\s*|"
    r"okay[!,\.]?\s*|"
    r"alright[!,\.]?\s*|"
    r"how\s+about\s+this[:\-\.,]?\s*|"
    r"try\s+this\s+one[:\-\.,]?\s*)",
    re.IGNORECASE,
)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_SURROUNDING_QUOTES_RE = re.compile(r'^["\'`""'']+|["\'`""'']+$')


def _select_examples(rng: random.Random, topic: Optional[str], count: int = 2) -> List[Dict[str, Any]]:
    """Pick `count` style examples. Strategy:
      - No topic: `count` random from the full bank.
      - Topic with ≥ `count` matches: `count` random from matches.
      - Topic with 1..count-1 matches: all matches + random general jokes
        to fill, so the LLM sees both a topic anchor AND structural variety.
      - Topic with 0 matches: fall back to `count` random general jokes
        (LLM will steer toward the topic via the prompt itself).
    """
    if topic:
        needle = topic.strip().lower()
        topic_matches = [
            j for j in _JOKE_BANK
            if any(needle in t or t in needle for t in j["tags"])
        ]
    else:
        topic_matches = []

    if len(topic_matches) >= count:
        return rng.sample(topic_matches, count)

    # Either no/few topic matches — fill with random non-overlapping general jokes.
    selected = list(topic_matches)
    remaining = [j for j in _JOKE_BANK if j not in selected]
    need = count - len(selected)
    if need > 0 and remaining:
        selected.extend(rng.sample(remaining, min(need, len(remaining))))
    return selected


def _build_riff_prompt(topic: Optional[str], examples: List[Dict[str, Any]]) -> str:
    """Build a chat_text prompt that asks for ONE fresh joke styled after the examples."""
    topic_clause = f"about {topic}" if topic else "on any clever subject"
    numbered = "\n".join(f"{i + 1}. {ex['joke']}" for i, ex in enumerate(examples))
    return (
        "/no_think\n"
        f"Generate ONE original, clean, family-friendly joke {topic_clause}.\n\n"
        "Style references — match their structure (Q&A, one-liner pun, knock-knock, etc.), "
        "length, and tone. DO NOT copy or paraphrase any of these jokes:\n"
        f"{numbered}\n\n"
        "Requirements:\n"
        "- Be witty and surprising. Lean on wordplay or a clever twist.\n"
        "- Keep it short: one to three short sentences.\n"
        "- Reply with ONLY the joke text. No preface like \"Here's a joke:\", "
        "no explanation, no commentary."
    )


def _clean_llm_output(raw: str) -> str:
    """Strip think tags, preambles, and surrounding quotes."""
    text = _THINK_RE.sub("", raw).strip()
    # Strip preamble repeatedly in case the model stacks "Sure! Here's a joke:"
    for _ in range(3):
        stripped = _PREAMBLE_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    text = _SURROUNDING_QUOTES_RE.sub("", text).strip()
    return text


class TellAJokeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "tell_joke"

    @property
    def keywords(self) -> List[str]:
        return ["joke", "funny", "humor", "laugh", "comedy", "make me laugh"]

    @property
    def description(self) -> str:
        return (
            "Tell a clean, family-friendly joke with an optional topic. "
            "Riffs a fresh joke off random style examples on every invocation."
        )

    @property
    def allow_direct_answer(self) -> bool:
        # Without this, the LLM bypasses the tool and tells its own joke —
        # which at low temperatures converges to the same one each time.
        return False

    @property
    def critical_rules(self) -> List[str]:
        return [
            "After calling tell_joke, speak the returned `message` verbatim. "
            "Do not invent a new joke, do not paraphrase, do not add commentary "
            "before or after — just deliver the joke text exactly as returned.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Tell me a joke",
                expected_parameters={},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Tell me a joke about animals",
                expected_parameters={"topic": "animals"},
            ),
            CommandExample(
                voice_command="Make me laugh with a joke about technology",
                expected_parameters={"topic": "technology"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [
            CommandExample(voice_command="Tell me a joke", expected_parameters={}, is_primary=True),
            CommandExample(voice_command="Make me laugh", expected_parameters={}, is_primary=False),
            CommandExample(voice_command="Say something funny", expected_parameters={}, is_primary=False),
            CommandExample(voice_command="Tell me a joke about cats", expected_parameters={"topic": "cats"}, is_primary=False),
            CommandExample(voice_command="Tell me a joke about programming", expected_parameters={"topic": "programming"}, is_primary=False),
            CommandExample(voice_command="Tell me a joke about sports", expected_parameters={"topic": "sports"}, is_primary=False),
            CommandExample(voice_command="Make me laugh with a joke about technology", expected_parameters={"topic": "technology"}, is_primary=False),
            CommandExample(voice_command="Got any jokes about animals?", expected_parameters={"topic": "animals"}, is_primary=False),
            CommandExample(voice_command="Tell me a dad joke", expected_parameters={"topic": "dad jokes"}, is_primary=False),
            CommandExample(voice_command="Give me a knock knock joke", expected_parameters={"topic": "knock knock"}, is_primary=False),
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "topic",
                "string",
                required=False,
                default=None,
                description="Optional topic; omit for a random joke.",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def _generate_via_llm(self, prompt: str) -> Optional[str]:
        """Ask CC's chat_text for a riffed joke. Returns None on any failure."""
        # Lazy imports — keep the command importable in lightweight test
        # environments where service-discovery deps aren't wired up.
        try:
            from clients.jarvis_command_center_client import JarvisCommandCenterClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.warning("tell_joke LLM path unavailable, falling back", error=str(e))
            return None

        try:
            cc_url = get_command_center_url()
            client = JarvisCommandCenterClient(cc_url)
            raw = client.chat_text(prompt)
        except Exception as e:
            logger.warning("tell_joke chat_text call failed", error=str(e))
            return None

        if not raw or not isinstance(raw, str):
            return None

        cleaned = _clean_llm_output(raw)
        # Reject suspiciously short or empty output.
        if len(cleaned) < 12:
            logger.warning("tell_joke LLM output too short", length=len(cleaned), raw_len=len(raw))
            return None
        return cleaned

    def run(self, request_info, **kwargs) -> CommandResponse:
        topic = kwargs.get("topic")
        rng = random.Random()

        examples = _select_examples(rng, topic, count=2)
        prompt = _build_riff_prompt(topic, examples)
        composed = self._generate_via_llm(prompt)

        used_fallback = False
        if not composed:
            # Fallback: pick an example we didn't show as a style reference
            # (or any if all candidates were sampled).
            shown = {ex["joke"] for ex in examples}
            unused = [j for j in _JOKE_BANK if j["joke"] not in shown]
            composed = rng.choice(unused if unused else _JOKE_BANK)["joke"]
            used_fallback = True

        logger.info(
            "tell_joke composed",
            topic=topic or "random",
            used_fallback=used_fallback,
            chars=len(composed),
        )

        return CommandResponse.success_response(
            context_data={
                # Canonical key — `message` is what the spoken response is built from.
                # `critical_rules` instructs the LLM to relay it verbatim.
                "message": composed,
                "joke": composed,
                "topic": topic if topic else "random",
                "style_examples": [ex["joke"] for ex in examples],
                "fallback_used": used_fallback,
            },
            wait_for_input=False,
        )
