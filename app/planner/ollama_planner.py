from json import JSONDecodeError, dumps, load
from os import getenv
from re import compile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from opentelemetry.trace import SpanKind
from pydantic import ValidationError

from app.engine.workflow_engine import execution_order
from app.observability.tracing import trace_span
from app.validation.plan import PlanModel, SupportAction, TicketLimit, WorkflowPlan, build_support_plan
from app.validation.policy import ToolPolicyViolation


class PlannerError(RuntimeError):
    pass


class PlannerRejected(ValueError):
    pass


class PlannerIntent(PlanModel):
    action: Literal[
        "list_tickets", "summarize_tickets", "create_followup_tasks",
        "unsupported", "needs_clarification",
    ]
    limit: TicketLimit | None


_INSTRUCTIONS = """Classify the whole request for Axon's support-ticket demo.

Available actions:
- list_tickets: show the highest-priority support tickets.
- summarize_tickets: summarize the highest-priority support tickets.
- create_followup_tasks: create local follow-up tasks for those tickets, only
  when the request explicitly asks to create follow-up tasks.
- unsupported: any requested operation outside these actions, even if another
  part of the request is supported. Sending email, closing or changing tickets,
  and deleting records are unsupported.
- needs_clarification: the user refers to missing context or gives no clear action.

Set limit to a count only when the request explicitly specifies it (including
number words or a singular ticket). If no count is specified, use null.
"Highest-priority tickets" alone gives no count. Counts outside 1 to 10 are
unsupported. For unsupported or needs_clarification, use limit=null.

Examples:
"Show four urgent tickets" -> {"action":"list_tickets","limit":4}
"Open follow-up tasks for the most urgent issues" -> {"action":"create_followup_tasks","limit":null}
"Show a ticket and resolve it" -> {"action":"unsupported","limit":null}
"Notify the requester by email" -> {"action":"unsupported","limit":null}
"Repeat my earlier request" -> {"action":"needs_clarification","limit":null}
"""


_COUNT_WORDS = {
    "zero": 0, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5,
    "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10,
}
_COUNT_WORD_PATTERN = "|".join(_COUNT_WORDS)
_TICKET_COUNT = compile(
    r"(?<![\w.+-])(?:(?P<approx>at least|at most|more than|less than|"
    r"fewer than|up to|around|about)\s+)?"
    rf"(?:top\s+)?(?P<count>-?\d+|{_COUNT_WORD_PATTERN})\s+"
    r"(?:(?:highest[-\s]+priority|highest[-\s]+ranked|most\s+urgent|urgent|support)\s+)*"
    r"(?:tickets?|destek\s+(?:kayd\w*|kayıt\w*))\b"
)


def _explicit_ticket_limit(user_request: str) -> int | None:
    normalized = user_request.casefold().replace("\u0307", "")
    counts = set()
    for match in _TICKET_COUNT.finditer(normalized):
        if match["approx"]:
            continue
        count = match["count"]
        counts.add(_COUNT_WORDS[count] if count in _COUNT_WORDS else int(count))

    if len(counts) > 1:
        raise PlannerRejected("needs_clarification")

    limit = next(iter(counts), None)
    if limit is not None and not 1 <= limit <= 10:
        raise PlannerRejected("unsupported")
    return limit


def generate_plan(user_request: str) -> WorkflowPlan:
    explicit_limit = _explicit_ticket_limit(user_request)
    payload = {
        "model": getenv("OLLAMA_MODEL", "qwen3:4b"),
        "messages": [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": user_request},
        ],
        "format": PlannerIntent.model_json_schema(),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 128},
    }
    base_url = getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    request = Request(
        f"{base_url}/api/chat",
        data=dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with (
            trace_span("ollama.chat", kind=SpanKind.CLIENT),
            urlopen(request, timeout=180) as response,
        ):
            body = load(response)
    except HTTPError as error:
        raise PlannerError(f"Ollama returned HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise PlannerError("Could not reach the local Ollama service") from error
    except JSONDecodeError as error:
        raise PlannerError("Ollama returned an invalid HTTP response") from error

    if not isinstance(body, dict) or body.get("done") is not True or body.get("done_reason") == "length":
        raise PlannerError("Ollama did not return a complete intent response")

    try:
        intent = PlannerIntent.model_validate_json(body["message"]["content"])
    except (KeyError, TypeError, ValidationError) as error:
        raise PlannerError("Ollama returned an invalid intent response") from error

    if intent.action in ("unsupported", "needs_clarification"):
        raise PlannerRejected(intent.action)

    limit = explicit_limit if explicit_limit is not None else (intent.limit or 3)
    plan = build_support_plan(SupportAction(intent.action), limit=limit)
    try:
        ordered_steps = execution_order(plan.steps)
    except ToolPolicyViolation as error:
        raise PlannerRejected("tool_denied") from error
    except ValueError as error:
        raise PlannerError("Plan failed dependency validation") from error

    return WorkflowPlan(steps=tuple(ordered_steps))
