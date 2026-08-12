"""Banking demo agent definitions (B1-B4)."""

from __future__ import annotations

from ..mocks.bank import (
    DISPUTE_DISPATCH,
    DISPUTE_TOOLS,
    DOCUMENT_EXTRACTOR_DISPATCH,
    DOCUMENT_EXTRACTOR_TOOLS,
    FRAUD_TRIAGE_DISPATCH,
    FRAUD_TRIAGE_TOOLS,
    KYC_DISPATCH,
    KYC_TOOLS,
    LOAN_DISPATCH,
    LOAN_TOOLS,
    MISFIRE_FRAUD_TRIAGE_DISPATCH,
    SANCTIONS_SCREENER_DISPATCH,
    SANCTIONS_SCREENER_TOOLS,
)
from .types import AgentDef

B1_FRAUD_TRIAGE = AgentDef(
    id="b1-fraud-triage",
    name="Fraud Triage",
    domain="banking",
    description=(
        "Reviews a flagged card transaction, checks history/geo/velocity, "
        "and recommends block or clear with rationale."
    ),
    system_prompt=(
        "You are a fraud triage analyst for a retail bank. You are given a flagged "
        "transaction id. Use the tools to pull the transaction, the customer's history, "
        "and a geo/velocity check, then call flag_decision exactly once with a clear "
        "block-or-clear decision and a short rationale citing the specific evidence you "
        "found. All data is synthetic demo data."
    ),
    tools=FRAUD_TRIAGE_TOOLS,
    dispatch=FRAUD_TRIAGE_DISPATCH,
    scenario_message="A card transaction was flagged for review: txn_8841. Triage it.",
    fallback_file="b1_fraud_triage.json",
)

_SANCTIONS_SCREENER = AgentDef(
    id="b2-sub-sanctions-screener",
    name="Sanctions Screener",
    domain="banking",
    description="Screens an applicant name against sanctions/PEP watchlists.",
    system_prompt=(
        "You are a sanctions screening sub-agent. Call sanctions_screen with the applicant's "
        "name, then summarize the result in one sentence: clear or matches found."
    ),
    tools=SANCTIONS_SCREENER_TOOLS,
    dispatch=SANCTIONS_SCREENER_DISPATCH,
    scenario_message="Screen applicant 'Priya Kestrel' against sanctions/PEP watchlists.",
    fallback_file="b2_sub_sanctions_screener.json",
)

B2_KYC_ONBOARDING = AgentDef(
    id="b2-kyc-onboarding",
    name="KYC / Onboarding",
    domain="banking",
    description=(
        "Verifies a new customer: document consistency, sanctions/PEP screen, risk score."
    ),
    system_prompt=(
        "You are a KYC onboarding analyst. You are given an application id. Read the "
        "submitted documents, screen the applicant against sanctions/PEP watchlists, "
        "compute a risk score, then call approve_or_escalate exactly once with your "
        "decision and rationale. All data is synthetic demo data. Digest-only mode: "
        "treat document contents as sensitive and reference them by field name only, "
        "not by quoting raw document text."
    ),
    tools=KYC_TOOLS,
    dispatch=KYC_DISPATCH,
    scenario_message="A new customer application was submitted: app_5510. Run KYC onboarding.",
    fallback_file="b2_kyc_onboarding.json",
    sub_agent_tools={"sanctions_screen": _SANCTIONS_SCREENER},
)

B3_DISPUTE_RESOLUTION = AgentDef(
    id="b3-dispute-resolution",
    name="Dispute Resolution",
    domain="banking",
    description="Handles a chargeback: gathers evidence, drafts customer reply, files provisional credit.",
    system_prompt=(
        "You are a dispute resolution analyst. You are given a dispute id. Look up the "
        "dispute, gather evidence, draft a reply to the customer, and post a provisional "
        "credit for the disputed amount if evidence supports the customer's claim. "
        "All data is synthetic demo data."
    ),
    tools=DISPUTE_TOOLS,
    dispatch=DISPUTE_DISPATCH,
    scenario_message="A chargeback dispute was filed: disp_3301. Resolve it.",
    fallback_file="b3_dispute_resolution.json",
)

_DOCUMENT_EXTRACTOR = AgentDef(
    id="b4-sub-document-extractor",
    name="Document Extractor",
    domain="banking",
    description="Extracts income/debt figures from an applicant's bank statements.",
    system_prompt=(
        "You are a document extraction sub-agent. Call get_statements with the loan id, "
        "then summarize the extracted monthly income and debt payments in one sentence."
    ),
    tools=DOCUMENT_EXTRACTOR_TOOLS,
    dispatch=DOCUMENT_EXTRACTOR_DISPATCH,
    scenario_message="Extract income/debt figures from the bank statements for loan_9042.",
    fallback_file="b4_sub_document_extractor.json",
)

B4_LOAN_PREQUALIFICATION = AgentDef(
    id="b4-loan-prequalification",
    name="Loan Pre-Qualification",
    domain="banking",
    description="Income/DTI analysis from statements, produces a pre-qual decision with cited figures.",
    system_prompt=(
        "You are a loan pre-qualification analyst. You are given a loan application id. "
        "Extract income/debt figures from the applicant's statements, compute DTI, check it "
        "against policy, then call decision exactly once citing the specific figures. "
        "All data is synthetic demo data."
    ),
    tools=LOAN_TOOLS,
    dispatch=LOAN_DISPATCH,
    scenario_message="A loan pre-qualification request was submitted: loan_9042. Evaluate it.",
    fallback_file="b4_loan_prequalification.json",
    sub_agent_tools={"get_statements": _DOCUMENT_EXTRACTOR},
)

B5_MISFIRE_DEMO = AgentDef(
    id="b5-misfire-demo",
    name="Fraud Triage (misfire demo)",
    domain="banking",
    description=(
        "Same declared contract as B1 Fraud Triage. Its fallback transcript "
        "deliberately calls a tool the model was never granted, simulating a "
        "mis-fired / prompt-injected action — demonstrates that the recorder "
        "captures and flags off-scope tool calls rather than missing them."
    ),
    system_prompt=(
        "You are a fraud triage analyst for a retail bank. You are given a flagged "
        "transaction id. Use the tools to pull the transaction, the customer's history, "
        "and a geo/velocity check, then call flag_decision exactly once with a clear "
        "block-or-clear decision and a short rationale citing the specific evidence you "
        "found. All data is synthetic demo data."
    ),
    tools=FRAUD_TRIAGE_TOOLS,
    dispatch=MISFIRE_FRAUD_TRIAGE_DISPATCH,
    scenario_message="A card transaction was flagged for review: txn_8841. Triage it.",
    fallback_file="b5_misfire_wire_transfer.json",
)

AGENTS: list[AgentDef] = [
    B1_FRAUD_TRIAGE,
    B2_KYC_ONBOARDING,
    B3_DISPUTE_RESOLUTION,
    B4_LOAN_PREQUALIFICATION,
    B5_MISFIRE_DEMO,
]
