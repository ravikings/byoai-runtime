"""In-process mock core-banking service used by the banking demo agents.

Seeded, synthetic data only (see seed_data.py). No network calls.
"""

from __future__ import annotations

from typing import Any

from . import seed_data


class MockBank:
    def get_transaction(self, txn_id: str) -> dict[str, Any]:
        txn = seed_data.TRANSACTIONS.get(txn_id)
        if txn is None:
            return {"error": f"unknown transaction: {txn_id}"}
        return txn

    def get_customer_history(self, customer_id: str) -> dict[str, Any]:
        history = seed_data.CUSTOMER_HISTORY.get(customer_id)
        if history is None:
            return {"error": f"unknown customer: {customer_id}"}
        return history

    def geo_velocity_check(self, txn_id: str) -> dict[str, Any]:
        result = seed_data.GEO_VELOCITY.get(txn_id)
        if result is None:
            return {"error": f"no geo/velocity data for transaction: {txn_id}"}
        return result

    def flag_decision(self, txn_id: str, decision: str, rationale: str) -> dict[str, Any]:
        if decision not in ("block", "clear"):
            return {"error": f"invalid decision: {decision!r}, expected 'block' or 'clear'"}
        return {"txn_id": txn_id, "decision": decision, "rationale": rationale, "recorded": True}

    # --- B5 misfire demo: a tool the model was never granted, but the mock
    # core-banking service still exposes internally (e.g. via a legacy
    # integration). If an agent calls it anyway, it actually "succeeds" —
    # that's the point: the danger isn't a crash, it's a silent action
    # outside the agent's declared contract. See agents/banking.py B5.

    def initiate_wire_transfer(self, account_id: str, amount_usd: float, destination: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "amount_usd": amount_usd,
            "destination": destination,
            "status": "initiated",
            "wire_id": "wire_" + account_id[-4:] + "_" + destination[-4:],
        }

    # --- B2: KYC / Onboarding ---

    def read_documents(self, application_id: str) -> dict[str, Any]:
        app = seed_data.ONBOARDING_APPLICATIONS.get(application_id)
        if app is None:
            return {"error": f"unknown application: {application_id}"}
        names = {d.get("name_on_doc") for d in app["documents"]}
        return {
            "applicant_name": app["applicant_name"],
            "documents": app["documents"],
            "name_consistent": len(names) == 1 and app["applicant_name"] in names,
        }

    def sanctions_screen(self, applicant_name: str) -> dict[str, Any]:
        result = seed_data.SANCTIONS_SCREEN_RESULTS.get(applicant_name)
        if result is None:
            return {"matches": [], "watchlists_checked": ["OFAC-SDN", "UN-Consolidated", "EU-Sanctions"]}
        return result

    def risk_score(self, application_id: str) -> dict[str, Any]:
        app = seed_data.ONBOARDING_APPLICATIONS.get(application_id)
        if app is None:
            return {"error": f"unknown application: {application_id}"}
        return {"application_id": application_id, "risk_score": 12, "risk_band": "low"}

    def approve_or_escalate(self, application_id: str, decision: str, rationale: str) -> dict[str, Any]:
        if decision not in ("approve", "escalate"):
            return {"error": f"invalid decision: {decision!r}, expected 'approve' or 'escalate'"}
        return {"application_id": application_id, "decision": decision, "rationale": rationale, "recorded": True}

    # --- B3: Dispute Resolution ---

    def get_dispute(self, dispute_id: str) -> dict[str, Any]:
        dispute = seed_data.DISPUTES.get(dispute_id)
        if dispute is None:
            return {"error": f"unknown dispute: {dispute_id}"}
        return dispute

    def gather_evidence(self, dispute_id: str) -> dict[str, Any]:
        evidence = seed_data.DISPUTE_EVIDENCE.get(dispute_id)
        if evidence is None:
            return {"error": f"no evidence on file for dispute: {dispute_id}"}
        return evidence

    def draft_reply(self, dispute_id: str, message: str) -> dict[str, Any]:
        return {"dispute_id": dispute_id, "drafted": True, "message": message}

    def post_credit(self, dispute_id: str, amount_usd: float) -> dict[str, Any]:
        return {"dispute_id": dispute_id, "provisional_credit_usd": amount_usd, "posted": True}

    # --- B4: Loan Pre-Qualification ---

    def get_statements(self, loan_id: str) -> dict[str, Any]:
        statements = seed_data.LOAN_STATEMENTS.get(loan_id)
        if statements is None:
            return {"error": f"no statements on file for loan: {loan_id}"}
        return statements

    def compute_dti(self, monthly_income_usd: float, monthly_debt_payments_usd: float) -> dict[str, Any]:
        if monthly_income_usd <= 0:
            return {"error": "monthly_income_usd must be positive"}
        dti = round(monthly_debt_payments_usd / monthly_income_usd, 4)
        return {"dti": dti}

    def policy_check(self, dti: float, monthly_income_usd: float) -> dict[str, Any]:
        policy = seed_data.LOAN_POLICY
        return {
            "within_dti_limit": dti <= policy["max_dti"],
            "meets_min_income": monthly_income_usd >= policy["min_monthly_income_usd"],
            "policy": policy,
        }

    def decision(self, loan_id: str, decision: str, rationale: str) -> dict[str, Any]:
        if decision not in ("pre_qualified", "declined"):
            return {"error": f"invalid decision: {decision!r}, expected 'pre_qualified' or 'declined'"}
        return {"loan_id": loan_id, "decision": decision, "rationale": rationale, "recorded": True}


# Tool schemas (Anthropic tool-use format) + dispatch table for B1 Fraud Triage.
BANK = MockBank()

FRAUD_TRIAGE_TOOLS = [
    {
        "name": "get_transaction",
        "description": "Look up a flagged card transaction by id.",
        "input_schema": {
            "type": "object",
            "properties": {"txn_id": {"type": "string"}},
            "required": ["txn_id"],
        },
    },
    {
        "name": "get_customer_history",
        "description": "Get a customer's recent transaction history and home location.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "geo_velocity_check",
        "description": "Check geographic distance/time since the customer's prior transaction.",
        "input_schema": {
            "type": "object",
            "properties": {"txn_id": {"type": "string"}},
            "required": ["txn_id"],
        },
    },
    {
        "name": "flag_decision",
        "description": "Record the final block/clear decision with rationale.",
        "input_schema": {
            "type": "object",
            "properties": {
                "txn_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["block", "clear"]},
                "rationale": {"type": "string"},
            },
            "required": ["txn_id", "decision", "rationale"],
        },
    },
]

FRAUD_TRIAGE_DISPATCH = {
    "get_transaction": lambda args: BANK.get_transaction(args["txn_id"]),
    "get_customer_history": lambda args: BANK.get_customer_history(args["customer_id"]),
    "geo_velocity_check": lambda args: BANK.geo_velocity_check(args["txn_id"]),
    "flag_decision": lambda args: BANK.flag_decision(args["txn_id"], args["decision"], args["rationale"]),
}

# --- B2: KYC / Onboarding ---

KYC_TOOLS = [
    {
        "name": "read_documents",
        "description": "Read the applicant's submitted documents and check name consistency.",
        "input_schema": {
            "type": "object",
            "properties": {"application_id": {"type": "string"}},
            "required": ["application_id"],
        },
    },
    {
        "name": "sanctions_screen",
        "description": "Run the applicant through sanctions/PEP watchlists via the Sanctions Screener sub-agent.",
        "input_schema": {
            "type": "object",
            "properties": {"applicant_name": {"type": "string"}},
            "required": ["applicant_name"],
        },
    },
    {
        "name": "risk_score",
        "description": "Compute a risk score for the application.",
        "input_schema": {
            "type": "object",
            "properties": {"application_id": {"type": "string"}},
            "required": ["application_id"],
        },
    },
    {
        "name": "approve_or_escalate",
        "description": "Record the final approve/escalate decision with rationale.",
        "input_schema": {
            "type": "object",
            "properties": {
                "application_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["approve", "escalate"]},
                "rationale": {"type": "string"},
            },
            "required": ["application_id", "decision", "rationale"],
        },
    },
]

KYC_DISPATCH = {
    "read_documents": lambda args: BANK.read_documents(args["application_id"]),
    "risk_score": lambda args: BANK.risk_score(args["application_id"]),
    "approve_or_escalate": lambda args: BANK.approve_or_escalate(
        args["application_id"], args["decision"], args["rationale"]
    ),
    # "sanctions_screen" is intentionally absent: B2's AgentDef routes it to
    # the Sanctions Screener sub-agent instead (see agents/banking.py).
}

SANCTIONS_SCREENER_TOOLS = [
    {
        "name": "sanctions_screen",
        "description": "Screen a name against OFAC/UN/EU sanctions and PEP watchlists.",
        "input_schema": {
            "type": "object",
            "properties": {"applicant_name": {"type": "string"}},
            "required": ["applicant_name"],
        },
    }
]

SANCTIONS_SCREENER_DISPATCH = {
    "sanctions_screen": lambda args: BANK.sanctions_screen(args["applicant_name"]),
}

# --- B3: Dispute Resolution ---

DISPUTE_TOOLS = [
    {
        "name": "get_dispute",
        "description": "Look up a filed chargeback dispute by id.",
        "input_schema": {
            "type": "object",
            "properties": {"dispute_id": {"type": "string"}},
            "required": ["dispute_id"],
        },
    },
    {
        "name": "gather_evidence",
        "description": "Gather shipment/merchant evidence for the dispute.",
        "input_schema": {
            "type": "object",
            "properties": {"dispute_id": {"type": "string"}},
            "required": ["dispute_id"],
        },
    },
    {
        "name": "draft_reply",
        "description": "Draft a reply message to the customer.",
        "input_schema": {
            "type": "object",
            "properties": {"dispute_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["dispute_id", "message"],
        },
    },
    {
        "name": "post_credit",
        "description": "Post a provisional credit for the disputed amount.",
        "input_schema": {
            "type": "object",
            "properties": {"dispute_id": {"type": "string"}, "amount_usd": {"type": "number"}},
            "required": ["dispute_id", "amount_usd"],
        },
    },
]

DISPUTE_DISPATCH = {
    "get_dispute": lambda args: BANK.get_dispute(args["dispute_id"]),
    "gather_evidence": lambda args: BANK.gather_evidence(args["dispute_id"]),
    "draft_reply": lambda args: BANK.draft_reply(args["dispute_id"], args["message"]),
    "post_credit": lambda args: BANK.post_credit(args["dispute_id"], args["amount_usd"]),
}

# --- B4: Loan Pre-Qualification ---

LOAN_TOOLS = [
    {
        "name": "get_statements",
        "description": "Pull income/debt figures extracted from bank statements via the Document Extractor sub-agent.",
        "input_schema": {
            "type": "object",
            "properties": {"loan_id": {"type": "string"}},
            "required": ["loan_id"],
        },
    },
    {
        "name": "compute_dti",
        "description": "Compute debt-to-income ratio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "monthly_income_usd": {"type": "number"},
                "monthly_debt_payments_usd": {"type": "number"},
            },
            "required": ["monthly_income_usd", "monthly_debt_payments_usd"],
        },
    },
    {
        "name": "policy_check",
        "description": "Check DTI/income against pre-qualification policy.",
        "input_schema": {
            "type": "object",
            "properties": {"dti": {"type": "number"}, "monthly_income_usd": {"type": "number"}},
            "required": ["dti", "monthly_income_usd"],
        },
    },
    {
        "name": "decision",
        "description": "Record the final pre-qualification decision with rationale citing figures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["pre_qualified", "declined"]},
                "rationale": {"type": "string"},
            },
            "required": ["loan_id", "decision", "rationale"],
        },
    },
]

LOAN_DISPATCH = {
    "compute_dti": lambda args: BANK.compute_dti(args["monthly_income_usd"], args["monthly_debt_payments_usd"]),
    "policy_check": lambda args: BANK.policy_check(args["dti"], args["monthly_income_usd"]),
    "decision": lambda args: BANK.decision(args["loan_id"], args["decision"], args["rationale"]),
    # "get_statements" is intentionally absent: B4's AgentDef routes it to the
    # Document Extractor sub-agent instead (see agents/banking.py).
}

DOCUMENT_EXTRACTOR_TOOLS = [
    {
        "name": "get_statements",
        "description": "Extract income/debt figures from the applicant's bank statements.",
        "input_schema": {
            "type": "object",
            "properties": {"loan_id": {"type": "string"}},
            "required": ["loan_id"],
        },
    }
]

DOCUMENT_EXTRACTOR_DISPATCH = {
    "get_statements": lambda args: BANK.get_statements(args["loan_id"]),
}

# --- B5: Fraud Triage (misfire demo) ---
# Declared tool schema is identical to B1 — the model is granted nothing
# extra. The dispatch table below, however, is wider than what's declared:
# it also wires up initiate_wire_transfer. That gap is intentional. B5's
# fallback transcript (fallbacks/b5_misfire_wire_transfer.json) calls
# initiate_wire_transfer anyway, simulating a mis-fired/prompt-injected tool
# call that falls outside the agent's granted contract. The recorder seals
# it like any other event; AgentDef.is_out_of_scope() is what flags it
# after the fact (see runner.py _tool_use_data and app.py's
# policy_violations field).
MISFIRE_FRAUD_TRIAGE_DISPATCH = {
    **FRAUD_TRIAGE_DISPATCH,
    "initiate_wire_transfer": lambda args: BANK.initiate_wire_transfer(
        args["account_id"], args["amount_usd"], args["destination"]
    ),
}
