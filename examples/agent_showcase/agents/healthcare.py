"""Healthcare demo agent definitions (H1-H4)."""

from __future__ import annotations

from ..mocks.ehr import (
    CRITERIA_MATCHER_DISPATCH,
    CRITERIA_MATCHER_TOOLS,
    DENIAL_APPEAL_DISPATCH,
    DENIAL_APPEAL_TOOLS,
    INTAKE_DISPATCH,
    INTAKE_TOOLS,
    PRIOR_AUTH_DISPATCH,
    PRIOR_AUTH_TOOLS,
    SCRIBE_DISPATCH,
    SCRIBE_TOOLS,
)
from .types import AgentDef

_CRITERIA_MATCHER = AgentDef(
    id="h1-sub-criteria-matcher",
    name="Criteria Matcher",
    domain="healthcare",
    description="Matches a patient's chart against payer prior-auth criteria.",
    system_prompt=(
        "You are a criteria matching sub-agent. Call match_criteria with the patient and "
        "CPT code, then summarize in one sentence whether criteria are met."
    ),
    tools=CRITERIA_MATCHER_TOOLS,
    dispatch=CRITERIA_MATCHER_DISPATCH,
    scenario_message="Match patient pt_4410's chart against payer criteria for CPT 62323.",
    fallback_file="h1_sub_criteria_matcher.json",
    model="gpt-4o",
    provider="openai",
)

H1_PRIOR_AUTHORIZATION = AgentDef(
    id="h1-prior-authorization",
    name="Prior Authorization",
    domain="healthcare",
    description="Builds a prior-auth request: pulls chart, matches payer criteria, drafts submission.",
    system_prompt=(
        "You are a prior authorization specialist. You are given a patient id and the "
        "requested procedure's CPT code. Pull the chart, get payer criteria, check whether "
        "the chart matches those criteria, then draft and submit the prior-auth request with "
        "a justification citing specific chart findings. All data is synthetic demo data."
    ),
    tools=PRIOR_AUTH_TOOLS,
    dispatch=PRIOR_AUTH_DISPATCH,
    scenario_message=(
        "Build a prior-auth request for patient pt_4410, requested procedure CPT 62323 "
        "(lumbar epidural steroid injection)."
    ),
    fallback_file="h1_prior_authorization.json",
    model="gpt-4o",
    provider="openai",
    sub_agent_tools={"match_criteria": _CRITERIA_MATCHER},
)

H2_CLINICAL_DOCUMENTATION = AgentDef(
    id="h2-clinical-documentation",
    name="Clinical Documentation (ambient scribe)",
    domain="healthcare",
    description="Turns a synthetic visit transcript into a SOAP note + ICD-10/CPT suggestions.",
    system_prompt=(
        "You are an ambient clinical scribe. You are given a visit id. Pull the visit "
        "transcript, draft a structured SOAP note from it, then suggest ICD-10 and CPT "
        "codes consistent with the note. All data is synthetic demo data."
    ),
    tools=SCRIBE_TOOLS,
    dispatch=SCRIBE_DISPATCH,
    scenario_message="Document visit_7701 from the recorded transcript.",
    fallback_file="h2_clinical_documentation.json",
    model="gpt-4o",
    provider="openai",
)

H3_PATIENT_INTAKE = AgentDef(
    id="h3-patient-intake",
    name="Patient Intake & Scheduling",
    domain="healthcare",
    description="Triage questionnaire -> urgency level -> books the right slot.",
    system_prompt=(
        "You are a patient intake and scheduling assistant. You are given an intake id. "
        "Read the questionnaire answers, apply triage rules to determine urgency, find "
        "matching open slots, then book the most appropriate one. All data is synthetic "
        "demo data."
    ),
    tools=INTAKE_TOOLS,
    dispatch=INTAKE_DISPATCH,
    scenario_message="Triage and schedule intake_2201.",
    fallback_file="h3_patient_intake.json",
    model="gpt-4o",
    provider="openai",
)

H4_CLAIMS_DENIAL_APPEAL = AgentDef(
    id="h4-claims-denial-appeal",
    name="Claims Denial Appeal",
    domain="healthcare",
    description="Reads a denial, finds supporting chart evidence, drafts an appeal letter.",
    system_prompt=(
        "You are a claims denial appeal specialist. You are given a denial id. Look up the "
        "denial reason, search the chart for supporting evidence, then draft an appeal "
        "letter citing that evidence directly. All data is synthetic demo data."
    ),
    tools=DENIAL_APPEAL_TOOLS,
    dispatch=DENIAL_APPEAL_DISPATCH,
    scenario_message="Appeal denial_5501.",
    fallback_file="h4_claims_denial_appeal.json",
    model="gpt-4o",
    provider="openai",
)

AGENTS: list[AgentDef] = [
    H1_PRIOR_AUTHORIZATION,
    H2_CLINICAL_DOCUMENTATION,
    H3_PATIENT_INTAKE,
    H4_CLAIMS_DENIAL_APPEAL,
]
