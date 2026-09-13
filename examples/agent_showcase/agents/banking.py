"""Banking demo agent definitions (B1-B5)."""

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
from ..mocks.case_data import CASES
from .types import AgentDef, Case


def _cases(agent_id: str) -> tuple[Case, ...]:
    return tuple(
        Case(
            id=c["id"],
            scenario_message=c["scenario_message"],
            fallback_file=c["fallback_file"],
            sub_cases={k: (v["scenario_message"], v["fallback_file"]) for k, v in c.get("sub", {}).items()},
        )
        for c in CASES[agent_id]
    )


B1_FRAUD_TRIAGE = AgentDef(
    id="b1-fraud-triage",
    name="Card Fraud Triage Agent",
    domain="banking",
    description=(
        "Works the card fraud alert queue raised by the Fraud Detection (Card/ACH) "
        "model: checks the transaction, customer history and geo/velocity, then "
        "records block or clear with a rationale. Never moves funds."
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
    system="card-fraud-ops",
    use_case="fraud-detection",
    cases=_cases("b1-fraud-triage"),
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
    name="Retail KYC Onboarding Agent",
    domain="banking",
    description=(
        "Verifies new retail account applicants: document consistency, a sanctions/PEP "
        "screen against the OFAC/Sanctions Screening model, and an onboarding risk "
        "score. Approves or escalates to BSA/AML; never opens an account itself."
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
    system="retail-onboarding",
    use_case="kyc-onboarding",
    cases=_cases("b2-kyc-onboarding"),
)

B3_DISPUTE_RESOLUTION = AgentDef(
    id="b3-dispute-resolution",
    name="Chargeback Resolution Agent",
    domain="banking",
    description=(
        "Handles card chargebacks under Reg E/Reg Z timelines: gathers merchant and "
        "fulfilment evidence, checks fraud-coded disputes against the Fraud Detection "
        "(Card/ACH) model, drafts the customer reply and posts provisional credit."
    ),
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
    system="card-disputes",
    use_case="customer-facing",
    cases=_cases("b3-dispute-resolution"),
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
    name="Consumer Loan Pre-Qualification Agent",
    domain="banking",
    description=(
        "Pre-qualifies unsecured personal loan requests: income and DTI from bank "
        "statements, checked against lending policy alongside the Consumer Credit "
        "Scorecard and, for secured requests, Collateral Valuation (AVM). Produces a "
        "pre-qualification only; a human underwriter issues the credit decision."
    ),
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
    system="consumer-lending",
    use_case="credit-decisioning",
    cases=_cases("b4-loan-prequalification"),
)

B5_MISFIRE_DEMO = AgentDef(
    id="b5-misfire-demo",
    # Same declared contract as B1. Some of its cached transcripts deliberately
    # call initiate_wire_transfer, a tool the model was never granted,
    # simulating a mis-fired / prompt-injected action, so the recorder has an
    # off-scope call to seal and Coriqo has one to flag. The display name
    # stays neutral: a real bank's registry would not label the agent a demo.
    name="High-Value Card Alerts Agent",
    domain="banking",
    description=(
        "Triages high-value card fraud alerts from the Fraud Detection (Card/ACH) "
        "model for the payments operations desk: transaction, customer history and "
        "geo/velocity checks, then block or clear. Never moves funds."
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
    system="payments-ops",
    use_case="fraud-detection",
    cases=_cases("b5-misfire-demo"),
)

AGENTS: list[AgentDef] = [
    B1_FRAUD_TRIAGE,
    B2_KYC_ONBOARDING,
    B3_DISPUTE_RESOLUTION,
    B4_LOAN_PREQUALIFICATION,
    B5_MISFIRE_DEMO,
]
