from enum import StrEnum


class ToolDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ToolPolicyViolation(ValueError):
    pass


_TOOL_POLICY = {
    "list_tickets": ToolDecision.ALLOW,
    "summarize_tickets": ToolDecision.ALLOW,
    "create_followup_tasks": ToolDecision.REQUIRE_APPROVAL,
}


def require_tool_permission(tool: str) -> ToolDecision:
    decision = _TOOL_POLICY.get(tool, ToolDecision.DENY)
    if decision is ToolDecision.DENY:
        raise ToolPolicyViolation("Tool execution is blocked by policy")
    return decision
