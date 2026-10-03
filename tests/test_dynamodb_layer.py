"""
tests/test_dynamodb_layer.py
----------------------------
Offline unit tests for app/storage/dynamodb.py serialization helpers
and the appointment_worker logic.

Runs with Python stdlib only — no pytest, no boto3 calls, no network.
All DynamoDB I/O is replaced with in-memory stubs.

Run:
  python tests/test_dynamodb_layer.py
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

# Resolve project root
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# Import the serialisation helpers directly (no boto3 I/O involved)
# ---------------------------------------------------------------------------
from app.storage.dynamodb import _to_ddb, _from_ddb, _item_to_dict  # noqa: E402


class TestDdbRoundTrip(unittest.TestCase):
    """_to_ddb and _from_ddb must round-trip all expected value types."""

    def _rt(self, value):
        return _from_ddb(_to_ddb(value))

    def test_string(self):
        self.assertEqual(self._rt("hello"), "hello")

    def test_none(self):
        self.assertIsNone(self._rt(None))

    def test_bool_true(self):
        self.assertTrue(self._rt(True))

    def test_bool_false(self):
        self.assertFalse(self._rt(False))

    def test_int(self):
        self.assertEqual(self._rt(42), 42)

    def test_float(self):
        self.assertAlmostEqual(self._rt(3.14), 3.14)

    def test_list_of_strings(self):
        self.assertEqual(self._rt(["CBC", "Lipid Profile"]), ["CBC", "Lipid Profile"])

    def test_nested_dict(self):
        d = {"name": "CBC", "available": True, "count": 1}
        self.assertEqual(self._rt(d), d)

    def test_list_of_dicts(self):
        meds = [
            {"name": "Amlodipine", "dosage": "5 mg", "frequency": "once daily"},
            {"name": "Atorvastatin", "dosage": "20 mg", "frequency": None},
        ]
        self.assertEqual(self._rt(meds), meds)

    def test_item_to_dict_strips_pk_sk_as_strings(self):
        raw_item = {
            "PK":  {"S": "PATIENT#raj_sharma"},
            "SK":  {"S": "CARE_STATE"},
            "patient_name": {"S": "Raj Sharma"},
            "requested_tests": {"L": [{"S": "CBC"}, {"S": "Lipid Profile"}]},
            "document_availability": {"M": {
                "CBC":           {"BOOL": True},
                "Lipid Profile": {"BOOL": False},
            }},
        }
        result = _item_to_dict(raw_item)
        self.assertEqual(result["PK"], "PATIENT#raj_sharma")
        self.assertEqual(result["patient_name"], "Raj Sharma")
        self.assertEqual(result["requested_tests"], ["CBC", "Lipid Profile"])
        self.assertTrue(result["document_availability"]["CBC"])
        self.assertFalse(result["document_availability"]["Lipid Profile"])


class TestWorkerLogic(unittest.TestCase):
    """
    Test appointment_worker.run_appointment_check() using in-memory stubs.
    Patches DynamoDB calls and SNS; verifies the preparation-window logic
    and notification deduplication without any network I/O.
    """

    def _make_care_state(self, days_from_now: int) -> dict:
        appt_date = (datetime.now(timezone.utc).date() + timedelta(days=days_from_now))
        # Worker now reads follow_up.confirmed_date + follow_up.status
        return {
            "patient_id":    "raj_sharma",
            "patient_name":  "Raj Sharma",
            "relationship":  "Dad",
            "specialty":     "Cardiology",
            "doctor":        "Dr. Meera Kapoor",
            "source_document": "Cardiology Prescription",
            "requested_tests": ["CBC", "Lipid Profile"],
            "document_availability": {
                "CBC":           True,
                "Lipid Profile": False,
            },
            "follow_up": {
                "source_instruction":          "Review after 4 weeks.",
                "suggested_date":              appt_date.isoformat(),
                "confirmed_date":              appt_date.isoformat(),
                "status":                      "CONFIRMED",
                "requires_human_confirmation": False,
            },
        }

    def _run_with_stubs(self, care_state, already_sent=False, sns_should_raise=False):
        """Run the worker with all external calls stubbed in-memory."""
        import appointment_worker as aw

        sent_notifications = []

        original_get   = aw.get_care_state
        original_check = aw.notification_already_sent
        original_rec   = aw.record_notification_sent
        original_sns   = aw._send_sns

        try:
            aw.get_care_state = lambda pid: care_state
            aw.notification_already_sent = lambda pid, key: already_sent
            aw.record_notification_sent  = lambda pid, key: sent_notifications.append(key)
            if sns_should_raise:
                def _bad_sns(msg, subj):
                    raise RuntimeError("SNS unavailable")
                aw._send_sns = _bad_sns
            else:
                aw._send_sns = lambda msg, subj: None

            result = aw.run_appointment_check()
        finally:
            aw.get_care_state             = original_get
            aw.notification_already_sent  = original_check
            aw.record_notification_sent   = original_rec
            aw._send_sns                  = original_sns

        return result, sent_notifications

    def test_within_window_sends_notification(self):
        state = self._make_care_state(days_from_now=4)
        result, sent = self._run_with_stubs(state, already_sent=False)
        self.assertTrue(result["within_prep_window"])
        self.assertTrue(result["notification_sent"])
        self.assertIn("CBC", result["ready_items"])
        self.assertIn("Lipid Profile", result["missing_items"])
        self.assertEqual(len(sent), 1)  # deduplication marker recorded

    def test_outside_window_skips(self):
        state = self._make_care_state(days_from_now=10)
        result, sent = self._run_with_stubs(state)
        self.assertFalse(result["within_prep_window"])
        self.assertFalse(result["notification_sent"])
        self.assertIsNotNone(result["skipped_reason"])
        self.assertEqual(len(sent), 0)

    def test_deduplication_skips_resend(self):
        state = self._make_care_state(days_from_now=4)
        result, sent = self._run_with_stubs(state, already_sent=True)
        self.assertTrue(result["within_prep_window"])
        self.assertFalse(result["notification_sent"])
        self.assertIn("already sent", result["skipped_reason"])
        self.assertEqual(len(sent), 0)

    def test_no_care_state_returns_gracefully(self):
        import appointment_worker as aw
        original = aw.get_care_state
        try:
            aw.get_care_state = lambda pid: None
            result = aw.run_appointment_check()
        finally:
            aw.get_care_state = original
        self.assertFalse(result["notification_sent"])
        self.assertIn("No care state", result["skipped_reason"])

    def test_past_appointment_skips(self):
        state = self._make_care_state(days_from_now=-1)
        result, _ = self._run_with_stubs(state)
        self.assertFalse(result["notification_sent"])
        self.assertIn("passed", result["skipped_reason"])


class TestUpdateExtractionFields(unittest.TestCase):
    """
    Regression test: update_extraction_fields must never overwrite
    appointment or document_availability.

    Stubs _client() so no DynamoDB connection is needed.
    Captures the UpdateItem call and verifies only extraction-owned
    attributes appear in the expression.
    """

    def test_appointment_and_availability_not_in_update_expression(self):
        """
        Root-cause regression:
        Previously save_care_state used put_item (full replace), which
        deleted appointment and document_availability.
        update_extraction_fields must use UpdateItem and must NOT include
        those fields in its SET expression.
        """
        import app.storage.dynamodb as ddb_module

        captured = {}

        class FakeClient:
            def update_item(self, **kwargs):
                captured.update(kwargs)

        original_client = ddb_module._client
        original_table  = os.environ.get("CARE_STATE_TABLE")
        try:
            ddb_module._client = lambda: FakeClient()
            os.environ["CARE_STATE_TABLE"] = "family-care-state"

            ddb_module.update_extraction_fields("raj_sharma", {
                "patient_id":      "raj_sharma",
                "patient_name":    "Raj Sharma",
                "source_document": "prescription",
                "doctor":          "Dr. Meera Kapoor",
                "specialty":       "Cardiology",
                "document_date":   "26 August 2026",
                "medicines":       [{"name": "Amlodipine", "dosage": "5 mg", "frequency": "once daily"}],
                "requested_tests": ["CBC", "Lipid Profile"],
                "follow_up":       "Review after 4 weeks.",
            })
        finally:
            ddb_module._client = original_client
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        # update_item must have been called (not put_item)
        self.assertIn("UpdateExpression", captured,
                      "update_item was not called — put_item would destroy appointment/availability")

        expr = captured["UpdateExpression"]
        names = captured.get("ExpressionAttributeNames", {})

        # The expression attribute names must not map to appointment or
        # document_availability — those are not extraction-owned fields.
        mapped_attributes = set(names.values())
        self.assertNotIn("appointment",           mapped_attributes,
                         "appointment must not appear in extraction UpdateItem")
        self.assertNotIn("document_availability", mapped_attributes,
                         "document_availability must not appear in extraction UpdateItem")

        # Extraction fields must be present
        self.assertIn("doctor",    mapped_attributes)
        self.assertIn("specialty", mapped_attributes)
        self.assertIn("medicines", mapped_attributes)

    def test_update_uses_update_item_not_put_item(self):
        """Calling update_extraction_fields must not invoke put_item at all."""
        import app.storage.dynamodb as ddb_module

        put_called = []

        class FakeClient:
            def update_item(self, **kwargs):
                pass
            def put_item(self, **kwargs):
                put_called.append(kwargs)

        original_client = ddb_module._client
        original_table  = os.environ.get("CARE_STATE_TABLE")
        try:
            ddb_module._client = lambda: FakeClient()
            os.environ["CARE_STATE_TABLE"] = "family-care-state"
            ddb_module.update_extraction_fields("raj_sharma", {"doctor": "Dr. X"})
        finally:
            ddb_module._client = original_client
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        self.assertEqual(put_called, [],
                         "put_item must never be called from update_extraction_fields")


if __name__ == "__main__":
    unittest.main(verbosity=2)
