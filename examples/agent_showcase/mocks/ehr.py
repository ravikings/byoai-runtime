"""In-process mock EHR service used by the healthcare demo agents.

Seeded, synthetic data only (see ehr_seed_data.py). No network calls, no real
patient data.
"""

from __future__ import annotations

from typing import Any

from . import ehr_seed_data as seed


class MockEHR:
    # --- H1 ---
    def get_chart(self, patient_id: str) -> dict[str, Any]:
        chart = seed.PATIENT_CHARTS.get(patient_id)
        if chart is None:
            return {"error": f"unknown patient: {patient_id}"}
        return chart

    def payer_criteria(self, cpt: str) -> dict[str, Any]:
        criteria = seed.PAYER_CRITERIA.get(cpt)
        if criteria is None:
            return {"error": f"no payer criteria on file for cpt: {cpt}"}
        return criteria

    def match_criteria(self, patient_id: str, cpt: str) -> dict[str, Any]:
        chart = seed.PATIENT_CHARTS.get(patient_id)
        criteria = seed.PAYER_CRITERIA.get(cpt)
        if chart is None or criteria is None:
            return {"error": "missing chart or payer criteria"}
        met = chart["conservative_treatment_weeks"] >= 6
        return {"criteria_met": met, "conservative_treatment_weeks": chart["conservative_treatment_weeks"]}

    def draft_auth(self, patient_id: str, cpt: str, justification: str) -> dict[str, Any]:
        return {"patient_id": patient_id, "cpt": cpt, "justification": justification, "submitted": True}

    # --- H2 ---
    def get_transcript(self, visit_id: str) -> dict[str, Any]:
        visit = seed.VISIT_TRANSCRIPTS.get(visit_id)
        if visit is None:
            return {"error": f"unknown visit: {visit_id}"}
        return visit

    def draft_soap(self, visit_id: str, soap_note: str) -> dict[str, Any]:
        return {"visit_id": visit_id, "soap_note": soap_note, "drafted": True}

    def suggest_codes(self, visit_id: str, icd10: list[str], cpt: list[str]) -> dict[str, Any]:
        return {"visit_id": visit_id, "icd10": icd10, "cpt": cpt}

    # --- H3 ---
    def intake_answers(self, intake_id: str) -> dict[str, Any]:
        intake = seed.INTAKE_QUESTIONNAIRES.get(intake_id)
        if intake is None:
            return {"error": f"unknown intake: {intake_id}"}
        return intake

    def triage_rules(self, intake_id: str) -> dict[str, Any]:
        intake = seed.INTAKE_QUESTIONNAIRES.get(intake_id)
        if intake is None:
            return {"error": f"unknown intake: {intake_id}"}
        answers = intake["answers"]
        urgent = answers.get("worst_headache_of_life") or answers.get("vision_changes") or answers.get("fever")
        return {"urgency": "urgent_care" if urgent else "primary_care"}

    def find_slots(self, urgency: str) -> dict[str, Any]:
        matches = [s for s in seed.APPOINTMENT_SLOTS if s["type"] == urgency]
        return {"slots": matches}

    def book(self, intake_id: str, slot_id: str) -> dict[str, Any]:
        slot = next((s for s in seed.APPOINTMENT_SLOTS if s["slot_id"] == slot_id), None)
        if slot is None:
            return {"error": f"unknown slot: {slot_id}"}
        return {"intake_id": intake_id, "slot_id": slot_id, "booked": True, "slot": slot}

    # --- H4 ---
    def get_denial(self, denial_id: str) -> dict[str, Any]:
        denial = seed.CLAIM_DENIALS.get(denial_id)
        if denial is None:
            return {"error": f"unknown denial: {denial_id}"}
        return denial

    def search_chart(self, denial_id: str) -> dict[str, Any]:
        evidence = seed.CHART_EVIDENCE.get(denial_id)
        if evidence is None:
            return {"error": f"no chart evidence on file for denial: {denial_id}"}
        return evidence

    def draft_appeal(self, denial_id: str, appeal_letter: str) -> dict[str, Any]:
        return {"denial_id": denial_id, "appeal_letter": appeal_letter, "drafted": True}


EHR = MockEHR()

# --- H1: Prior Authorization -------------------------------------------------

PRIOR_AUTH_TOOLS = [
    {
        "name": "get_chart",
        "description": "Pull the patient's chart.",
        "input_schema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string"}},
            "required": ["patient_id"],
        },
    },
    {
        "name": "payer_criteria",
        "description": "Look up the payer's prior-auth criteria for a CPT code.",
        "input_schema": {
            "type": "object",
            "properties": {"cpt": {"type": "string"}},
            "required": ["cpt"],
        },
    },
    {
        "name": "match_criteria",
        "description": "Check the chart against payer criteria via the Criteria Matcher sub-agent.",
        "input_schema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string"}, "cpt": {"type": "string"}},
            "required": ["patient_id", "cpt"],
        },
    },
    {
        "name": "draft_auth",
        "description": "Draft and submit the prior-auth request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "cpt": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["patient_id", "cpt", "justification"],
        },
    },
]

PRIOR_AUTH_DISPATCH = {
    "get_chart": lambda args: EHR.get_chart(args["patient_id"]),
    "payer_criteria": lambda args: EHR.payer_criteria(args["cpt"]),
    "draft_auth": lambda args: EHR.draft_auth(args["patient_id"], args["cpt"], args["justification"]),
    # "match_criteria" routes to the Criteria Matcher sub-agent instead.
}

CRITERIA_MATCHER_TOOLS = [
    {
        "name": "match_criteria",
        "description": "Match a patient's chart against payer prior-auth criteria.",
        "input_schema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string"}, "cpt": {"type": "string"}},
            "required": ["patient_id", "cpt"],
        },
    }
]

CRITERIA_MATCHER_DISPATCH = {
    "match_criteria": lambda args: EHR.match_criteria(args["patient_id"], args["cpt"]),
}

# --- H2: Clinical Documentation ----------------------------------------------

SCRIBE_TOOLS = [
    {
        "name": "get_transcript",
        "description": "Get the synthetic visit transcript.",
        "input_schema": {
            "type": "object",
            "properties": {"visit_id": {"type": "string"}},
            "required": ["visit_id"],
        },
    },
    {
        "name": "draft_soap",
        "description": "Draft a SOAP note from the transcript.",
        "input_schema": {
            "type": "object",
            "properties": {"visit_id": {"type": "string"}, "soap_note": {"type": "string"}},
            "required": ["visit_id", "soap_note"],
        },
    },
    {
        "name": "suggest_codes",
        "description": "Suggest ICD-10/CPT codes for the visit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visit_id": {"type": "string"},
                "icd10": {"type": "array", "items": {"type": "string"}},
                "cpt": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["visit_id", "icd10", "cpt"],
        },
    },
]

SCRIBE_DISPATCH = {
    "get_transcript": lambda args: EHR.get_transcript(args["visit_id"]),
    "draft_soap": lambda args: EHR.draft_soap(args["visit_id"], args["soap_note"]),
    "suggest_codes": lambda args: EHR.suggest_codes(args["visit_id"], args["icd10"], args["cpt"]),
}

# --- H3: Patient Intake & Scheduling ------------------------------------------

INTAKE_TOOLS = [
    {
        "name": "intake_answers",
        "description": "Get the patient's intake questionnaire answers.",
        "input_schema": {
            "type": "object",
            "properties": {"intake_id": {"type": "string"}},
            "required": ["intake_id"],
        },
    },
    {
        "name": "triage_rules",
        "description": "Apply triage rules to determine urgency.",
        "input_schema": {
            "type": "object",
            "properties": {"intake_id": {"type": "string"}},
            "required": ["intake_id"],
        },
    },
    {
        "name": "find_slots",
        "description": "Find open appointment slots matching an urgency level.",
        "input_schema": {
            "type": "object",
            "properties": {"urgency": {"type": "string"}},
            "required": ["urgency"],
        },
    },
    {
        "name": "book",
        "description": "Book the appointment.",
        "input_schema": {
            "type": "object",
            "properties": {"intake_id": {"type": "string"}, "slot_id": {"type": "string"}},
            "required": ["intake_id", "slot_id"],
        },
    },
]

INTAKE_DISPATCH = {
    "intake_answers": lambda args: EHR.intake_answers(args["intake_id"]),
    "triage_rules": lambda args: EHR.triage_rules(args["intake_id"]),
    "find_slots": lambda args: EHR.find_slots(args["urgency"]),
    "book": lambda args: EHR.book(args["intake_id"], args["slot_id"]),
}

# --- H4: Claims Denial Appeal --------------------------------------------------

DENIAL_APPEAL_TOOLS = [
    {
        "name": "get_denial",
        "description": "Look up a claim denial by id.",
        "input_schema": {
            "type": "object",
            "properties": {"denial_id": {"type": "string"}},
            "required": ["denial_id"],
        },
    },
    {
        "name": "search_chart",
        "description": "Search the chart for evidence supporting an appeal.",
        "input_schema": {
            "type": "object",
            "properties": {"denial_id": {"type": "string"}},
            "required": ["denial_id"],
        },
    },
    {
        "name": "draft_appeal",
        "description": "Draft the appeal letter.",
        "input_schema": {
            "type": "object",
            "properties": {"denial_id": {"type": "string"}, "appeal_letter": {"type": "string"}},
            "required": ["denial_id", "appeal_letter"],
        },
    },
]

DENIAL_APPEAL_DISPATCH = {
    "get_denial": lambda args: EHR.get_denial(args["denial_id"]),
    "search_chart": lambda args: EHR.search_chart(args["denial_id"]),
    "draft_appeal": lambda args: EHR.draft_appeal(args["denial_id"], args["appeal_letter"]),
}
