from json import JSONDecodeError, dumps, load
from os import getenv
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from app.engine.workflow_engine import execution_order
from app.validation.plan import PlanModel, WorkflowPlan


class PlannerError(RuntimeError):
    pass


class PlannerRejected(ValueError):
    pass


class PlannerResult(PlanModel):
    plan: WorkflowPlan | None
    reason: Literal["unsupported", "needs_clarification"] | None


_INSTRUCTIONS = """Generate a workflow plan for Axon's support-ticket demo.

Available tools:
- list_tickets: lists the highest-priority tickets. Set arguments.limit to
  the requested count (1 to 10), or 3 if no count is given. No dependencies.
- summarize_tickets: summarizes the selected tickets. Arguments: {}.
  Depends on the list_tickets step.
- create_followup_tasks: creates one local task per summarized ticket.
  Arguments: {}. Depends on the summarize_tickets step.

Use each tool at most once. Give every step a unique lowercase name and use
those names in depends_on. Include requested actions and their prerequisites.
For a valid plan, set reason to null. For unsupported actions, set plan to
null and reason to "unsupported". If clarification is needed, set plan to
null and reason to "needs_clarification".

Example for "List the two highest-priority tickets":
{"plan":{"steps":[{"name":"find_tickets","tool":"list_tickets",
"depends_on":[],"arguments":{"limit":2}}]},"reason":null}
"""


def generate_plan(user_request: str) -> WorkflowPlan:
    payload = {
        "model": getenv("OLLAMA_MODEL", "qwen3:4b"),
        "messages": [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": user_request},
        ],
        "format": PlannerResult.model_json_schema(),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 384},
    }
    request = Request(
        "http://127.0.0.1:11434/api/chat",
        data=dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=180) as response:
            body = load(response)
    except HTTPError as error:
        raise PlannerError(f"Ollama returned HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise PlannerError("Could not reach the local Ollama service") from error
    except JSONDecodeError as error:
        raise PlannerError("Ollama returned an invalid HTTP response") from error

    if body.get("done") is not True or body.get("done_reason") == "length":
        raise PlannerError("Ollama did not return a complete plan response")

    try:
        result = PlannerResult.model_validate_json(body["message"]["content"])
    except (KeyError, TypeError, ValidationError) as error:
        raise PlannerError("Ollama returned an invalid plan response") from error

    if result.plan is None and result.reason is not None:
        raise PlannerRejected(result.reason)
    if result.plan is None or result.reason is not None:
        raise PlannerError("Ollama returned an inconsistent plan response")

    try:
        ordered_steps = execution_order(result.plan.steps)
    except ValueError as error:
        raise PlannerError("Ollama plan failed dependency validation") from error

    return WorkflowPlan(steps=tuple(ordered_steps))
