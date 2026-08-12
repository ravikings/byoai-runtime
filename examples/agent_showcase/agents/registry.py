"""Catalog of all demo agents (B1-B4, H1-H4)."""

from __future__ import annotations

from .banking import AGENTS as BANKING_AGENTS
from .healthcare import AGENTS as HEALTHCARE_AGENTS
from .types import AgentDef

CATALOG: dict[str, AgentDef] = {agent.id: agent for agent in [*BANKING_AGENTS, *HEALTHCARE_AGENTS]}


def get_agent(agent_id: str) -> AgentDef | None:
    return CATALOG.get(agent_id)


def list_agents() -> list[AgentDef]:
    return list(CATALOG.values())
