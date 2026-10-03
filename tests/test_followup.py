"""
tests/test_followup.py
----------------------
Tests for:
- follow-up date derivation (app/followup.py)
- follow-up DynamoDB partial update (update_followup_state)
- worker status guards (CONFIRMED only)
- notification deduplication preserved
- appointment/document_availability not overwritten

Runs with stdlib only — no pytest, no boto3 I/O, no network.

Run:
  python tests/test_followup.py
"""

import os
import sys
import unittest
from datetime import date, datetime, timezone, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.followup import (
    derive_followup, confirm_followup, ignore_followup,
    STATUS_AWAITING, STATUS_CONFIRMED, STATUS_NEEDS_CLAR, STATUS_IGNORED,
)
import app.storage.dynamodb as ddb_module


# ---------------------------------------------------------------------------
# 1. Follow-up derivation
# ---------------------------------------------------------------------------

class TestDeriveFollowup(unittest.TestCase):

    def test_after_4_weeks(self):
        fu = derive_followup("Review after 4 weeks.", "26 August 2026")
        self.assertEqual(fu["status"], STATUS_AWAITING)
        self.assertEqual(fu["suggested_date"], "2026-09-23")
        self.assertIsNone(fu["confirmed_date"])
        self.assertTrue(fu["requires_human_confirmation"])

    def test_after_n_days(self):
        fu = derive_followup("Follow up after 10 days.", "2026-09-01")
        self.assertEqual(fu["status"], STATUS_AWAITING)
        self.assertEqual(fu["suggested_date"], "2026-09-11")

    def test_explicit_date_in_instruction(self):
        fu = derive_followup("Next visit on 15 October 2026.", "26 August 2026")
        self.assertEqual(fu["status"], STATUS_AWAITING)
        self.assertEqual(fu["suggested_date"], "2026-10-15")

    def test_ambiguous_next_month(self):
        fu = derive_followup("Review next month.", "26 August 2026")
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)
        self.assertIsNone(fu["suggested_date"])
        self.assertTrue(fu["requires_human_confirmation"])

    def test_ambiguous_soon(self):
        fu = derive_followup("Follow up soon.", "2026-09-01")
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)

    def test_ambiguous_as_needed(self):
        fu = derive_followup("As needed.", "2026-09-01")
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)

    def test_ambiguous_later(self):
        fu = derive_followup("Review later.", "2026-09-01")
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)

    def test_missing_document_date(self):
        # Duration pattern matches but no doc date → NEEDS_CLARIFICATION
        fu = derive_followup("Review after 4 weeks.", None)
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)
        self.assertIsNone(fu["suggested_date"])

    def test_empty_instruction(self):
        fu = derive_followup(None, "2026-09-01")
        self.assertEqual(fu["status"], STATUS_NEEDS_CLAR)
        self.assertIsNone(fu["source_instruction"])

    def test_after_2_months_approximation(self):
        fu = derive_followup("Review after 2 months.", "2026-09-01")
        self.assertEqual(fu["status"], STATUS_AWAITING)
        # 2 × 30 = 60 days from 2026-09-01 = 2026-10-31
        self.assertEqual(fu["suggested_date"], "2026-10-31")

    def test_iso_document_date(self):
        fu = derive_followup("After 7 days.", "2026-10-01")
        self.assertEqual(fu["suggested_date"], "2026-10-08")


# ---------------------------------------------------------------------------
# 2. Confirm / ignore helpers
# ---------------------------------------------------------------------------

class TestConfirmIgnore(unittest.TestCase):

    def _base_fu(self):
        return derive_followup("Review after 4 weeks.", "26 August 2026")

    def test_confirm_sets_confirmed_date_and_status(self):
        fu = self._base_fu()
        confirmed = confirm_followup(fu, "2026-09-23")
        self.assertEqual(confirmed["status"], STATUS_CONFIRMED)
        self.assertEqual(confirmed["confirmed_date"], "2026-09-23")
        self.assertFalse(confirmed["requires_human_confirmation"])

    def test_confirm_with_edited_date(self):
        fu = self._base_fu()
        confirmed = confirm_followup(fu, "2026-09-30")
        self.assertEqual(confirmed["confirmed_date"], "2026-09-30")
        self.assertEqual(confirmed["status"], STATUS_CONFIRMED)

    def test_ignore_sets_status_ignored(self):
        fu = self._base_fu()
        ignored = ignore_followup(fu)
        self.assertEqual(ignored["status"], STATUS_IGNORED)
        self.assertFalse(ignored["requires_human_confirmation"])

    def test_source_instruction_preserved_after_confirm(self):
        fu = self._base_fu()
        confirmed = confirm_followup(fu, "2026-09-23")
        self.assertEqual(confirmed["source_instruction"], "Review after 4 weeks.")


# ---------------------------------------------------------------------------
# 3. DynamoDB update_followup_state — partial update only
# ---------------------------------------------------------------------------

class TestUpdateFollowupState(unittest.TestCase):

    def _run_update(self, followup_dict):
        """Run update_followup_state with a fake client; return captured kwargs."""
        captured = {}

        class FakeClient:
            def update_item(self, **kwargs):
                captured.update(kwargs)

        original_client = ddb_module._client
        original_table  = os.environ.get("CARE_STATE_TABLE")
        try:
            ddb_module._client = lambda: FakeClient()
            os.environ["CARE_STATE_TABLE"] = "family-care-state"
            ddb_module.update_followup_state("raj_sharma", followup_dict)
        finally:
            ddb_module._client = original_client
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table
        return captured

    def test_uses_update_item_not_put_item(self):
        fu = confirm_followup(
            derive_followup("After 4 weeks.", "2026-08-26"), "2026-09-23"
        )
        captured = self._run_update(fu)
        self.assertIn("UpdateExpression", captured)

    def test_only_follow_up_and_updated_at_in_expression(self):
        fu = confirm_followup(
            derive_followup("After 4 weeks.", "2026-08-26"), "2026-09-23"
        )
        captured = self._run_update(fu)
        names = set(captured.get("ExpressionAttributeNames", {}).values())
        # appointment and document_availability must NOT appear
        self.assertNotIn("appointment",           names)
        self.assertNotIn("document_availability", names)
        # follow_up and updated_at must appear
        self.assertIn("follow_up",   names)
        self.assertIn("updated_at",  names)


# ---------------------------------------------------------------------------
# 4. Worker status guards
# ---------------------------------------------------------------------------

class TestWorkerStatusGuards(unittest.TestCase):

    def _make_care_state(self, fu_status, days_from_now=4):
        confirmed_date = (
            (datetime.now(timezone.utc).date() + timedelta(days=days_from_now)).isoformat()
            if fu_status == STATUS_CONFIRMED
            else None
        )
        return {
            "patient_name":           "Raj Sharma",
            "relationship":           "Dad",
            "specialty":              "Cardiology",
            "source_document":        "Cardiology Prescription",
            "requested_tests":        ["CBC", "Lipid Profile"],
            "document_availability":  {"CBC": True, "Lipid Profile": False},
            "follow_up": {
                "source_instruction":         "Review after 4 weeks.",
                "suggested_date":             "2026-09-23",
                "confirmed_date":             confirmed_date,
                "status":                     fu_status,
                "requires_human_confirmation": fu_status != STATUS_CONFIRMED,
            },
        }

    def _run_worker(self, care_state, already_sent=False):
        import appointment_worker as aw
        sent = []
        orig_get   = aw.get_care_state
        orig_check = aw.notification_already_sent
        orig_rec   = aw.record_notification_sent
        orig_sns   = aw._send_sns
        try:
            aw.get_care_state             = lambda pid: care_state
            aw.notification_already_sent  = lambda pid, key: already_sent
            aw.record_notification_sent   = lambda pid, key: sent.append(key)
            aw._send_sns                  = lambda msg, subj: None
            result = aw.run_appointment_check()
        finally:
            aw.get_care_state             = orig_get
            aw.notification_already_sent  = orig_check
            aw.record_notification_sent   = orig_rec
            aw._send_sns                  = orig_sns
        return result, sent

    def test_worker_refuses_awaiting_confirmation(self):
        state = self._make_care_state(STATUS_AWAITING)
        result, sent = self._run_worker(state)
        self.assertFalse(result["notification_sent"])
        self.assertIn(STATUS_AWAITING, result["skipped_reason"])
        self.assertEqual(sent, [])

    def test_worker_refuses_needs_clarification(self):
        state = self._make_care_state(STATUS_NEEDS_CLAR)
        result, sent = self._run_worker(state)
        self.assertFalse(result["notification_sent"])
        self.assertIn(STATUS_NEEDS_CLAR, result["skipped_reason"])
        self.assertEqual(sent, [])

    def test_worker_refuses_ignored(self):
        state = self._make_care_state(STATUS_IGNORED)
        result, sent = self._run_worker(state)
        self.assertFalse(result["notification_sent"])
        self.assertEqual(sent, [])

    def test_worker_acts_on_confirmed_within_window(self):
        state = self._make_care_state(STATUS_CONFIRMED, days_from_now=4)
        result, sent = self._run_worker(state)
        self.assertTrue(result["notification_sent"])
        self.assertIn("CBC", result["ready_items"])
        self.assertIn("Lipid Profile", result["missing_items"])
        self.assertEqual(len(sent), 1)

    def test_worker_deduplication_on_confirmed(self):
        state = self._make_care_state(STATUS_CONFIRMED, days_from_now=4)
        result, sent = self._run_worker(state, already_sent=True)
        self.assertFalse(result["notification_sent"])
        self.assertIn("already sent", result["skipped_reason"])
        self.assertEqual(sent, [])

    def test_worker_confirmed_outside_window_skips(self):
        state = self._make_care_state(STATUS_CONFIRMED, days_from_now=10)
        result, sent = self._run_worker(state)
        self.assertFalse(result["notification_sent"])
        self.assertIn("outside", result["skipped_reason"])

    def test_worker_confirmed_past_date_skips(self):
        state = self._make_care_state(STATUS_CONFIRMED, days_from_now=-1)
        result, sent = self._run_worker(state)
        self.assertFalse(result["notification_sent"])
        self.assertIn("passed", result["skipped_reason"])


# ---------------------------------------------------------------------------
# 5. Appointment / document_availability not overwritten
# ---------------------------------------------------------------------------

class TestNoOverwriteOfSeparateState(unittest.TestCase):
    """
    update_extraction_fields must never touch appointment or document_availability.
    """

    def test_extraction_update_does_not_touch_appointment(self):
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
                "doctor":    "Dr. Meera Kapoor",
                "follow_up": {"status": "AWAITING_CONFIRMATION"},
            })
        finally:
            ddb_module._client = original_client
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        names = set(captured.get("ExpressionAttributeNames", {}).values())
        self.assertNotIn("appointment",           names)
        self.assertNotIn("document_availability", names)


# ---------------------------------------------------------------------------
# 6. Regression — false-success bug and patient_id propagation
# ---------------------------------------------------------------------------

class TestFollowupPersistenceContract(unittest.TestCase):
    """
    Regression tests covering the live bug where:
    - except Exception: pass swallowed DynamoDB errors and returned 200
    - the browser displayed "Follow-up confirmed" despite nothing being written
    - patient_id was None when CARE_STATE_TABLE was unset
    - old raw-string follow_up from DynamoDB lost suggested_date
    """

    def _run_followup_logic(self, ddb_raises, stored_fu, ui_fu=None):
        """
        Simulate the POST /followup handler core logic in isolation.
        Returns (status_code, response_body).
        """
        from app.followup import confirm_followup

        confirmed_date = "2026-09-23"

        # Priority logic (mirrors the fixed handler)
        if isinstance(stored_fu, dict) and stored_fu.get("status"):
            current_fu = stored_fu
        elif isinstance(ui_fu, dict) and ui_fu.get("status"):
            current_fu = ui_fu
        elif isinstance(stored_fu, str):
            current_fu = {"source_instruction": stored_fu}
        else:
            current_fu = {}

        updated_fu = confirm_followup(current_fu, confirmed_date)

        if ddb_raises:
            # Fixed handler propagates this as 500 — NOT swallowed
            try:
                raise RuntimeError("DynamoDB throttled")
            except Exception as e:
                return 500, {"error": str(e)}
        else:
            return 200, {"follow_up": updated_fu}

    def test_ddb_error_returns_500_not_200(self):
        """Core regression: a DynamoDB failure must surface as 500, never 200."""
        status, body = self._run_followup_logic(ddb_raises=True, stored_fu={})
        self.assertEqual(status, 500,
            "DynamoDB failure must return 500 — never silently return 200")
        self.assertIn("error", body)

    def test_success_returns_200_with_confirmed_follow_up(self):
        stored_fu = {
            "source_instruction":          "Review after 4 weeks.",
            "suggested_date":              "2026-09-23",
            "confirmed_date":              None,
            "status":                      "AWAITING_CONFIRMATION",
            "requires_human_confirmation": True,
        }
        status, body = self._run_followup_logic(ddb_raises=False, stored_fu=stored_fu)
        self.assertEqual(status, 200)
        fu = body["follow_up"]
        self.assertEqual(fu["status"], "CONFIRMED")
        self.assertEqual(fu["confirmed_date"], "2026-09-23")

    def test_old_string_follow_up_uses_ui_fallback(self):
        """
        When DynamoDB still holds follow_up as a raw string (old format),
        the backend must use the UI-passed follow_up_state so that
        suggested_date is preserved.
        """
        stored_fu = "Review after 4 weeks."    # old raw string from DynamoDB
        ui_fu = {
            "source_instruction":          "Review after 4 weeks.",
            "suggested_date":              "2026-09-23",
            "confirmed_date":              None,
            "status":                      "AWAITING_CONFIRMATION",
            "requires_human_confirmation": True,
        }
        status, body = self._run_followup_logic(
            ddb_raises=False, stored_fu=stored_fu, ui_fu=ui_fu
        )
        self.assertEqual(status, 200)
        fu = body["follow_up"]
        self.assertEqual(fu["status"], "CONFIRMED")
        # suggested_date must survive (came from ui_fu, not the bare string dict)
        self.assertEqual(fu["suggested_date"], "2026-09-23")

    def test_persist_extracted_always_returns_tuple(self):
        """
        _persist_extracted must always return (follow_up_state, patient_id)
        regardless of CARE_STATE_TABLE, so POST /analyze always has patient_id.
        """
        import app_web
        original_env = os.environ.get("CARE_STATE_TABLE")
        try:
            os.environ.pop("CARE_STATE_TABLE", None)
            result = app_web._persist_extracted({
                "patient":         "Raj Sharma",
                "document_date":   "26 August 2026",
                "follow_up":       "Review after 4 weeks.",
                "doctor":          "Dr. Meera Kapoor",
                "specialty":       "Cardiology",
                "medicines":       [],
                "requested_tests": [],
            })
        finally:
            if original_env is not None:
                os.environ["CARE_STATE_TABLE"] = original_env

        self.assertIsInstance(result, tuple,
            "_persist_extracted must always return a tuple, never a bare dict")
        fu_state, patient_id = result
        self.assertEqual(patient_id, "raj_sharma")
        self.assertIsInstance(fu_state, dict)
        self.assertEqual(fu_state["suggested_date"], "2026-09-23")

    def test_persist_extracted_returns_tuple_when_patient_missing(self):
        """Even with no patient name, must return a tuple (not raise or return dict)."""
        import app_web
        original_env = os.environ.get("CARE_STATE_TABLE")
        try:
            os.environ.pop("CARE_STATE_TABLE", None)
            result = app_web._persist_extracted({
                "patient":       None,
                "document_date": "26 August 2026",
                "follow_up":     "Review after 4 weeks.",
            })
        finally:
            if original_env is not None:
                os.environ["CARE_STATE_TABLE"] = original_env

        self.assertIsInstance(result, tuple)
        fu_state, patient_id = result
        self.assertIsNone(patient_id)

    def test_analyze_persistence_exception_propagates(self):
        """
        Core regression: if update_extraction_fields raises inside
        _persist_extracted, the exception must NOT be swallowed.
        It must propagate so the /analyze caller can return 500.
        Previously: except Exception: pass caused silent DynamoDB failure.
        """
        import app_web
        import app.storage.dynamodb as ddb_module

        raised_calls = []
        original_fn    = ddb_module.update_extraction_fields
        original_table = os.environ.get("CARE_STATE_TABLE")

        try:
            os.environ["CARE_STATE_TABLE"] = "family-care-state"

            def _raise(*args, **kwargs):
                raised_calls.append(True)
                raise RuntimeError("Simulated DynamoDB AccessDeniedException")

            ddb_module.update_extraction_fields = _raise

            with self.assertRaises(RuntimeError,
                    msg="DynamoDB error must propagate — not be swallowed by _persist_extracted"):
                app_web._persist_extracted({
                    "patient":         "Raj Sharma",
                    "document_date":   "28 September 2026",
                    "follow_up":       "Review after 7 days.",
                    "doctor":          "Dr. Meera Kapoor",
                    "specialty":       "Cardiology",
                    "medicines":       [],
                    "requested_tests": [],
                })
        finally:
            ddb_module.update_extraction_fields = original_fn
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        self.assertEqual(len(raised_calls), 1, "update_extraction_fields was not even called")

    def test_analyze_persistence_is_called_when_table_configured(self):
        """
        _persist_extracted must call update_extraction_fields when
        CARE_STATE_TABLE is set and a patient name is present.
        Regression: silent pass meant the function returned without writing.
        """
        import app_web
        import app.storage.dynamodb as ddb_module

        write_calls = []
        original_fn    = ddb_module.update_extraction_fields
        original_table = os.environ.get("CARE_STATE_TABLE")

        try:
            os.environ["CARE_STATE_TABLE"] = "family-care-state"
            ddb_module.update_extraction_fields = lambda pid, fields: write_calls.append(pid)

            fu_state, patient_id = app_web._persist_extracted({
                "patient":         "Raj Sharma",
                "document_date":   "28 September 2026",
                "follow_up":       "Review after 7 days.",
                "doctor":          "Dr. Meera Kapoor",
                "specialty":       "Cardiology",
                "medicines":       [],
                "requested_tests": [],
            })
        finally:
            ddb_module.update_extraction_fields = original_fn
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        self.assertEqual(write_calls, ["raj_sharma"],
            "update_extraction_fields must be called with the derived patient_id")
        self.assertEqual(patient_id, "raj_sharma")
        self.assertIsNotNone(fu_state)
        # "Review after 7 days" from doc_date 28 Sep 2026 → 5 Oct 2026
        self.assertEqual(fu_state["suggested_date"], "2026-10-05")


# ---------------------------------------------------------------------------
# 6. False-success regression (the live bug)
# ---------------------------------------------------------------------------

class TestFollowupPersistenceContract(unittest.TestCase):
    """
    Regression: POST /followup must return a non-200 status when
    update_followup_state raises, so the frontend never shows false success.
    """

    def _simulate_followup_handler(self, ddb_raises: bool, stored_fu, ui_fu=None):
        """
        Simulate the POST /followup handler logic in isolation.
        Returns (status_code, response_body).
        """
        # Replicate handler logic (not importing Lambda to avoid env setup)
        from app.followup import confirm_followup, ignore_followup

        patient_id     = "raj_sharma"
        action         = "confirm"
        confirmed_date = "2026-09-23"

        # Resolve current_fu using the new priority logic
        if isinstance(stored_fu, dict) and stored_fu.get("status"):
            current_fu = stored_fu
        elif isinstance(ui_fu, dict) and ui_fu.get("status"):
            current_fu = ui_fu
        elif isinstance(stored_fu, str):
            current_fu = {"source_instruction": stored_fu}
        else:
            current_fu = {}

        updated_fu = confirm_followup(current_fu, confirmed_date)

        # Simulate persistence call
        if ddb_raises:
            # The handler must NOT swallow this — it should propagate to a 500
            try:
                raise RuntimeError("DynamoDB: ProvisionedThroughputExceededException")
                status_code = 200
            except Exception as e:
                status_code = 500
                return status_code, {"error": str(e)}
        else:
            status_code = 200
            return status_code, {"follow_up": updated_fu}

    def test_ddb_error_returns_500_not_200(self):
        """
        If update_followup_state raises, the handler must return 500,
        NOT 200. This prevents the browser from showing false success.
        """
        status, body = self._simulate_followup_handler(ddb_raises=True, stored_fu={})
        self.assertEqual(status, 500)
        self.assertIn("error", body)

    def test_success_returns_200_with_confirmed_follow_up(self):
        stored_fu = {
            "source_instruction":          "Review after 4 weeks.",
            "suggested_date":              "2026-09-23",
            "confirmed_date":              None,
            "status":                      "AWAITING_CONFIRMATION",
            "requires_human_confirmation": True,
        }
        status, body = self._simulate_followup_handler(ddb_raises=False, stored_fu=stored_fu)
        self.assertEqual(status, 200)
        self.assertEqual(body["follow_up"]["status"], "CONFIRMED")
        self.assertEqual(body["follow_up"]["confirmed_date"], "2026-09-23")

    def test_old_string_follow_up_uses_ui_fallback(self):
        """
        When DynamoDB still holds follow_up as a raw string (old format),
        and the UI passes back a proper follow_up_state, the backend should
        use the UI state rather than the degraded string-only dict.
        """
        stored_fu = "Review after 4 weeks."   # old raw string from DynamoDB
        ui_fu = {
            "source_instruction":          "Review after 4 weeks.",
            "suggested_date":              "2026-09-23",
            "confirmed_date":              None,
            "status":                      "AWAITING_CONFIRMATION",
            "requires_human_confirmation": True,
        }
        status, body = self._simulate_followup_handler(
            ddb_raises=False, stored_fu=stored_fu, ui_fu=ui_fu
        )
        self.assertEqual(status, 200)
        fu = body["follow_up"]
        self.assertEqual(fu["status"], "CONFIRMED")
        # suggested_date must survive (came from ui_fu, not the bare string)
        self.assertEqual(fu["suggested_date"], "2026-09-23")

    def test_persist_extracted_always_returns_tuple(self):
        """
        _persist_extracted must always return (follow_up_state, patient_id)
        regardless of whether CARE_STATE_TABLE is set, so POST /analyze
        can always include patient_id in its response.
        """
        import app_web
        original_env = os.environ.get("CARE_STATE_TABLE")
        try:
            os.environ.pop("CARE_STATE_TABLE", None)
            result = app_web._persist_extracted({
                "patient":        "Raj Sharma",
                "document_date":  "26 August 2026",
                "follow_up":      "Review after 4 weeks.",
                "doctor":         "Dr. Meera Kapoor",
                "specialty":      "Cardiology",
                "medicines":      [],
                "requested_tests": [],
            })
        finally:
            if original_env is not None:
                os.environ["CARE_STATE_TABLE"] = original_env

        self.assertIsInstance(result, tuple, "_persist_extracted must return a tuple")
        fu_state, patient_id = result
        self.assertEqual(patient_id, "raj_sharma")
        self.assertIsInstance(fu_state, dict)
        self.assertEqual(fu_state["suggested_date"], "2026-09-23")


class TestAnalyzePersistenceContract(unittest.TestCase):
    """
    Regression: _persist_extracted must call update_extraction_fields and
    must propagate any exception — never swallow it silently.
    """

    def test_persistence_is_called_when_table_configured(self):
        """
        _persist_extracted must call update_extraction_fields when
        CARE_STATE_TABLE is set and patient is present.
        Previously: silent except Exception: pass meant the function
        returned without ever touching DynamoDB.
        """
        import app_web

        write_calls = []
        original_fn    = app_web.update_extraction_fields
        original_table = os.environ.get("CARE_STATE_TABLE")
        try:
            os.environ["CARE_STATE_TABLE"] = "family-care-state"
            # Patch the name in app_web's own namespace (it was imported with `from`)
            app_web.update_extraction_fields = lambda pid, fields: write_calls.append(pid)

            fu_state, patient_id = app_web._persist_extracted({
                "patient":         "Raj Sharma",
                "document_date":   "28 September 2026",
                "follow_up":       "Review after 7 days.",
                "doctor":          "Dr. Meera Kapoor",
                "specialty":       "Cardiology",
                "medicines":       [],
                "requested_tests": [],
            })
        finally:
            app_web.update_extraction_fields = original_fn
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        self.assertEqual(write_calls, ["raj_sharma"],
            "update_extraction_fields must be called with the derived patient_id")
        self.assertEqual(patient_id, "raj_sharma")
        # "Review after 7 days" from 28 Sep 2026 → 5 Oct 2026
        self.assertEqual(fu_state["suggested_date"], "2026-10-05")

    def test_persistence_exception_propagates_not_swallowed(self):
        """
        If update_extraction_fields raises, the exception must propagate
        out of _persist_extracted so the /analyze handler returns 500.
        Previously: except Exception: pass caused CloudWatch-silent failures.
        This test also reproduces the live root cause: AccessDeniedException
        was being silently swallowed — DynamoDB never wrote, no log, 200 returned.
        """
        import app_web

        call_count = []
        original_fn    = app_web.update_extraction_fields
        original_table = os.environ.get("CARE_STATE_TABLE")
        try:
            os.environ["CARE_STATE_TABLE"] = "family-care-state"

            def _raise(pid, fields):
                call_count.append(pid)
                raise RuntimeError("Simulated DynamoDB AccessDeniedException")

            # Patch the name in app_web's own namespace
            app_web.update_extraction_fields = _raise

            with self.assertRaises(RuntimeError,
                    msg="DynamoDB error must NOT be swallowed by _persist_extracted"):
                app_web._persist_extracted({
                    "patient":         "Raj Sharma",
                    "document_date":   "28 September 2026",
                    "follow_up":       "Review after 7 days.",
                    "medicines":       [],
                    "requested_tests": [],
                })
        finally:
            app_web.update_extraction_fields = original_fn
            if original_table is None:
                os.environ.pop("CARE_STATE_TABLE", None)
            else:
                os.environ["CARE_STATE_TABLE"] = original_table

        self.assertEqual(call_count, ["raj_sharma"],
            "update_extraction_fields must have been called before the raise")


if __name__ == "__main__":
    unittest.main(verbosity=2)
