"""
appointment_worker.py
---------------------
CareCircle proactive follow-up preparation worker.

Triggered by EventBridge Scheduler (daily at 8:00 AM IST).

Key change: the worker now acts on confirmed_date from a CONFIRMED follow-up,
not on a separately managed appointment date.

The worker ONLY sends an SNS notification when:
  follow_up.status == "CONFIRMED"

All other statuses (AWAITING_CONFIRMATION, NEEDS_CLARIFICATION, IGNORED,
missing) are skipped with a clear reason — no SNS, no side effects.

Notification deduplication is preserved.

All medical data is synthetic demo data.

Environment variables required:
  SNS_TOPIC_ARN      — ARN of the SNS topic for caregiver notifications.
  CARE_STATE_TABLE   — DynamoDB table name (family-care-state).
"""

import json
import os
import sys
import boto3
from datetime import date, datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.storage.dynamodb import (  # noqa: E402
    get_care_state,
    list_family_members,
    notification_already_sent,
    record_notification_sent,
)
from app.followup import STATUS_CONFIRMED  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PREP_WINDOW_DAYS = 7
PATIENT_ID       = "raj_sharma"

# ---------------------------------------------------------------------------
# SNS helpers
# ---------------------------------------------------------------------------

_SNS_TEMPLATE = """\
CareCircle Follow-up Reminder

{relationship}'s {specialty} follow-up is coming up on {date_label}.

Ready:
{ready_lines}

Still needed:
{missing_lines}

Source: {source_document}

CareCircle organizes documented care instructions and does not provide \
medical advice."""


def _build_sns_message(
    relationship: str,
    specialty: str,
    date_label: str,
    source_document: str,
    ready: list,
    missing: list,
) -> str:
    ready_lines   = "\n".join(f"- {i}" for i in ready)   or "- (none)"
    missing_lines = "\n".join(f"- {i}" for i in missing) or "- (none)"
    return _SNS_TEMPLATE.format(
        relationship=relationship,
        specialty=specialty,
        date_label=date_label,
        source_document=source_document,
        ready_lines=ready_lines,
        missing_lines=missing_lines,
    )


def _send_sns(message: str, subject: str) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    if not topic_arn:
        raise EnvironmentError("SNS_TOPIC_ARN environment variable is not set.")
    client = boto3.client("sns", region_name="us-east-1")
    client.publish(TopicArn=topic_arn, Subject=subject, Message=message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days_until(date_str: str) -> int:
    return (date.fromisoformat(date_str) - datetime.now(timezone.utc).date()).days


def _followup_key(specialty: str, confirmed_date: str) -> str:
    """Stable deduplication key for a follow-up notification."""
    return f"followup-{specialty.lower().replace(' ', '-')}-{confirmed_date}"


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------

def run_appointment_check(patient_id=PATIENT_ID, member=None) -> dict:
    """
    1. Load care state from DynamoDB.
    2. Guard: follow_up.status must be CONFIRMED.
    3. Guard: within preparation window.
    4. Guard: notification not already sent.
    5. Compare requested tests with document_availability.
    6. Send SNS + record deduplication.
    """
    care_state = get_care_state(patient_id)
    if not care_state:
        return {
            "patient": patient_id,
            "notification_sent": False,
            "skipped_reason": f"No care state found for '{patient_id}'.",
        }

    patient_name          = (member or {}).get("name") or care_state.get("patient_name", patient_id)
    relationship          = (member or {}).get("relationship") or care_state.get("relationship", "Family member")
    specialty             = care_state.get("specialty", "")
    source_document       = care_state.get("source_document", "Prescription")
    requested_tests       = care_state.get("requested_tests") or []
    document_availability = care_state.get("document_availability") or {}
    follow_up             = care_state.get("follow_up") or {}

    if not isinstance(follow_up, dict):
        follow_up = {}

    result = {
        "patient":           patient_name,
        "relationship":      relationship,
        "follow_up_status":  follow_up.get("status"),
        "within_prep_window": False,
        "ready_items":        [],
        "missing_items":      [],
        "notification_sent":  False,
        "skipped_reason":     None,
    }

    # --- Guard 1: status must be CONFIRMED ---
    status = follow_up.get("status")
    if status != STATUS_CONFIRMED:
        result["skipped_reason"] = (
            f"Follow-up status is '{status}' — only CONFIRMED follow-ups "
            "trigger automated reminders. Caregiver confirmation required."
        )
        return result

    confirmed_date = follow_up.get("confirmed_date")
    if not confirmed_date:
        result["skipped_reason"] = "Follow-up is CONFIRMED but confirmed_date is missing."
        return result

    # --- Guard 2: preparation window ---
    days_away = _days_until(confirmed_date)
    result["days_away"] = days_away

    if days_away < 0:
        result["skipped_reason"] = f"Follow-up date has passed ({abs(days_away)} days ago)."
        return result

    if days_away > PREP_WINDOW_DAYS:
        result["skipped_reason"] = (
            f"Follow-up is {days_away} days away, "
            f"outside the {PREP_WINDOW_DAYS}-day preparation window."
        )
        return result

    result["within_prep_window"] = True

    # --- Guard 3: deduplication ---
    notif_key = _followup_key(specialty, confirmed_date)
    if notification_already_sent(patient_id, notif_key):
        result["skipped_reason"] = (
            f"Preparation notification already sent for key '{notif_key}'."
        )
        return result

    # --- Gather ready/missing ---
    for test in requested_tests:
        if document_availability.get(test, False):
            result["ready_items"].append(test)
        else:
            result["missing_items"].append(test)

    # --- Send SNS ---
    date_label = datetime.strptime(confirmed_date, "%Y-%m-%d").strftime("%-d %b %Y")
    message = _build_sns_message(
        f"{patient_name} ({relationship})", specialty, date_label, source_document,
        result["ready_items"], result["missing_items"],
    )
    subject = f"CareCircle: {patient_name} follow-up on {date_label}"[:100]
    _send_sns(message, subject)
    record_notification_sent(patient_id, notif_key)
    result["notification_sent"] = True

    return result


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    try:
        results = []
        for member in list_family_members():
            try:
                result = run_appointment_check(member["patient_id"], member)
                result["patient_id"] = member["patient_id"]
                results.append(result)
            except Exception as exc:
                # Keep checking other family members even if one check fails.
                print("Family worker member check failed: " + type(exc).__name__)
                results.append({"patient_id": member["patient_id"], "error": "Member check failed",
                                "notification_sent": False})
        return {"statusCode": 207 if any("error" in r for r in results) else 200,
                "body": {"members_checked": len(results), "results": results}}
    except EnvironmentError as e:
        return {"statusCode": 500, "body": {"error": "Configuration error", "detail": str(e)}}
    except Exception as e:
        return {"statusCode": 500, "body": {"error": "Unexpected error", "detail": str(e)}}
