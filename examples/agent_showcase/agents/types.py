"""Shared agent definition type. Split out from banking.py/healthcare.py so both
(and the runner's sub-agent spawn path) can share one type without cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    domain: str
    description: str
    system_prompt: str
    tools: list[dict[str, Any]]
    dispatch: dict[str, Callable[[dict[str, Any]], Any]]
    scenario_message: str
    model: str = "claude-sonnet-5"
    provider: str = "anthropic"
    fallback_file: str = ""
    # tool name -> the AgentDef of a sub-agent spawned when that tool is called.
    # The tool's dispatch entry (if any) is ignored for these names; the runner
    # runs a nested AgentRunner instead and its final text becomes the tool result.
    sub_agent_tools: dict[str, "AgentDef"] = field(default_factory=dict)

    @property
    def sub_agents(self) -> list[str]:
        return [sub.name for sub in self.sub_agent_tools.values()]

    @property
    def declared_tool_names(self) -> set[str]:
        """Tool names this agent's model-facing schema actually offers. A tool
        call whose name falls outside this set (and isn't a sub-agent trigger)
        was never granted to the model — see is_out_of_scope."""
        return {t["name"] for t in self.tools}

    def is_out_of_scope(self, tool_name: str) -> bool:
        return tool_name not in self.declared_tool_names and tool_name not in self.sub_agent_tools
