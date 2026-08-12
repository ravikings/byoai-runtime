"""Synthetic seed data for the agent showcase mocks.

All names/amounts are made up. No real customers, patients, or institutions.
"""

from __future__ import annotations

TRANSACTIONS = {
    "txn_8841": {
        "id": "txn_8841",
        "customer_id": "cust_2201",
        "amount_usd": 842.17,
        "merchant": "Northwind Electronics",
        "mcc": "5732",
        "city": "Austin",
        "country": "US",
        "card_present": False,
        "ts": "2026-08-11T22:14:03Z",
    }
}

CUSTOMER_HISTORY = {
    "cust_2201": {
        "customer_id": "cust_2201",
        "name": "Jordan Ellery",
        "home_city": "Austin",
        "home_country": "US",
        "avg_txn_usd": 61.40,
        "recent_txns": [
            {"id": "txn_8830", "amount_usd": 42.10, "city": "Austin", "country": "US", "ts": "2026-08-10T13:02:00Z"},
            {"id": "txn_8835", "amount_usd": 18.75, "city": "Austin", "country": "US", "ts": "2026-08-11T09:41:00Z"},
        ],
    }
}

GEO_VELOCITY = {
    "txn_8841": {
        "prior_txn_city": "Austin",
        "prior_txn_country": "US",
        "prior_txn_ts": "2026-08-11T09:41:00Z",
        "current_city": "Austin",
        "current_country": "US",
        "distance_km": 0.0,
        "minutes_since_prior": 793,
        "impossible_travel": False,
    }
}

# --- B2: KYC / Onboarding -------------------------------------------------

ONBOARDING_APPLICATIONS = {
    "app_5510": {
        "id": "app_5510",
        "applicant_name": "Priya Kestrel",
        "dob": "1991-04-02",
        "documents": [
            {"type": "passport", "name_on_doc": "Priya Kestrel", "number": "X1928734", "expiry": "2029-01-15"},
            {"type": "proof_of_address", "name_on_doc": "Priya Kestrel", "issued": "2026-06-01"},
        ],
        "declared_country": "US",
    }
}

SANCTIONS_SCREEN_RESULTS = {
    "Priya Kestrel": {"matches": [], "watchlists_checked": ["OFAC-SDN", "UN-Consolidated", "EU-Sanctions"]},
}

# --- B3: Dispute Resolution ------------------------------------------------

DISPUTES = {
    "disp_3301": {
        "id": "disp_3301",
        "customer_id": "cust_2201",
        "txn_id": "txn_7710",
        "amount_usd": 129.99,
        "merchant": "Summit Outdoor Gear",
        "reason": "item_not_received",
        "filed_ts": "2026-08-05T10:00:00Z",
    }
}

DISPUTE_EVIDENCE = {
    "disp_3301": {
        "shipment_status": "delivered",
        "delivery_confirmation": False,
        "merchant_response": "Carrier marked delivered but no signature captured; merchant offered reship, customer declined.",
        "prior_disputes_by_customer": 0,
    }
}

# --- B4: Loan Pre-Qualification --------------------------------------------

LOAN_APPLICATIONS = {
    "loan_9042": {
        "id": "loan_9042",
        "applicant_name": "Marcus Devane",
        "requested_amount_usd": 25000,
        "statement_files": ["stmt_2026_06.pdf", "stmt_2026_07.pdf", "stmt_2026_08.pdf"],
    }
}

LOAN_STATEMENTS = {
    "loan_9042": {
        "monthly_income_usd": 6100.0,
        "monthly_debt_payments_usd": 1450.0,
        "months_covered": 3,
        "overdrafts": 0,
    }
}

LOAN_POLICY = {
    "max_dti": 0.43,
    "min_monthly_income_usd": 3000.0,
}
