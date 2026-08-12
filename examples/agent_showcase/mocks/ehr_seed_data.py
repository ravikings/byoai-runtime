"""Synthetic seed data for the mock EHR. Fully fabricated: no real patients."""

from __future__ import annotations

# --- H1: Prior Authorization -----------------------------------------------

PATIENT_CHARTS = {
    "pt_4410": {
        "patient_id": "pt_4410",
        "name": "Elena Marchetti",
        "diagnosis": "Lumbar radiculopathy (M54.16)",
        "conservative_treatment_weeks": 8,
        "imaging": "MRI lumbar spine 2026-07-20: L4-L5 disc herniation with nerve root compression",
        "requested_procedure": "Lumbar epidural steroid injection",
    }
}

PAYER_CRITERIA = {
    "cpt_62323": {
        "cpt": "62323",
        "description": "Lumbar epidural steroid injection",
        "criteria": [
            "Documented radiculopathy consistent with imaging findings",
            "At least 6 weeks of conservative treatment attempted",
            "Imaging within 12 months confirming the level treated",
        ],
    }
}

# --- H2: Clinical Documentation (ambient scribe) ----------------------------

VISIT_TRANSCRIPTS = {
    "visit_7701": {
        "visit_id": "visit_7701",
        "patient_name": "Devon Okafor",
        "transcript": (
            "Patient reports three days of sore throat, low-grade fever, and mild cough. "
            "No shortness of breath. Denies chest pain. Exam: pharyngeal erythema, no exudate, "
            "lungs clear bilaterally, temp 100.2F. Rapid strep negative. Assessment: viral "
            "pharyngitis. Plan: supportive care, fluids, rest, return if worsening or fever >3 more days."
        ),
    }
}

# --- H3: Patient Intake & Scheduling -----------------------------------------

INTAKE_QUESTIONNAIRES = {
    "intake_2201": {
        "intake_id": "intake_2201",
        "patient_name": "Robin Vasquez",
        "answers": {
            "chief_complaint": "persistent headache, 4 days",
            "pain_scale": 5,
            "fever": False,
            "vision_changes": False,
            "worst_headache_of_life": False,
        },
    }
}

APPOINTMENT_SLOTS = [
    {"slot_id": "slot_101", "provider": "Dr. Nkemelu", "type": "primary_care", "start": "2026-08-14T09:00:00Z"},
    {"slot_id": "slot_102", "provider": "Dr. Fenwick", "type": "urgent_care", "start": "2026-08-12T14:00:00Z"},
]

# --- H4: Claims Denial Appeal -------------------------------------------------

CLAIM_DENIALS = {
    "denial_5501": {
        "denial_id": "denial_5501",
        "patient_name": "Amara Okonkwo",
        "cpt": "97110",
        "reason": "Not medically necessary per payer review",
        "denied_ts": "2026-08-01T00:00:00Z",
    }
}

CHART_EVIDENCE = {
    "denial_5501": {
        "supporting_notes": (
            "PT evaluation 2026-07-15 documents reduced lumbar ROM (flexion 40deg), "
            "functional limitation ambulating >2 blocks, and a documented plan of care "
            "with measurable functional goals reassessed weekly."
        ),
        "prior_therapy_response": "20% improvement in functional mobility over 4 sessions",
    }
}
