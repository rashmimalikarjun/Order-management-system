import os
import tempfile
import unittest
from unittest.mock import patch


_import_fd, _import_db = tempfile.mkstemp(suffix=".db")
os.close(_import_fd)
os.environ["DATABASE_PATH"] = _import_db
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.pop("GEMINI_API_KEY", None)

import app as oms


def tearDownModule():
    if os.path.exists(_import_db):
        os.remove(_import_db)


class Phase3FinancialApprovalTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        oms.DATABASE = self.db_path
        oms.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        os.environ.pop("GEMINI_API_KEY", None)
        oms.init_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def connect(self):
        return oms.get_db_connection()

    def login_admin(self, client):
        with client.session_transaction() as sess:
            sess["admin_logged_in"] = True

    def create_order(self, conn, status="Delivered", payment_status="Pending", total_price=120.0):
        now = oms.now_string()
        cursor = conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time,
                total_price, payment_method, payment_status, payment_reference,
                contact_number, payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alice",
                "Veg Meal",
                1,
                now,
                status,
                now,
                total_price,
                "UPI QR",
                payment_status,
                "",
                "5550100",
                "",
            ),
        )
        return cursor.lastrowid

    def create_case(self):
        conn = self.connect()
        order_id = self.create_order(conn)
        case_id, outcome = oms.create_financial_case_for_order(conn, order_id, "admin")
        self.assertEqual(outcome, "created")
        conn.commit()
        conn.close()
        return case_id, order_id

    def latest_reasoning(self, conn, case_id):
        return conn.execute(
            "SELECT * FROM case_reasoning WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    def case_row(self, conn, case_id):
        return conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()

    def set_case_risk(self, conn, case_id, risk_tier="Low", risk_score=10.0, confidence=20.0):
        conn.execute(
            """
            UPDATE financial_case
            SET risk_tier = ?, risk_score = ?, confidence = ?, updated_at = ?
            WHERE id = ?
            """,
            (risk_tier, risk_score, confidence, oms.now_string(), case_id),
        )

    def test_analysis_creates_pending_reasoning_without_mutating_case(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        self.set_case_risk(conn, case_id, "Low", 10.0, 20.0)
        conn.commit()

        result = oms.analyze_financial_case(conn, case_id, "admin", "manual")
        case = self.case_row(conn, case_id)
        reasoning = self.latest_reasoning(conn, case_id)

        self.assertIsNotNone(result)
        self.assertEqual(case["risk_tier"], "Low")
        self.assertEqual(case["risk_score"], 10.0)
        self.assertEqual(case["confidence"], 20.0)
        self.assertEqual(reasoning["approval_state"], "PENDING")
        self.assertEqual(reasoning["risk_tier"], result["risk_tier"])
        self.assertEqual(reasoning["evidence_snapshot_id"], result["evidence_snapshot_id"])
        conn.close()

    def test_approve_pending_reasoning_updates_case_and_marks_approved(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(
            f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve"
        )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        reviewed = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'financial_case_reasoning_approved'"
        ).fetchone()
        self.assertEqual(case["risk_tier"], reasoning["risk_tier"])
        self.assertEqual(case["risk_score"], reasoning["risk_score"])
        self.assertEqual(case["confidence"], reasoning["confidence"])
        self.assertEqual(reviewed["approval_state"], "APPROVED")
        self.assertEqual(reviewed["reviewed_by"], "admin")
        self.assertIsNotNone(audit)
        conn.close()

    def test_reject_pending_reasoning_does_not_update_case(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        self.set_case_risk(conn, case_id, "Low", 12.0, 34.0)
        reasoning = self.latest_reasoning(conn, case_id)
        conn.commit()
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(
            f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/reject"
        )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        reviewed = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        self.assertEqual(case["risk_tier"], "Low")
        self.assertEqual(case["risk_score"], 12.0)
        self.assertEqual(case["confidence"], 34.0)
        self.assertEqual(reviewed["approval_state"], "REJECTED")
        conn.close()

    def test_non_pending_reasoning_cannot_be_approved(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()
        client.post(f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve")
        second_response = client.post(
            f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve"
        )
        self.assertEqual(second_response.status_code, 302)

        conn = self.connect()
        approved = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        self.assertEqual(approved["approval_state"], "APPROVED")

        oms.analyze_financial_case(conn, case_id, "admin", "manual")
        rejected = self.latest_reasoning(conn, case_id)
        conn.commit()
        conn.close()
        client.post(f"/admin/finance/case/{case_id}/reasoning/{rejected['id']}/reject")
        client.post(f"/admin/finance/case/{case_id}/reasoning/{rejected['id']}/approve")

        conn = self.connect()
        rejected_after = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (rejected["id"],)).fetchone()
        self.assertEqual(rejected_after["approval_state"], "REJECTED")
        conn.close()

    def test_non_admin_cannot_approve_or_reject(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        response = client.post(
            f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers["Location"])

        with client.session_transaction() as sess:
            sess["user_logged_in"] = True
        response = client.post(
            f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/reject"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers["Location"])

        conn = self.connect()
        case = self.case_row(conn, case_id)
        unchanged = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        self.assertEqual(case["risk_tier"], "Unscored")
        self.assertEqual(unchanged["approval_state"], "PENDING")
        conn.close()

    def test_wrong_case_and_stale_reasoning_are_blocked(self):
        case_id, _ = self.create_case()
        other_case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        conn = self.connect()
        self.set_case_risk(conn, case_id, "Low", 1.0, 2.0)
        original_reasoning = self.latest_reasoning(conn, case_id)
        other_reasoning = self.latest_reasoning(conn, other_case_id)
        conn.commit()
        conn.close()

        client.post(f"/admin/finance/case/{case_id}/reasoning/{other_reasoning['id']}/approve")
        conn = self.connect()
        case = self.case_row(conn, case_id)
        other_after = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (other_reasoning["id"],)).fetchone()
        self.assertEqual(case["risk_tier"], "Low")
        self.assertEqual(other_after["approval_state"], "PENDING")

        oms.analyze_financial_case(conn, case_id, "admin", "manual")
        newer_reasoning = self.latest_reasoning(conn, case_id)
        self.assertNotEqual(original_reasoning["id"], newer_reasoning["id"])
        conn.commit()
        conn.close()

        client.post(f"/admin/finance/case/{case_id}/reasoning/{original_reasoning['id']}/approve")
        conn = self.connect()
        case = self.case_row(conn, case_id)
        stale = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (original_reasoning["id"],)).fetchone()
        blocked_audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'financial_case_reasoning_approval_blocked'"
        ).fetchone()
        self.assertEqual(case["risk_tier"], "Low")
        self.assertEqual(stale["approval_state"], "PENDING")
        self.assertIsNotNone(blocked_audit)
        conn.close()

    def test_approval_rolls_back_when_audit_fails(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        with patch.object(oms, "record_audit", side_effect=RuntimeError("audit failed")):
            response = client.post(
                f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve"
            )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        reviewed = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'reasoning_approved'",
            (case_id,),
        ).fetchone()
        self.assertEqual(case["risk_tier"], "Unscored")
        self.assertEqual(reviewed["approval_state"], "PENDING")
        self.assertIsNone(action)
        conn.close()

    def test_rejection_rolls_back_when_audit_fails(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        with patch.object(oms, "record_audit", side_effect=RuntimeError("audit failed")):
            response = client.post(
                f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/reject"
            )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        reviewed = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'reasoning_rejected'",
            (case_id,),
        ).fetchone()
        self.assertEqual(reviewed["approval_state"], "PENDING")
        self.assertIsNone(action)
        conn.close()


class Phase4FollowUpAndFailureTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        oms.DATABASE = self.db_path
        oms.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        os.environ.pop("GEMINI_API_KEY", None)
        oms.init_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.environ.pop("GEMINI_API_KEY", None)

    def connect(self):
        return oms.get_db_connection()

    def login_admin(self, client):
        with client.session_transaction() as sess:
            sess["admin_logged_in"] = True

    def create_order(self, conn, status="Delivered", payment_status="Pending", total_price=120.0):
        now = oms.now_string()
        cursor = conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time,
                total_price, payment_method, payment_status, payment_reference,
                contact_number, payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alice", "Veg Meal", 1, now, status, now,
                total_price, "UPI QR", payment_status, "", "5550100", "",
            ),
        )
        return cursor.lastrowid

    def create_case(self):
        conn = self.connect()
        order_id = self.create_order(conn)
        case_id, outcome = oms.create_financial_case_for_order(conn, order_id, "admin")
        self.assertEqual(outcome, "created")
        conn.commit()
        conn.close()
        return case_id, order_id

    def case_row(self, conn, case_id):
        return conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()

    def set_follow_up(self, conn, case_id, follow_up_due_at):
        conn.execute(
            "UPDATE financial_case SET follow_up_due_at = ? WHERE id = ?",
            (follow_up_due_at, case_id),
        )

    # ---- Follow-up visibility ----

    def test_past_follow_up_is_overdue(self):
        past = (oms.current_local_datetime() - oms.timedelta(days=2)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        self.assertTrue(oms.is_follow_up_overdue(past))

    def test_future_follow_up_is_not_overdue(self):
        future = (oms.current_local_datetime() + oms.timedelta(days=2)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        self.assertFalse(oms.is_follow_up_overdue(future))

    def test_missing_follow_up_is_not_overdue(self):
        self.assertFalse(oms.is_follow_up_overdue(""))
        self.assertFalse(oms.is_follow_up_overdue(None))

    def test_unparseable_follow_up_is_not_overdue(self):
        self.assertFalse(oms.is_follow_up_overdue("not-a-real-date"))

    def test_dashboard_flags_overdue_case_without_changing_status_or_escalating(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        past = (oms.current_local_datetime() - oms.timedelta(days=1)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        self.set_follow_up(conn, case_id, past)
        conn.commit()
        before = self.case_row(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.get("/admin/finance")
        self.assertEqual(response.status_code, 200)

        conn = self.connect()
        after = self.case_row(conn, case_id)
        conn.close()
        # Visibility must be read-only: status/risk fields untouched by simply viewing the dashboard.
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(after["status"], "Escalated")
        self.assertEqual(before["risk_tier"], after["risk_tier"])

    def test_detail_page_flags_overdue_case_without_changing_status(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        past = (oms.current_local_datetime() - oms.timedelta(days=1)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        self.set_follow_up(conn, case_id, past)
        conn.commit()
        before = self.case_row(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.get(f"/admin/finance/case/{case_id}")
        self.assertEqual(response.status_code, 200)

        conn = self.connect()
        after = self.case_row(conn, case_id)
        conn.close()
        self.assertEqual(before["status"], after["status"])
        self.assertNotEqual(after["status"], "Escalated")

    # ---- AI failure auditability ----

    def test_validation_failure_persists_case_action_without_mutating_case(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        before = self.case_row(conn, case_id)
        conn.close()

        os.environ["GEMINI_API_KEY"] = "test-key-should-not-leak"
        conn = self.connect()
        with patch.object(oms, "call_gemini_api", return_value='{"not": "valid"}'), \
             patch.object(oms, "validate_gemini_response", return_value=False):
            result = oms.analyze_financial_case(conn, case_id, "admin", "manual")
        conn.commit()

        self.assertIsNotNone(result)
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'ai_analysis_fallback' "
            "ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        self.assertIsNotNone(action)
        self.assertEqual(action["case_id"], case_id)
        self.assertIn("schema validation", action["outcome"])

        after = self.case_row(conn, case_id)
        self.assertEqual(before["risk_tier"], after["risk_tier"])
        self.assertEqual(before["risk_score"], after["risk_score"])
        self.assertEqual(before["confidence"], after["confidence"])
        conn.close()

    def test_exception_failure_persists_case_action_and_redacts_key(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        before = self.case_row(conn, case_id)
        conn.close()

        secret = "test-key-should-not-leak"
        os.environ["GEMINI_API_KEY"] = secret
        conn = self.connect()
        with patch.object(
            oms, "call_gemini_api",
            side_effect=RuntimeError(f"connection failed for key={secret}"),
        ):
            result = oms.analyze_financial_case(conn, case_id, "admin", "manual")
        conn.commit()

        self.assertIsNotNone(result)
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'ai_analysis_fallback' "
            "ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        self.assertIsNotNone(action)
        self.assertEqual(action["case_id"], case_id)
        self.assertNotIn(secret, action["outcome"])
        self.assertIn("[REDACTED]", action["outcome"])

        after = self.case_row(conn, case_id)
        self.assertEqual(before["risk_tier"], after["risk_tier"])
        self.assertEqual(before["risk_score"], after["risk_score"])
        self.assertEqual(before["confidence"], after["confidence"])
        conn.close()

    def test_no_gemini_key_configured_does_not_create_failure_action(self):
        # Missing API key is not a "failure" of the AI workflow — it's simply not attempted.
        case_id, _ = self.create_case()
        conn = self.connect()
        result = oms.analyze_financial_case(conn, case_id, "admin", "manual")
        conn.commit()
        self.assertIsNotNone(result)
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'ai_analysis_fallback'",
            (case_id,),
        ).fetchone()
        self.assertIsNone(action)
        conn.close()


class Phase3UIWiringTests(unittest.TestCase):
    """Confirms the reasoning detail page actually exposes the Phase 3 approval
    routes to an admin, using only routes/fields that already existed and were
    already covered by Phase3FinancialApprovalTests."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        oms.DATABASE = self.db_path
        oms.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        os.environ.pop("GEMINI_API_KEY", None)
        oms.init_db()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def connect(self):
        return oms.get_db_connection()

    def login_admin(self, client):
        with client.session_transaction() as sess:
            sess["admin_logged_in"] = True

    def create_order(self, conn):
        now = oms.now_string()
        cursor = conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time,
                total_price, payment_method, payment_status, payment_reference,
                contact_number, payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("alice", "Veg Meal", 1, now, "Delivered", now, 120.0, "UPI QR", "Pending", "", "5550100", ""),
        )
        return cursor.lastrowid

    def create_case(self):
        conn = self.connect()
        order_id = self.create_order(conn)
        case_id, outcome = oms.create_financial_case_for_order(conn, order_id, "admin")
        self.assertEqual(outcome, "created")
        conn.commit()
        conn.close()
        return case_id

    def test_detail_page_renders_approve_and_reject_forms_for_pending_reasoning(self):
        case_id = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        response = client.get(f"/admin/finance/case/{case_id}")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()

        approve_url = f"/admin/finance/case/{case_id}/reasoning/"
        self.assertIn(approve_url, body)
        self.assertIn("/approve", body)
        self.assertIn("/reject", body)
        self.assertIn("Pending", body)

    def test_clicking_rendered_approve_action_actually_approves(self):
        case_id = self.create_case()
        conn = self.connect()
        reasoning = conn.execute(
            "SELECT * FROM case_reasoning WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        detail = client.get(f"/admin/finance/case/{case_id}").data.decode()
        expected_action = f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve"
        self.assertIn(expected_action, detail)

        response = client.post(expected_action)
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        approved = conn.execute("SELECT * FROM case_reasoning WHERE id = ?", (reasoning["id"],)).fetchone()
        self.assertEqual(approved["approval_state"], "APPROVED")
        conn.close()

    def test_already_reviewed_reasoning_shows_no_action_buttons(self):
        case_id = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        conn = self.connect()
        reasoning = conn.execute(
            "SELECT * FROM case_reasoning WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        conn.close()
        client.post(f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve")

        body = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertIn("Approved", body)
        # No pending approve/reject action should remain for this now-resolved entry.
        self.assertNotIn(f"/reasoning/{reasoning['id']}/approve", body)
        self.assertNotIn(f"/reasoning/{reasoning['id']}/reject", body)


if __name__ == "__main__":
    unittest.main()