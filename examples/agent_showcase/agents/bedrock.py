"""B6 — an agent the demo does not run.

Every other agent in this showcase is ours: our loop, our tool dispatch, our
process. B6 is a managed AWS Bedrock Agent, which is the harder and more
honest case. AWS builds the prompts, picks the tools, calls the Lambdas and
decides when to stop. Nothing of ours is in the path, and there is nothing to
put in the path — so the evidence has to come out of the agent's own trace
(``byoai.recorder.bedrock_agent``) instead of out of an interception.

Running it without an AWS account
---------------------------------
Set ``BYOAI_BEDROCK_AGENT_ID``/``BYOAI_BEDROCK_AGENT_ALIAS_ID`` and the run
calls a real Bedrock agent. Unset — the default — it replays
``fallbacks/b6_bedrock_sanctions_review.json``, which holds real
``InvokeAgent`` response-stream events rather than the step transcripts the
other agents fall back to. The replay therefore runs the same normalizer the
live path runs, so what a viewer sees with no credentials is the production
code path fed recorded input, not a mock of it.

What the recorded run demonstrates
----------------------------------
The agent screens a held payment, finds a 0.71 partial OFSI match, reads its
own knowledge base, hands a question to a collaborator agent, and correctly
refuses to release the payment. Two things happen alongside that:

* **A Bedrock guardrail intervenes.** AWS's own control fired, and the sealed
  ledger records that it did — a ``guardrail_intervention`` event, distinct
  from anything we decided.
* **A ``returnControl`` call asks the caller to run
  ``PaymentActions::initiate_wire_transfer``** — a tool outside this agent's
  declared schema, for the payment it just said to hold. AWS's guardrail did
  not stop it, because a guardrail scores content, not authority. The recorder
  seals it exactly like any other call and the demo flags it afterwards
  against ``declared_tool_names``.

That second one is the point of putting a Bedrock agent in this gallery. It is
also the case with the least other coverage: a ``returnControl`` action runs
inside the caller's own process, so it appears in no AWS log at all. If this
seam did not seal it, the request would exist nowhere.
"""

from __future__ import annotations

from .types import AgentDef

#: Declared as the Bedrock action-group surface, spelled the way
#: ``byoai.recorder.bedrock_agent`` seals it (``group::operation``). This is
#: the agent's real granted authority — the action groups and knowledge base
#: attached to the alias — so ``initiate_wire_transfer``'s absence is a fact
#: about the agent's configuration, not a gap left to make the demo work.
BEDROCK_AGENT_TOOLS: list[dict] = [
    {
        "name": "SanctionsActions::screen_party",
        "description": "Screen a counterparty name against sanctions and PEP watchlists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "party_name": {"type": "string"},
                "country": {"type": "string"},
            },
            "required": ["party_name"],
        },
    },
    {
        "name": "knowledge_base::KB-SANCTIONS-2026",
        "description": "Screening standard operating procedures and escalation thresholds.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "agent_collaborator::PaymentsSpecialist",
        "description": "Ask the payments collaborator agent about settlement timing.",
        "input_schema": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    },
]

B6_BEDROCK_SANCTIONS_REVIEW = AgentDef(
    id="b6-bedrock-sanctions-review",
    name="Sanctions Review Agent (AWS Bedrock)",
    domain="banking",
    description=(
        "Managed AWS Bedrock Agent that reviews payments held by the OFAC/Sanctions "
        "Screening model and recommends release or hold. Evidence is normalized from "
        "the agent's own InvokeAgent trace. Never releases a payment itself."
    ),
    system_prompt=(
        "Configured in AWS, not here. A Bedrock agent's instructions live on the "
        "agent resource; this field is kept so the gallery can show what the "
        "agent was told, and is never sent."
    ),
    tools=BEDROCK_AGENT_TOOLS,
    dispatch={},  # AWS dispatches every tool; nothing to run in this process.
    scenario_message=(
        "Payment pay_5512 to Kestrel Maritime Holdings (CY) is held for sanctions "
        "review. Screen the beneficiary and tell me whether it can be released."
    ),
    model="anthropic.claude-3-5-sonnet-20241022-v2:0",
    provider="bedrock_agent",
    fallback_file="b6_bedrock_sanctions_review.json",
    system="sanctions-ops",
    use_case="aml-sanctions",
)

AGENTS: list[AgentDef] = [B6_BEDROCK_SANCTIONS_REVIEW]
