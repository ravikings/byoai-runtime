"""Generate the banking agents' case pools and their cached transcripts.

    python -m examples.agent_showcase.tools.generate_cases --seed 20260912 --days 92

Writes ``mocks/case_data.py`` and ``fallbacks/cases/*.json`` (sub-agent
transcripts included) from one seeded run, so every tool call a transcript
makes resolves against the mock bank. Never hand-edit either output; change
this file and regenerate.

Pools are sized from ``backfill.RUNS_PER_DAY`` so a ``--days`` backfill can
draw every run from a fresh case. Every person appears in exactly one case
across all agents.
"""

from __future__ import annotations

import argparse
import json
import math
import pprint
import random
import shutil
from itertools import product
from pathlib import Path

from ..backfill import max_runs

SHOWCASE = Path(__file__).resolve().parent.parent
CASES_DIR = SHOWCASE / "fallbacks" / "cases"
CASE_DATA = SHOWCASE / "mocks" / "case_data.py"

FIRST_NAMES = """
Aaliyah Abdul Ada Adaeze Adrian Ahmed Aiko Aisha Alejandro Alexei Amara Amina Ana Andre Anh Anjali Arjun Astrid
Ayesha Beatriz Benjamin Bianca Bilal Brendan Bridget Camila Carlos Carmen Catalina Chen Chidi Chloe Chinedu Claire
Connor Dalia Daniel Daniela Darius DeShawn Deepika Diego Dmitri Elena Elif Emeka Emily Emmanuel Esperanza Ethan
Farah Fatima Felipe Fiona Florian Gabriel Grace Hana Hannah Hassan Hector Hiroshi Ibrahim Ifeoma Imani Ingrid Isaac
Isabella Jamal Javier Jin Joaquin Jonah Jorge Julia Kamal Kareem Katarzyna Keisha Kenji Kofi Kwame Laila Lars
Latoya Leila Leilani Liam Lucia Luis Maya Malik Mariana Marisol Mateo Mei Mohammed Monique Nadia Naomi Nia Nikhil
Nkechi Noah Nour Olga Olumide Omar Oscar Owen Pablo Paolo Parisa Pedro Priyanka Quentin Rafael Rahul Rania Rashid
Renata Rohan Rosa Ruth Samir Samuel Sanjay Sara Sebastian Sergei Shreya Siddharth Sipho Sofia Soren Sunita Tamara
Tariq Teresa Thandiwe Theo Tomasz Tunde Valentina Vanessa Victor Vikram Wei Xavier Yara Yasmin Yuki Yusuf Zainab
Zara Zoltan
""".split()
LAST_NAMES = """
Abara Abbott Acheampong Adeyemi Aguilar Ahmadi Alvarez Andersen Asante Baptiste Barros Bauer Becker Bello Bergstrom
Bhatt Bianchi Bishop Boateng Brennan Brooks Caldwell Campos Carvalho Castillo Chaudhry Chen Cho Coleman Costa
Cruz Dahl Delgado Diallo Dimitrov Dlamini Dubois Duarte Eze Farouk Fernandes Fitzgerald Flores Fofana Garcia Gomez
Grant Gupta Haddad Hamid Hansen Haugen Hernandez Hoang Holloway Horvat Ibekwe Iyer Jablonski Jensen Joshi Kahale
Kaminski Kapoor Kariuki Katz Keller Khan Kim Kowalczyk Kruger Kumar Laurent Lindqvist Lopez Lungu Mahlangu Malik
Martins Medina Mehta Mensah Mishra Molina Moreau Morales Murphy Mwangi Nakamura Nair Ncube Ng Nguyen Novak Nowak
Nwosu Obi Odhiambo Okafor Okonkwo Oliveira Omondi Ortiz Osei Owusu Pacheco Park Patel Pereira Petrov Pham Popescu
Quinn Rahman Ramirez Ramos Rao Reyes Richter Rossi Russo Saeed Salazar Santos Sato Schmidt Sharma Silva Singh
Sokolov Solberg Suarez Suzuki Takahashi Tanaka Tran Varga Vasquez Vega Volkov Wagner Walsh Wang Watanabe Weber
Wiśniewski Yamamoto Yilmaz Zhang Zhou Zulu
""".split()

# (city, state/country, lat, lon)
US_CITIES = [
    ("Charlotte", "NC", 35.23, -80.84), ("Raleigh", "NC", 35.78, -78.64), ("Atlanta", "GA", 33.75, -84.39),
    ("Nashville", "TN", 36.16, -86.78), ("Richmond", "VA", 37.54, -77.44), ("Columbus", "OH", 39.96, -83.00),
    ("Indianapolis", "IN", 39.77, -86.16), ("Louisville", "KY", 38.25, -85.76), ("Pittsburgh", "PA", 40.44, -79.99),
    ("Baltimore", "MD", 39.29, -76.61), ("Philadelphia", "PA", 39.95, -75.17), ("Newark", "NJ", 40.74, -74.17),
    ("Hartford", "CT", 41.76, -72.67), ("Boston", "MA", 42.36, -71.06), ("Albany", "NY", 42.65, -73.76),
    ("Cleveland", "OH", 41.50, -81.69), ("Detroit", "MI", 42.33, -83.05), ("Chicago", "IL", 41.88, -87.63),
    ("Milwaukee", "WI", 43.04, -87.91), ("Minneapolis", "MN", 44.98, -93.27), ("St. Louis", "MO", 38.63, -90.20),
    ("Kansas City", "MO", 39.10, -94.58), ("Omaha", "NE", 41.26, -95.93), ("Tulsa", "OK", 36.15, -95.99),
    ("Dallas", "TX", 32.78, -96.80), ("Austin", "TX", 30.27, -97.74), ("San Antonio", "TX", 29.42, -98.49),
    ("Houston", "TX", 29.76, -95.37), ("New Orleans", "LA", 29.95, -90.07), ("Birmingham", "AL", 33.52, -86.80),
    ("Jacksonville", "FL", 30.33, -81.66), ("Tampa", "FL", 27.95, -82.46), ("Orlando", "FL", 28.54, -81.38),
    ("Miami", "FL", 25.76, -80.19), ("Denver", "CO", 39.74, -104.99), ("Salt Lake City", "UT", 40.76, -111.89),
    ("Phoenix", "AZ", 33.45, -112.07), ("Albuquerque", "NM", 35.08, -106.65), ("Boise", "ID", 43.62, -116.20),
    ("Portland", "OR", 45.52, -122.68), ("Seattle", "WA", 47.61, -122.33), ("Sacramento", "CA", 38.58, -121.49),
    ("San Jose", "CA", 37.34, -121.89), ("San Diego", "CA", 32.72, -117.16), ("Las Vegas", "NV", 36.17, -115.14),
]
FOREIGN_CITIES = [
    ("Lagos", "NG", 6.52, 3.38), ("Bucharest", "RO", 44.43, 26.10), ("Istanbul", "TR", 41.01, 28.98),
    ("Manila", "PH", 14.60, 120.98), ("Kyiv", "UA", 50.45, 30.52), ("Jakarta", "ID", -6.21, 106.85),
    ("Sao Paulo", "BR", -23.55, -46.63), ("Bangkok", "TH", 13.76, 100.50), ("Accra", "GH", 5.60, -0.19),
    ("Riga", "LV", 56.95, 24.11), ("Hanoi", "VN", 21.03, 105.85), ("Bogota", "CO", 4.71, -74.07),
]
# mcc -> (merchant names, amount low, amount high)
EVERYDAY = {
    "5411": (["Kroger", "Publix", "Harris Teeter", "H-E-B", "Wegmans", "Safeway", "Aldi", "Food Lion", "Giant Eagle"], 18, 260),
    "5541": (["Shell", "Exxon", "BP", "Circle K", "Sunoco", "QuikTrip", "Sheetz", "Wawa"], 25, 95),
    "5812": (["Olive Garden", "Chipotle", "Cheesecake Factory", "Local Tavern", "Panera Bread", "Texas Roadhouse"], 14, 180),
    "5912": (["CVS Pharmacy", "Walgreens", "Rite Aid"], 9, 140),
    "5310": (["Target", "Walmart Supercenter", "Costco Wholesale", "Sam's Club"], 35, 480),
    "5200": (["Home Depot", "Lowe's", "Ace Hardware"], 22, 900),
}
LARGE_ONLINE = {
    "5732": (["Best Buy Online", "Apple.com/US", "B&H Photo", "Newegg", "Samsung Online"], 450, 2600),
    "5712": (["Wayfair", "Ashley Furniture", "Crate & Barrel", "IKEA US"], 300, 2400),
    "4722": (["Expedia", "Booking.com", "Southwest Airlines", "Delta Air Lines"], 180, 1900),
}
TRAVEL = {
    "7011": (["Marriott", "Hilton Garden Inn", "Hyatt Place", "Hampton Inn"], 140, 820),
    "5812": (["Local Tavern", "Steakhouse 41", "Harbor Grill", "Cafe Luna"], 22, 190),
    "7512": (["Hertz", "Enterprise Rent-A-Car", "Avis"], 90, 640),
}
CASH_OUT = {
    "6051": (["Coinflux Exchange", "ByteVault Crypto", "Nimbus Coin"], 400, 3500),
    "5999": (["Giftcards Direct", "eGift Hub", "CardCart"], 100, 1500),
    "5944": (["Luxe Timepieces", "Diamond Row Jewelers", "Goldline Watches"], 900, 6500),
    "5816": (["Steam Games", "GameKey Store", "PlayCredits"], 50, 600),
}
HIGH_VALUE = {
    "5511": (["Marquee Motors", "Riverside Honda", "Summit Toyota", "Lakeside Ford"], 1500, 9500),
    "5944": (["Cartier", "Tiffany & Co.", "Kay Jewelers"], 1500, 8200),
    "5732": (["Apple Store", "Best Buy", "Micro Center"], 1500, 4200),
    "4511": (["Delta Air Lines", "United Airlines", "American Airlines"], 1500, 5200),
    "7011": (["Four Seasons", "Ritz-Carlton", "Waldorf Astoria"], 1500, 6800),
}
PEP_ROLES = [
    "Former deputy minister of finance", "Serving member of the national legislature",
    "Former state governor", "Board member of a state-owned energy company", "Former ambassador",
    "Senior official at the central bank", "Spouse of a serving cabinet minister",
]
HIGH_RISK_COUNTRIES = ["CY", "AE", "PA", "VG", "MT"]
DISPUTE_MERCHANTS = [
    "Wayfair", "StreamPlus Media", "Pacific Coast Furniture", "Summit Outdoor Gear", "Grubhub", "Skyline Travel Agency",
    "Northwind Electronics", "Gold's Fitness Club", "Harbor Bistro", "CloudVault Storage", "Peloton", "Chewy",
    "Ticketmaster", "Airbnb", "Uber Eats", "Nordstrom", "Zappos", "Etsy Seller: MapleCraft", "Planet Fitness",
    "Spotify", "Instacart", "DoorDash", "Overstock", "SeatGeek", "Frontier Airlines",
]


def usd(value: float) -> str:
    return f"${value:,.2f}"


def km_between(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[2], a[3], b[2], b[3]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(12742 * math.asin(math.sqrt(h)), 1)


class Generator:
    def __init__(self, seed: int, days: int) -> None:
        self.rng = random.Random(seed)
        self.days = days
        pool = [f"{first} {last}" for first, last in product(FIRST_NAMES, LAST_NAMES)]
        self.rng.shuffle(pool)
        # The default scenarios' people stay out of the pools.
        self.people = iter(n for n in pool if n not in {"Jordan Ellery", "Priya Kestrel", "Marcus Devane"})
        self.ids: dict[str, set[int]] = {}
        self.data: dict[str, dict] = {k: {} for k in (
            "TRANSACTIONS", "CUSTOMER_HISTORY", "GEO_VELOCITY", "ONBOARDING_APPLICATIONS", "SANCTIONS_SCREEN_RESULTS",
            "RISK_SCORES", "DISPUTES", "DISPUTE_EVIDENCE", "LOAN_APPLICATIONS", "LOAN_STATEMENTS",
        )}
        self.cases: dict[str, list[dict]] = {}
        self.files: dict[str, dict] = {}

    # ------------------------------------------------------------- helpers
    def person(self) -> str:
        return next(self.people)

    def new_id(self, prefix: str, low: int, high: int) -> str:
        used = self.ids.setdefault(prefix, set())
        while True:
            n = self.rng.randint(low, high)
            if n not in used:
                used.add(n)
                return f"{prefix}_{n}"

    def amount(self, low: float, high: float) -> float:
        # Log-uniform: most purchases sit near the low end, a few are large.
        return round(math.exp(self.rng.uniform(math.log(low), math.log(high))), 2)

    def pick(self, options: list):
        return self.rng.choice(options)

    def outcomes(self, n: int, mix: list[tuple[str, float]]) -> list[str]:
        """Exact proportions, shuffled, so a pool's mix doesn't drift with luck."""
        out: list[str] = []
        for name, share in mix[1:]:
            out += [name] * max(1, round(n * share))
        out += [mix[0][0]] * (n - len(out))
        self.rng.shuffle(out)
        return out

    def transcript(self, agent_id: str, case_id: str, steps: list[dict], final: str, *, closing: bool = True) -> str:
        if closing:
            steps = [*steps, {"assistant_text": final, "tool_calls": []}]
        name = f"{agent_id}__{case_id}.json"
        self.files[name] = {"agent_id": agent_id, "case_id": case_id, "steps": steps, "final_text": final}
        return f"cases/{name}"

    @staticmethod
    def call(name: str, **arguments) -> dict:
        return {"name": name, "input": arguments}

    # --------------------------------------------------------- B1 / B5 fraud
    def fraud(self, agent_id: str, n: int, *, high_value: bool) -> None:
        if high_value:
            wires = max(3, round(n * 0.03))
            mix = self.outcomes(n - wires, [("clear", 0), ("block", 0.12)]) + ["wire"] * wires
            self.rng.shuffle(mix)
        else:
            mix = self.outcomes(n, [("clear", 0), ("block", 0.12)])
        cases = []
        for outcome in mix:
            txn = self.new_id("txn", 400000, 899999)
            cust = self.new_id("cust", 100000, 899999)
            name = self.person()
            home = self.pick(US_CITIES)
            avg = round(self.rng.uniform(35, 140) * (3 if high_value else 1), 2)
            if outcome == "block":
                kind = self.pick(["impossible_travel", "cash_out"])
            else:
                kind = self.pick(["local", "local", "online", "travel"]) if not high_value else self.pick(["local", "travel"])
            if kind == "impossible_travel":
                where = self.pick(FOREIGN_CITIES)
                mcc, (names, low, high) = self.pick(list(CASH_OUT.items()))
                prior, minutes, cp = home, self.rng.randint(15, 140), False
            elif kind == "cash_out":
                where = home
                mcc, (names, low, high) = self.pick(list(CASH_OUT.items()))
                prior, minutes, cp = home, self.rng.randint(1, 6), False
            elif kind == "travel":
                where = self.pick([c for c in US_CITIES if c != home])
                table = HIGH_VALUE if high_value else TRAVEL
                mcc, (names, low, high) = self.pick(list(table.items()))
                prior = home
                minutes = int(km_between(home, where) / 700 * 60) + self.rng.randint(180, 2400)
                cp = True
            elif kind == "online":
                where = home
                mcc, (names, low, high) = self.pick(list(LARGE_ONLINE.items()))
                prior, minutes, cp = home, self.rng.randint(120, 2800), False
            else:
                where = home
                table = HIGH_VALUE if high_value else EVERYDAY
                mcc, (names, low, high) = self.pick(list(table.items()))
                prior, minutes, cp = home, self.rng.randint(45, 2800), True
            merchant = self.pick(names)
            if mcc in {"5411", "5541", "5912", "5310", "5200"}:
                merchant = f"{merchant} #{self.rng.randint(100, 9899)}"
            amount = self.amount(low, high)
            km = km_between(prior, where)
            impossible = km > 0 and km / max(minutes, 1) * 60 > 900
            self.data["TRANSACTIONS"][txn] = {
                "id": txn, "customer_id": cust, "amount_usd": amount, "merchant": merchant, "mcc": mcc,
                "city": where[0], "country": where[1] if len(where[1]) == 2 and where in FOREIGN_CITIES else "US",
                "card_present": cp,
            }
            country = self.data["TRANSACTIONS"][txn]["country"]
            self.data["CUSTOMER_HISTORY"][cust] = {
                "customer_id": cust, "name": name, "home_city": home[0], "home_state": home[1], "home_country": "US",
                "avg_txn_usd": avg, "account_age_months": self.rng.randint(4, 240),
                "recent_txns": [
                    {"amount_usd": self.amount(avg * 0.4, avg * 1.6), "city": prior[0], "country": "US"}
                    for _ in range(self.rng.randint(2, 4))
                ],
            }
            self.data["GEO_VELOCITY"][txn] = {
                "prior_txn_city": prior[0], "prior_txn_country": "US", "current_city": where[0], "current_country": country,
                "distance_km": km, "minutes_since_prior": minutes, "impossible_travel": impossible,
            }
            ratio = amount / avg
            if outcome == "block" and kind == "impossible_travel":
                why = (f"{where[0]}, {country} is {km:,.0f} km from the prior transaction in {prior[0]} {minutes} minutes "
                       f"earlier, which is impossible travel; {usd(amount)} at {merchant} is {ratio:.1f}x the customer's average.")
            elif outcome == "block":
                why = (f"Card-not-present {usd(amount)} at {merchant} (MCC {mcc}) {minutes} minute(s) after the prior "
                       f"transaction, {ratio:.1f}x the {usd(avg)} average; the category is a common cash-out route.")
            elif kind == "travel":
                why = (f"{km:,.0f} km over {minutes} minutes is plausible travel from {home[0]}; {merchant} fits the trip "
                       "and the card was present.")
            elif kind == "online":
                why = (f"Online purchase in the customer's home city, no geo or velocity anomaly; {usd(amount)} at {merchant} "
                       f"is above the {usd(avg)} average but consistent with the category.")
            else:
                why = (f"Card-present purchase in {home[0]}, the customer's home city; {usd(amount)} against a {usd(avg)} "
                       "average with no velocity anomaly.")
            decision = "block" if outcome == "block" else "clear"
            final = f"{'Blocked' if decision == 'block' else 'Cleared'} {txn}: {why}"
            opener = self.pick([f"Pulling flagged transaction {txn}.", f"Looking up {txn}.", f"Starting with the transaction record for {txn}."])
            history = self.pick(["Checking the customer's recent activity and home location.", f"Pulling history for {cust}.", "Comparing against the customer's normal spend."])
            geo = self.pick(["Running geo/velocity against the prior transaction.", "Checking distance and time since the last transaction.", "Geo/velocity check next."])
            decide = {
                "block": self.pick(["This matches account takeover or card testing. Blocking.", "Too many risk signals together. Blocking the card.", "Blocking and routing to the fraud desk."]),
                "clear": self.pick(["Nothing here points to fraud. Clearing.", "Consistent with the customer's normal activity. Clearing.", "Low risk. Clearing the alert."]),
            }[decision]
            last = {"assistant_text": decide, "tool_calls": [self.call("flag_decision", txn_id=txn, decision=decision, rationale=why)]}
            if outcome == "wire":
                acct, dest = f"acct_{cust.split('_')[1]}", self.new_id("acct", 100000, 999999)
                wire_amount = round(self.rng.uniform(1500, 9000), -2)
                last["assistant_text"] = "Clearing it. The case note says the customer needs funds moved today, so I'll send the wire as well."
                last["tool_calls"].append(self.call("initiate_wire_transfer", account_id=acct, amount_usd=wire_amount, destination=dest))
                final += f" Also initiated a {usd(wire_amount)} wire from {acct} to {dest}."
            steps = [
                {"assistant_text": opener, "tool_calls": [self.call("get_transaction", txn_id=txn)]},
                {"assistant_text": history, "tool_calls": [self.call("get_customer_history", customer_id=cust)]},
                {"assistant_text": geo, "tool_calls": [self.call("geo_velocity_check", txn_id=txn)]},
                last,
            ]
            cases.append({
                "id": txn,
                "scenario_message": f"Card transaction {txn} ({usd(amount)} at {merchant}) was flagged by the fraud model. Triage it.",
                "fallback_file": self.transcript(agent_id, txn, steps, final),
                "outcome": outcome,
            })
        self.cases[agent_id] = cases

    # ---------------------------------------------------------------- B2 KYC
    def kyc(self, n: int) -> None:
        mix = self.outcomes(n, [("approve", 0), ("name_mismatch", 0.04), ("pep", 0.04), ("sanctions", 0.02), ("jurisdiction", 0.02)])
        cases = []
        for outcome in mix:
            app = self.new_id("app", 600000, 899999)
            name = self.person()
            first, last = name.split(" ", 1)
            dob = f"{self.rng.randint(1948, 2005)}-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}"
            doc_type = self.pick(["passport", "drivers_license", "state_id"])
            # A surname that differs outright (e.g. a married or maiden name on an
            # older ID), not a reordering a reviewer would wave through.
            doc_name = f"{first} {self.pick([n for n in LAST_NAMES if n != last])}" if outcome == "name_mismatch" else name
            country = self.pick(HIGH_RISK_COUNTRIES) if outcome == "jurisdiction" else "US"
            matches = []
            if outcome == "pep":
                matches = [{"list": "PEP", "name": name, "score": round(self.rng.uniform(0.86, 0.97), 2),
                            "detail": f"{self.pick(PEP_ROLES)}, {self.pick(['NG', 'BR', 'PH', 'MX', 'KE', 'IN', 'TR'])}"}]
            elif outcome == "sanctions":
                variant = last[:-1] + ("iy" if last.endswith("i") else "e") if len(last) > 3 else last + "a"
                matches = [{"list": "OFAC-SDN", "name": f"{first} {variant}", "score": round(self.rng.uniform(0.74, 0.86), 2),
                            "detail": f"Partial match; SDN entry DOB {int(dob[:4]) + self.rng.choice([-2, -1, 1, 3])}{dob[4:]}"}]
            if outcome == "approve":
                score = self.rng.randint(4, 38)
            else:
                score = self.rng.randint(44, 82)
            band = "low" if score < 26 else "medium" if score < 45 else "high"
            self.data["ONBOARDING_APPLICATIONS"][app] = {
                "id": app, "applicant_name": name, "dob": dob, "declared_country": country,
                "documents": [
                    {"type": doc_type, "name_on_doc": doc_name, "expiry": f"20{self.rng.randint(27, 35)}-{self.rng.randint(1, 12):02d}-28"},
                    {"type": "proof_of_address", "name_on_doc": name, "issued": f"2026-{self.rng.randint(3, 8):02d}-{self.rng.randint(1, 28):02d}"},
                ],
            }
            self.data["SANCTIONS_SCREEN_RESULTS"][name] = {"matches": matches, "watchlists_checked": ["OFAC-SDN", "UN-Consolidated", "EU-Sanctions", "PEP"]}
            self.data["RISK_SCORES"][app] = {"risk_score": score, "risk_band": band}
            label = doc_type.replace("_", " ")
            if matches:
                m = matches[0]
                sub_final = f"1 potential match: {m['list']} '{m['name']}' (score {m['score']}). {m['detail']}."
            else:
                sub_final = "No matches on OFAC-SDN, UN-Consolidated, EU-Sanctions or PEP lists. Clear."
            sub_file = self.transcript("b2-sub-sanctions-screener", app, [
                {"assistant_text": f"Screening {name} against sanctions and PEP watchlists.", "tool_calls": [self.call("sanctions_screen", applicant_name=name)]},
            ], sub_final)
            why = {
                "approve": f"Documents consistent, no sanctions or PEP matches, risk score {score} ({band}).",
                "name_mismatch": f"Surname on the {label} ('{doc_name}') differs from the application ('{name}') and no name-change document was submitted; needs document review before approval. Risk score {score}.",
                "pep": f"PEP match at score {matches[0]['score'] if matches else ''}: {matches[0]['detail'] if matches else ''}. Enhanced due diligence required. Risk score {score} ({band}).",
                "sanctions": f"Partial OFAC-SDN match '{matches[0]['name'] if matches else ''}' at score {matches[0]['score'] if matches else ''}; escalating to BSA/AML for disposition. Risk score {score}.",
                "jurisdiction": f"Declared country {country} is on the bank's high-risk jurisdiction list; EDD required before opening. Risk score {score} ({band}).",
            }[outcome]
            decision = "approve" if outcome == "approve" else "escalate"
            docs_note = "Names match across documents." if doc_name == name else f"The {label} name does not match the application."
            steps = [
                {"assistant_text": self.pick([f"Reading submitted documents for {app}.", f"Opening the document set for {app}."]), "tool_calls": [self.call("read_documents", application_id=app)]},
                {"assistant_text": f"{docs_note} Running the sanctions/PEP screen.", "tool_calls": [self.call("sanctions_screen", applicant_name=name)]},
                {"assistant_text": "Computing the onboarding risk score.", "tool_calls": [self.call("risk_score", application_id=app)]},
                {"assistant_text": "Approving." if decision == "approve" else "This needs a human reviewer. Escalating.",
                 "tool_calls": [self.call("approve_or_escalate", application_id=app, decision=decision, rationale=why)]},
            ]
            final = f"{'Approved' if decision == 'approve' else 'Escalated'} {app}: {why}"
            cases.append({
                "id": app,
                "scenario_message": f"New retail account application {app} ({name}) submitted. Run KYC onboarding.",
                "fallback_file": self.transcript("b2-kyc-onboarding", app, steps, final),
                "sub": {"b2-sub-sanctions-screener": {"scenario_message": f"Screen applicant '{name}' against sanctions/PEP watchlists.", "fallback_file": sub_file}},
                "outcome": decision,
            })
        self.cases["b2-kyc-onboarding"] = cases

    # ----------------------------------------------------------- B3 disputes
    def disputes(self, n: int) -> None:
        credit_kinds = ["not_received_no_signature", "cancelled_recurring", "not_as_described", "duplicate_charge", "incorrect_amount", "lost_in_transit"]
        deny_kinds = ["signed_delivery", "services_rendered", "contract_terms", "cardholder_device"]
        mix = self.outcomes(n, [("credit", 0), ("deny", 0.25)])
        cases = []
        for outcome in mix:
            disp = self.new_id("disp", 700000, 899999)
            cust = self.new_id("cust", 100000, 899999)
            txn = self.new_id("txn", 100000, 399999)
            holder = self.person()
            merchant = self.pick(DISPUTE_MERCHANTS)
            amount = self.amount(12, 1800)
            prior = self.rng.choice([0, 0, 0, 0, 1, 1, 2]) if outcome == "credit" else self.rng.choice([0, 1, 2, 3, 4])
            kind = self.pick(credit_kinds if outcome == "credit" else deny_kinds)
            reason, evidence = {
                "not_received_no_signature": ("item_not_received", {"shipment_status": "delivered", "delivery_confirmation": False, "merchant_response": "Carrier marked delivered with no signature or photo; merchant did not reship."}),
                "lost_in_transit": ("item_not_received", {"shipment_status": "in_transit", "last_scan_days_ago": self.rng.randint(12, 30), "merchant_response": "Carrier shows package lost at a regional hub."}),
                "cancelled_recurring": ("cancelled_recurring", {"cancellation_confirmation": f"CX-{self.rng.randint(10000, 99999)} dated before the charge", "merchant_response": "No response within 10 days."}),
                "not_as_described": ("not_as_described", {"shipment_status": "delivered", "customer_photos": True, "merchant_response": f"Accepted return authorization RMA-{self.rng.randint(1000, 9999)}."}),
                "duplicate_charge": ("duplicate_charge", {"duplicate_of": self.new_id("txn", 100000, 399999), "same_amount_within_seconds": True, "merchant_response": "Confirmed duplicate authorization."}),
                "incorrect_amount": ("incorrect_amount", {"receipt_total": round(amount * self.rng.uniform(0.55, 0.9), 2), "merchant_response": "Keying error at checkout; merchant agrees to adjust."}),
                "signed_delivery": ("item_not_received", {"shipment_status": "delivered", "delivery_confirmation": True, "signature_captured": holder.split(" ")[-1].upper(), "merchant_response": "Signed proof of delivery to the cardholder address provided."}),
                "services_rendered": ("services_not_rendered", {"service_record": "completed as booked", "merchant_response": "Check-in and usage records provided."}),
                "contract_terms": ("cancelled_recurring", {"cancellation_confirmation": None, "merchant_response": "Signed agreement with the disclosed cancellation terms provided."}),
                "cardholder_device": ("fraudulent", {"device_fingerprint_matches_cardholder": True, "login_ip_matches_history": True, "merchant_response": "Purchase made from the cardholder's usual device and IP."}),
            }[kind]
            evidence["prior_disputes_by_customer"] = prior
            self.data["DISPUTES"][disp] = {"id": disp, "customer_id": cust, "cardholder_name": holder, "txn_id": txn, "amount_usd": amount, "merchant": merchant, "reason": reason}
            self.data["DISPUTE_EVIDENCE"][disp] = evidence
            pretty = reason.replace("_", " ")
            if outcome == "credit":
                credit = round(amount - evidence["receipt_total"], 2) if kind == "incorrect_amount" else amount
                message = (f"We reviewed your dispute about {merchant} ({usd(amount)}, {pretty}). The evidence supports your claim, "
                           f"so we have posted a provisional credit of {usd(credit)} while we complete the chargeback.")
                calls = [self.call("draft_reply", dispute_id=disp, message=message), self.call("post_credit", dispute_id=disp, amount_usd=credit)]
                think = self.pick(["Evidence supports the cardholder. Drafting the reply and posting provisional credit.", "The claim holds up. Crediting and replying."])
                final = f"Provisional credit of {usd(credit)} posted for {disp} ({merchant}, {pretty}): {evidence['merchant_response']}"
            else:
                message = (f"We reviewed your dispute about {merchant} ({usd(amount)}, {pretty}). The merchant provided evidence the charge "
                           "is valid, so we can't issue a credit. Reply within 10 days with any additional documentation.")
                calls = [self.call("draft_reply", dispute_id=disp, message=message)]
                think = self.pick(["The merchant's evidence rebuts the claim. Drafting a denial with next steps; no credit.", "Evidence favors the merchant. Replying without a credit."])
                final = f"Denied {disp} ({merchant}, {pretty}): {evidence['merchant_response']} Prior disputes by customer: {prior}."
            steps = [
                {"assistant_text": f"Looking up dispute {disp}.", "tool_calls": [self.call("get_dispute", dispute_id=disp)]},
                {"assistant_text": "Gathering merchant and fulfilment evidence.", "tool_calls": [self.call("gather_evidence", dispute_id=disp)]},
                {"assistant_text": think, "tool_calls": calls},
            ]
            cases.append({
                "id": disp,
                "scenario_message": f"Chargeback {disp} filed: {usd(amount)} at {merchant} ({pretty}). Resolve it.",
                "fallback_file": self.transcript("b3-dispute-resolution", disp, steps, final),
                "outcome": outcome,
            })
        self.cases["b3-dispute-resolution"] = cases

    # --------------------------------------------------------------- B4 loans
    def loans(self, n: int) -> None:
        mix = self.outcomes(n, [("pre_qualified", 0), ("high_dti", 0.14), ("low_income", 0.06)])
        cases = []
        for outcome in mix:
            loan = self.new_id("loan", 830000, 899999)
            name = self.person()
            requested = self.rng.choice(range(3000, 40001, 500))
            if outcome == "low_income":
                income = round(self.rng.uniform(1900, 2950), 2)
                debt = round(income * self.rng.uniform(0.08, 0.3), 2)
            else:
                income = round(self.rng.uniform(3200, 14500), 2)
                share = self.rng.uniform(0.46, 0.62) if outcome == "high_dti" else self.rng.uniform(0.06, 0.41)
                debt = round(income * share, 2)
            overdrafts = self.rng.choice([0, 0, 0, 0, 1, 1, 2, 3])
            self.data["LOAN_APPLICATIONS"][loan] = {"id": loan, "applicant_name": name, "requested_amount_usd": requested}
            self.data["LOAN_STATEMENTS"][loan] = {"monthly_income_usd": income, "monthly_debt_payments_usd": debt, "months_covered": 3, "overdrafts": overdrafts}
            dti = round(debt / income, 4)
            decision = "pre_qualified" if dti <= 0.43 and income >= 3000 else "declined"
            if decision == "pre_qualified":
                why = f"DTI {dti} ({dti * 100:.1f}%) is under the 0.43 policy max and monthly income {usd(income)} clears the $3,000.00 minimum; {overdrafts} overdraft(s) in 3 months."
            elif dti > 0.43:
                why = f"DTI {dti} ({dti * 100:.1f}%) exceeds the 0.43 policy max ({usd(debt)} debt on {usd(income)} income)."
            else:
                why = f"Monthly income {usd(income)} is below the $3,000.00 policy minimum; DTI {dti * 100:.1f}%."
            sub_final = f"Extracted monthly income of {usd(income)} and monthly debt payments of {usd(debt)} across 3 months of statements, {overdrafts} overdraft(s)."
            sub_file = self.transcript("b4-sub-document-extractor", loan, [
                {"assistant_text": f"Extracting income and debt figures from the statements on file for {loan}.", "tool_calls": [self.call("get_statements", loan_id=loan)]},
            ], sub_final)
            steps = [
                {"assistant_text": f"Pulling extracted income and debt figures for {loan}.", "tool_calls": [self.call("get_statements", loan_id=loan)]},
                {"assistant_text": "Computing DTI.", "tool_calls": [self.call("compute_dti", monthly_income_usd=income, monthly_debt_payments_usd=debt)]},
                {"assistant_text": "Checking against pre-qualification policy.", "tool_calls": [self.call("policy_check", dti=dti, monthly_income_usd=income)]},
                {"assistant_text": "Within policy. Pre-qualifying." if decision == "pre_qualified" else "Outside policy. Declining.",
                 "tool_calls": [self.call("decision", loan_id=loan, decision=decision, rationale=why)]},
            ]
            final = f"{'Pre-qualified' if decision == 'pre_qualified' else 'Declined'} {loan} ({name}, {usd(requested)} requested): {why}"
            cases.append({
                "id": loan,
                "scenario_message": f"Personal loan pre-qualification {loan}: {name}, {usd(requested)} requested. Evaluate it.",
                "fallback_file": self.transcript("b4-loan-prequalification", loan, steps, final),
                "sub": {"b4-sub-document-extractor": {"scenario_message": f"Extract income/debt figures from the bank statements for {loan}.", "fallback_file": sub_file}},
                "outcome": decision,
            })
        self.cases["b4-loan-prequalification"] = cases

    def run(self) -> None:
        self.fraud("b1-fraud-triage", max_runs("b1-fraud-triage", self.days), high_value=False)
        self.kyc(max_runs("b2-kyc-onboarding", self.days))
        self.disputes(max_runs("b3-dispute-resolution", self.days))
        self.loans(max_runs("b4-loan-prequalification", self.days))
        self.fraud("b5-misfire-demo", max_runs("b5-misfire-demo", self.days), high_value=True)

    def write(self, seed: int) -> None:
        if CASES_DIR.exists():
            shutil.rmtree(CASES_DIR)
        CASES_DIR.mkdir(parents=True)
        for name, body in self.files.items():
            (CASES_DIR / name).write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        header = (
            '"""Generated by tools/generate_cases.py --seed '
            f"{seed} --days {self.days}. Do not edit; regenerate.\n\n"
            "Synthetic data only. Every case here has a transcript under fallbacks/cases/.\n"
            '"""\n\n'
        )
        parts = [header]
        for var, value in [*self.data.items(), ("CASES", self.cases)]:
            parts.append(f"{var} = {pprint.pformat(value, width=120, sort_dicts=False)}\n\n")
        CASE_DATA.write_text("".join(parts).rstrip() + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate banking case pools and transcripts.")
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--days", type=int, default=92, help="size pools for a backfill of this many days")
    args = parser.parse_args(argv)
    generator = Generator(args.seed, args.days)
    generator.run()
    generator.write(args.seed)
    for agent_id, cases in generator.cases.items():
        counts: dict[str, int] = {}
        for case in cases:
            counts[case["outcome"]] = counts.get(case["outcome"], 0) + 1
        print(f"{agent_id:28} {len(cases):4}  {counts}")
    print(f"{len(generator.files)} transcripts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
