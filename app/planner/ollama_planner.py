from json import JSONDecodeError, dumps, load
from os import getenv
from re import compile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from opentelemetry.trace import SpanKind
from pydantic import ValidationError

from app.engine.workflow_engine import execution_order
from app.observability.telemetry import log_event
from app.observability.tracing import trace_span
from app.validation.plan import PlanModel, SupportAction, TicketLimit, WorkflowPlan, build_support_plan
from app.validation.policy import ToolPolicyViolation


class PlannerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "planner_error") -> None:
        super().__init__(message)
        self.code = code


class PlannerRejected(ValueError):
    pass


class PlannerIntent(PlanModel):
    action: Literal[
        "list_tickets", "summarize_tickets", "create_followup_tasks",
        "unsupported", "needs_clarification",
    ]
    limit: TicketLimit | None


_INSTRUCTIONS = """Classify the whole request for Axon's support-ticket demo.

Supported operations can be combined. Choose the last operation needed:
- list_tickets: identify or display the highest-priority support tickets and
  their original details. Asking which ticket has highest priority is listing.
- summarize_tickets: provide a summary, recap or digest of those tickets.
- create_followup_tasks: create local follow-up tasks for those tickets.
  Listing, summarizing and creating follow-up tasks together is supported.
  Only choose this action when the user asks to create tasks.

Choose unsupported if any requested operation is outside these actions.
Sending email, closing or deleting records, and changing ticket fields or
priority are unsupported. Ranking tickets by priority is supported.
For Turkish, "önceliğini değiştir", "önceliğini düşür" and "önceliğini yükselt"
ask to change priority. Reject the whole request even when it also asks for
follow-up tasks. An operation explicitly excluded by the user is not requested.

Choose needs_clarification for missing context or no clear action.

Counts:
- Use the number of tickets explicitly requested, including number words.
- A singular ticket implies limit=1.
- Plural tickets with no count imply limit=null. "Destek kayıtlarını" is plural.
- Numbers of sentences, times and dates are not ticket counts.
- Counts outside 1 to 10 are unsupported.
- For unsupported or needs_clarification, use limit=null.

Examples:
"Display the most urgent ticket's details" -> {"action":"list_tickets","limit":1}
"Summarize four urgent tickets" -> {"action":"summarize_tickets","limit":4}
"Find three urgent tickets, summarize them and create local follow-up tasks" -> {"action":"create_followup_tasks","limit":3}
"En yüksek öncelikli destek kayıtlarını göster" -> {"action":"list_tickets","limit":null}
"Bir destek kaydını göster ve önceliğini değiştir" -> {"action":"unsupported","limit":null}
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
_SINGULAR_TICKET = compile(
    r"\b(?:a|an|the|which)\s+"
    r"(?:(?:highest[-\s]+priority|highest[-\s]+ranked|most\s+urgent|urgent|support)\s+)*"
    r"ticket\b(?!\s+(?:ids?|numbers?)\b)"
)
_PRIORITY_CHANGE = compile(
    r"\b(?:önceliği(?:ni)?|önceliklerini)\s+"
    r"(?:düşür(?:ün(?:üz)?)?|(?:yükselt|değiştir)(?:in(?:iz)?)?)\b"
    r"|\b(?:(?P<negated>do\s+not|don['’]t|never|not)\s+)?"
    r"(?:change|update|lower|raise)\s+(?:(?:the|their|its)\s+)?"
    r"(?:(?:support\s+)?tickets?'?\s+)?priorit(?:y|ies)\b"
)


def _explicit_ticket_limit(user_request: str) -> int | None:
    normalized = user_request.casefold().replace("\u0307", "")
    counts = set()
    for match in _TICKET_COUNT.finditer(normalized):
        if match["approx"]:
            continue
        count = match["count"]
        counts.add(_COUNT_WORDS[count] if count in _COUNT_WORDS else int(count))

    if not counts and _SINGULAR_TICKET.search(normalized):
        counts.add(1)

    if len(counts) > 1:
        raise PlannerRejected("needs_clarification")

    limit = next(iter(counts), None)
    if limit is not None and not 1 <= limit <= 10:
        raise PlannerRejected("unsupported")
    return limit


def generate_plan(user_request: str) -> WorkflowPlan:
    normalized = user_request.casefold().replace("\u0307", "")
    for match in _PRIORITY_CHANGE.finditer(normalized):
        if match["negated"] is None:
            raise PlannerRejected("unsupported")

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
        raise PlannerError(
            f"Ollama returned HTTP {error.code}", code="ollama_http_error"
        ) from error
    except (URLError, TimeoutError) as error:
        timed_out = isinstance(error, TimeoutError) or (
            isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
        )
        code = "ollama_timeout" if timed_out else "ollama_connection_error"
        raise PlannerError("Could not reach the local Ollama service", code=code) from error
    except JSONDecodeError as error:
        raise PlannerError("Ollama returned an invalid HTTP response", code="ollama_invalid_response") from error

    if not isinstance(body, dict) or body.get("done") is not True or body.get("done_reason") == "length":
        raise PlannerError("Ollama did not return a complete intent response", code="ollama_invalid_response")

    log_event(
        "ollama.response",
        model=payload["model"],
        load_duration_ns=body.get("load_duration"),
        prompt_eval_duration_ns=body.get("prompt_eval_duration"),
        eval_duration_ns=body.get("eval_duration"),
    )

    try:
        intent = PlannerIntent.model_validate_json(body["message"]["content"])
    except (KeyError, TypeError, ValidationError) as error:
        raise PlannerError("Ollama returned an invalid intent response", code="ollama_invalid_response") from error

    if intent.action in ("unsupported", "needs_clarification"):
        raise PlannerRejected(intent.action)

    limit = explicit_limit if explicit_limit is not None else (intent.limit or 3)
    plan = build_support_plan(SupportAction(intent.action), limit=limit)
    try:
        ordered_steps = execution_order(plan.steps)
    except ToolPolicyViolation as error:
        raise PlannerRejected("tool_denied") from error
    except ValueError as error:
        raise PlannerError("Plan failed dependency validation", code="invalid_plan_dependencies") from error

    return WorkflowPlan(steps=tuple(ordered_steps))
