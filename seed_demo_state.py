"""
seed_demo_state.py
------------------
One-time script to seed the CareCircle demo care state into DynamoDB.

This writes the synthetic appointment date and document-availability state
that cannot be inferred from the prescription text. Run this once before
the appointment worker or app_web.py DynamoDB reads are needed.

Usage:
  CARE_STATE_TABLE=family-care-state python seed_demo_state.py

All data is synthetic demo data for the AWS Zero to Shipped hackathon.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.storage.dynamodb import save_care_state  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic demo care state for Raj Sharma
#
# NOTE: The appointment date (6 Oct 2026) is explicit demo state.
# It is NOT derived from the prescription's "Review after 4 weeks" follow-up
# instruction. Inferring a concrete date from that vague instruction would
# violate CareCircle's safety rule of never inventing medical information.
# ---------------------------------------------------------------------------

DEMO_CARE_STATE = {
    "patient_id":   "raj_sharma",
    "patient_name": "Raj Sharma",
    "relationship": "Dad",
    "source_document": "Cardiology Prescription",
    "doctor":       "Dr. Meera Kapoor",
    "specialty":    "Cardiology",
    "document_date": "26 August 2026",
    "medicines": [
        {"name": "Amlodipine",   "dosage": "5 mg",  "frequency": "once daily"},
        {"name": "Atorvastatin", "dosage": "20 mg", "frequency": "once daily at night"},
    ],
    # Explicitly listed in the prescription under "Investigations".
    "requested_tests": ["CBC", "Lipid Profile"],
    "follow_up": "Review after 4 weeks.",
    # Appointment is explicitly set demo state — NOT inferred from follow_up text.
    "appointment": {
        "specialty":   "Cardiology",
        "doctor":      "Dr. Meera Kapoor",
        "date":        "2026-10-06",    # ISO format for date arithmetic
        "date_label":  "6 Oct 2026",
    },
    # Synthetic document availability for the demo scenario.
    # CBC is available; Lipid Profile is missing — the gap CareCircle highlights.
    "document_availability": {
        "CBC":           True,
        "Lipid Profile": False,
    },
}


def main():
    if not os.environ.get("CARE_STATE_TABLE", "").strip():
        print("ERROR: CARE_STATE_TABLE environment variable is not set.")
        sys.exit(1)

    print(f"Seeding demo care state for patient: {DEMO_CARE_STATE['patient_name']}")
    save_care_state("raj_sharma", DEMO_CARE_STATE)
    print("Done. DynamoDB record written: PK=PATIENT#raj_sharma  SK=CARE_STATE")


if __name__ == "__main__":
    main()
