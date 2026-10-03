"""
app/storage/dynamodb.py
-----------------------
Minimal DynamoDB persistence layer for CareCircle.

Table: family-care-state  (name read from CARE_STATE_TABLE env var)
  PK (String) — partition key
  SK (String) — sort key

Current record types:
  PK=PATIENT#<patient_id>  SK=CARE_STATE   — patient care state
  PK=PATIENT#<patient_id>  SK=NOTIF#<appt_key> — notification deduplication

All medical data stored here is synthetic demo data.
No diagnosis, treatment recommendations, or lab interpretation is performed.
"""

import os
import json
import boto3
from datetime import datetime, timezone

_REGION = "us-east-1"


def _table_name() -> str:
    name = os.environ.get("CARE_STATE_TABLE", "").strip()
    if not name:
        raise EnvironmentError(
            "CARE_STATE_TABLE environment variable is not set."
        )
    return name


def _client():
    return boto3.client("dynamodb", region_name=_REGION)


# ---------------------------------------------------------------------------
# Serialisation helpers (plain-dict ↔ DynamoDB AttributeValue)
# We use the low-level client (not resource) to keep the dependency surface
# minimal and compatible with all Lambda Python runtimes.
# ---------------------------------------------------------------------------

def _to_ddb(value):
    """Recursively convert a Python value to a DynamoDB AttributeValue dict."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, list):
        return {"L": [_to_ddb(v) for v in value]}
    if isinstance(value, dict):
        return {"M": {k: _to_ddb(v) for k, v in value.items()}}
    # Fallback: coerce to string
    return {"S": str(value)}


def _from_ddb(attr):
    """Recursively convert a DynamoDB AttributeValue dict to a Python value."""
    if "NULL" in attr:
        return None
    if "BOOL" in attr:
        return attr["BOOL"]
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        v = attr["N"]
        return int(v) if "." not in v else float(v)
    if "L" in attr:
        return [_from_ddb(v) for v in attr["L"]]
    if "M" in attr:
        return {k: _from_ddb(v) for k, v in attr["M"].items()}
    return None


def _item_to_dict(item: dict) -> dict:
    return {k: _from_ddb(v) for k, v in item.items()}


# ---------------------------------------------------------------------------
# Patient care state
# ---------------------------------------------------------------------------

def save_care_state(patient_id: str, care_state: dict) -> None:
    """
    Persist or overwrite the care state for a patient.

    care_state keys (all optional except patient_id):
      patient_id, patient_name, relationship, source_document, doctor,
      specialty, medicines, requested_tests, follow_up, appointment,
      document_availability

    Appointment and document_availability are SEPARATE demo state that must
    be seeded explicitly — they are NOT inferred from prescription text.
    """
    client = _client()
    pk = f"PATIENT#{patient_id}"
    sk = "CARE_STATE"

    item = {
        "PK": {"S": pk},
        "SK": {"S": sk},
        "updated_at": {"S": datetime.now(timezone.utc).isoformat()},
    }
    for key, value in care_state.items():
        item[key] = _to_ddb(value)

    client.put_item(TableName=_table_name(), Item=item)


def update_extraction_fields(patient_id: str, extraction: dict) -> None:
    """
    Partial update: write ONLY prescription-extraction-owned fields.

    Uses DynamoDB UpdateItem so attributes not listed here
    (appointment, document_availability, notification state, etc.)
    are left completely untouched.

    Owned fields:
      patient_id, patient_name, source_document, document_date,
      doctor, specialty, medicines, requested_tests, follow_up, updated_at
    """
    client = _client()

    # Build SET expression and ExpressionAttributeValues for only the
    # extraction-owned attributes that have a non-None value.
    EXTRACTION_FIELDS = [
        "patient_id", "patient_name", "source_document", "source_storage",
        "document_date", "doctor", "specialty",
        "medicines", "requested_tests", "follow_up",
    ]

    set_parts = ["#ua = :updated_at"]
    expr_names  = {"#ua": "updated_at"}
    expr_values = {":updated_at": {"S": datetime.now(timezone.utc).isoformat()}}

    for field in EXTRACTION_FIELDS:
        if field in extraction:
            placeholder_name  = f"#{field}"
            placeholder_value = f":{field}"
            set_parts.append(f"{placeholder_name} = {placeholder_value}")
            expr_names[placeholder_name]  = field
            expr_values[placeholder_value] = _to_ddb(extraction[field])

    if not set_parts:
        return  # nothing to write

    client.update_item(
        TableName=_table_name(),
        Key={
            "PK": {"S": f"PATIENT#{patient_id}"},
            "SK": {"S": "CARE_STATE"},
        },
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def update_followup_state(patient_id: str, followup: dict) -> None:
    """
    Partial update: write ONLY the follow_up structured field.

    Uses UpdateItem so appointment, document_availability, and all other
    attributes are left untouched.

    followup dict shape:
      source_instruction, suggested_date, confirmed_date,
      status, requires_human_confirmation
    """
    client = _client()
    client.update_item(
        TableName=_table_name(),
        Key={
            "PK": {"S": f"PATIENT#{patient_id}"},
            "SK": {"S": "CARE_STATE"},
        },
        UpdateExpression="SET #fu = :fu, #ua = :ua",
        ExpressionAttributeNames={"#fu": "follow_up", "#ua": "updated_at"},
        ExpressionAttributeValues={
            ":fu": _to_ddb(followup),
            ":ua": {"S": datetime.now(timezone.utc).isoformat()},
        },
    )


def get_care_state(patient_id: str) -> dict | None:
    """
    Retrieve the care state for a patient.
    Returns None if not found.
    """
    client = _client()
    response = client.get_item(
        TableName=_table_name(),
        ConsistentRead=True,
        Key={
            "PK": {"S": f"PATIENT#{patient_id}"},
            "SK": {"S": "CARE_STATE"},
        },
    )
    item = response.get("Item")
    if not item:
        return None
    return _item_to_dict(item)


# ---------------------------------------------------------------------------
# Notification deduplication
# ---------------------------------------------------------------------------

def notification_already_sent(patient_id: str, appt_key: str) -> bool:
    """
    Check whether a preparation notification has already been sent for
    this patient + appointment key (e.g. "Cardiology-2026-10-06").
    """
    client = _client()
    response = client.get_item(
        TableName=_table_name(),
        Key={
            "PK": {"S": f"PATIENT#{patient_id}"},
            "SK": {"S": f"NOTIF#{appt_key}"},
        },
    )
    return "Item" in response


def record_notification_sent(patient_id: str, appt_key: str) -> None:
    """
    Record that a preparation notification was sent, to prevent duplicates
    on subsequent daily scheduler runs.
    """
    client = _client()
    client.put_item(
        TableName=_table_name(),
        Item={
            "PK": {"S": f"PATIENT#{patient_id}"},
            "SK": {"S": f"NOTIF#{appt_key}"},
            "sent_at": {"S": datetime.now(timezone.utc).isoformat()},
        },
    )

# One demo household registry; patient care and notification records stay separate.
_DEFAULT_MEMBERS = {
    "raj_sharma": {"patient_id": "raj_sharma", "name": "Raj Sharma", "relationship": "Dad"},
    "nisha_mehta": {"patient_id": "nisha_mehta", "name": "Nisha Mehta", "relationship": "Mom"},
}


def list_family_members():
    response = _client().get_item(
        TableName=_table_name(), Key={"PK": {"S": "FAMILY#demo"}, "SK": {"S": "MEMBERS"}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    members = _item_to_dict(item).get("members", {}) if item else _DEFAULT_MEMBERS
    return list(members.values())


def save_family_member(member):
    client = _client()
    key = {"PK": {"S": "FAMILY#demo"}, "SK": {"S": "MEMBERS"}}
    # Initialize once, then update only this member to avoid losing concurrent edits.
    client.update_item(TableName=_table_name(), Key=key,
        UpdateExpression="SET #m = if_not_exists(#m, :defaults)",
        ExpressionAttributeNames={"#m": "members"},
        ExpressionAttributeValues={":defaults": _to_ddb(_DEFAULT_MEMBERS)})
    client.update_item(TableName=_table_name(), Key=key,
        UpdateExpression="SET #m.#id = :member",
        ExpressionAttributeNames={"#m": "members", "#id": member["patient_id"]},
        ExpressionAttributeValues={":member": _to_ddb(member)})
