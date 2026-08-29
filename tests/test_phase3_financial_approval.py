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


class Phase5ActionsAndFollowUpsTests(unittest.TestCase):
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

    def latest_reasoning(self, conn, case_id):
        return conn.execute(
            "SELECT * FROM case_reasoning WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    def approve_latest_reasoning(self, client, case_id):
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()
        response = client.post(f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/approve")
        self.assertEqual(response.status_code, 302)

    def pending_action(self, conn, case_id):
        return conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'controlled_action_queued' "
            "ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    # ---- Existing case_action rows keep their pre-Phase-5 meaning ----

    def test_pre_existing_case_actions_default_to_completed_status(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        created_action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'case_created'",
            (case_id,),
        ).fetchone()
        self.assertIsNotNone(created_action)
        self.assertEqual(created_action["status"], "completed")
        conn.close()

    # ---- Approval queues a controlled action ----

    def test_approving_reasoning_queues_pending_controlled_action(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        self.assertIsNotNone(action)
        self.assertEqual(action["status"], "pending")
        conn.close()

    def test_rejecting_reasoning_does_not_queue_controlled_action(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        reasoning = self.latest_reasoning(conn, case_id)
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        client.post(f"/admin/finance/case/{case_id}/reasoning/{reasoning['id']}/reject")

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        self.assertIsNone(action)
        conn.close()

    # ---- Dispatch ----

    def test_dispatch_marks_pending_action_completed(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        response = client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        dispatched = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(dispatched["status"], "completed")
        self.assertIn("Dispatched", dispatched["outcome"])
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'financial_case_action_dispatched'"
        ).fetchone()
        self.assertIsNotNone(audit)
        conn.close()

    def test_dispatch_rejects_already_completed_action(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()
        client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")

        response = client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")
        self.assertEqual(response.status_code, 302)
        self.assertIn("error=action_not_pending", response.headers["Location"])

    def test_dispatch_rejects_action_from_another_case(self):
        case_id, _ = self.create_case()
        other_case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, other_case_id)

        conn = self.connect()
        other_action = self.pending_action(conn, other_case_id)
        conn.close()

        response = client.post(f"/admin/finance/case/{case_id}/action/{other_action['id']}/dispatch")
        self.assertIn("error=action_case_mismatch", response.headers["Location"])

        conn = self.connect()
        unchanged = conn.execute("SELECT * FROM case_action WHERE id = ?", (other_action["id"],)).fetchone()
        self.assertEqual(unchanged["status"], "pending")
        conn.close()

    def test_dispatch_rolls_back_when_audit_fails(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        with patch.object(oms, "record_audit", side_effect=RuntimeError("audit failed")):
            response = client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        unchanged = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(unchanged["status"], "pending")
        conn.close()

    def test_non_admin_cannot_dispatch(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        anon_client = oms.app.test_client()
        response = anon_client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers["Location"])

        conn = self.connect()
        unchanged = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(unchanged["status"], "pending")
        conn.close()

    # ---- Override ----

    def test_override_marks_pending_action_overridden_with_reason(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        response = client.post(
            f"/admin/finance/case/{case_id}/action/{action['id']}/override",
            data={"reason": "Customer already paid via bank transfer."},
        )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        overridden = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(overridden["status"], "overridden")
        self.assertIn("Customer already paid via bank transfer.", overridden["outcome"])
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'financial_case_action_overridden'"
        ).fetchone()
        self.assertIsNotNone(audit)
        conn.close()

    def test_override_requires_non_empty_reason(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        response = client.post(
            f"/admin/finance/case/{case_id}/action/{action['id']}/override",
            data={"reason": "   "},
        )
        self.assertIn("error=empty_override_reason", response.headers["Location"])

        conn = self.connect()
        unchanged = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(unchanged["status"], "pending")
        conn.close()

    def test_override_rolls_back_when_audit_fails(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        self.approve_latest_reasoning(client, case_id)

        conn = self.connect()
        action = self.pending_action(conn, case_id)
        conn.close()

        with patch.object(oms, "record_audit", side_effect=RuntimeError("audit failed")):
            response = client.post(
                f"/admin/finance/case/{case_id}/action/{action['id']}/override",
                data={"reason": "Manual override for testing."},
            )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        unchanged = conn.execute("SELECT * FROM case_action WHERE id = ?", (action["id"],)).fetchone()
        self.assertEqual(unchanged["status"], "pending")
        conn.close()

    # ---- Follow-up completion ----

    def test_complete_follow_up_clears_due_date_and_is_no_longer_overdue(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        past = (oms.current_local_datetime() - oms.timedelta(days=1)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        conn.execute("UPDATE financial_case SET follow_up_due_at = ? WHERE id = ?", (past, case_id))
        conn.commit()
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/finance/case/{case_id}/follow-up/complete")
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertEqual(case["follow_up_due_at"], "")
        self.assertFalse(oms.is_follow_up_overdue(case["follow_up_due_at"]))
        completed_action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'follow_up_completed'",
            (case_id,),
        ).fetchone()
        self.assertIsNotNone(completed_action)
        conn.close()

    def test_complete_follow_up_with_no_due_date_is_rejected(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/finance/case/{case_id}/follow-up/complete")
        self.assertIn("error=no_follow_up_to_complete", response.headers["Location"])

    # ---- Escalation ----

    def test_escalate_sets_status_and_records_reason(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(
            f"/admin/finance/case/{case_id}/escalate",
            data={"reason": "Repeated missed follow-ups."},
        )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertEqual(case["status"], "Escalated")
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'case_escalated'",
            (case_id,),
        ).fetchone()
        self.assertIsNotNone(action)
        self.assertIn("Repeated missed follow-ups.", action["outcome"])
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'financial_case_escalated'"
        ).fetchone()
        self.assertIsNotNone(audit)
        conn.close()

    def test_escalate_requires_non_empty_reason(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/finance/case/{case_id}/escalate", data={"reason": ""})
        self.assertIn("error=empty_escalation_reason", response.headers["Location"])

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertNotEqual(case["status"], "Escalated")
        conn.close()

    def test_escalate_is_blocked_when_already_escalated(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)
        client.post(f"/admin/finance/case/{case_id}/escalate", data={"reason": "First escalation."})

        response = client.post(f"/admin/finance/case/{case_id}/escalate", data={"reason": "Second attempt."})
        self.assertIn("error=escalation_blocked", response.headers["Location"])

    def test_escalate_is_blocked_when_case_is_closed(self):
        case_id, _ = self.create_case()
        conn = self.connect()
        conn.execute("UPDATE financial_case SET status = 'Resolved' WHERE id = ?", (case_id,))
        conn.commit()
        conn.close()

        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/finance/case/{case_id}/escalate", data={"reason": "Too late."})
        self.assertIn("error=escalation_blocked", response.headers["Location"])

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertEqual(case["status"], "Resolved")
        conn.close()

    def test_escalate_rolls_back_when_audit_fails(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        with patch.object(oms, "record_audit", side_effect=RuntimeError("audit failed")):
            response = client.post(
                f"/admin/finance/case/{case_id}/escalate",
                data={"reason": "Should not persist."},
            )
        self.assertEqual(response.status_code, 302)

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertNotEqual(case["status"], "Escalated")
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'case_escalated'",
            (case_id,),
        ).fetchone()
        self.assertIsNone(action)
        conn.close()

    def test_non_admin_cannot_escalate(self):
        case_id, _ = self.create_case()
        client = oms.app.test_client()
        response = client.post(
            f"/admin/finance/case/{case_id}/escalate",
            data={"reason": "Unauthorized attempt."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers["Location"])

        conn = self.connect()
        case = self.case_row(conn, case_id)
        self.assertNotEqual(case["status"], "Escalated")
        conn.close()


class Phase5UIWiringTests(unittest.TestCase):
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

    def test_pending_action_renders_dispatch_and_override_controls(self):
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
        self.assertIn(f"/admin/finance/case/{case_id}/action/", body)
        self.assertIn("/dispatch", body)
        self.assertIn("/override", body)
        self.assertIn("Pending", body)

    def test_completed_action_shows_no_controls(self):
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

        conn = self.connect()
        action = conn.execute(
            "SELECT * FROM case_action WHERE case_id = ? AND action_type = 'controlled_action_queued'",
            (case_id,),
        ).fetchone()
        conn.close()
        client.post(f"/admin/finance/case/{case_id}/action/{action['id']}/dispatch")

        body = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertNotIn(f"/action/{action['id']}/dispatch", body)
        self.assertNotIn(f"/action/{action['id']}/override", body)
        self.assertIn("Completed", body)

    def test_escalate_button_hidden_once_case_is_escalated(self):
        case_id = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        before = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertIn(f"/admin/finance/case/{case_id}/escalate", before)

        client.post(f"/admin/finance/case/{case_id}/escalate", data={"reason": "Testing UI hide."})
        after = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertNotIn(f"/admin/finance/case/{case_id}/escalate", after)

    def test_follow_up_complete_button_only_shown_when_due_date_set(self):
        case_id = self.create_case()
        client = oms.app.test_client()
        self.login_admin(client)

        without_due_date = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertNotIn(f"/admin/finance/case/{case_id}/follow-up/complete", without_due_date)

        conn = self.connect()
        future = (oms.current_local_datetime() + oms.timedelta(days=2)).strftime(oms.DISPLAY_DATETIME_FORMAT)
        conn.execute("UPDATE financial_case SET follow_up_due_at = ? WHERE id = ?", (future, case_id))
        conn.commit()
        conn.close()

        with_due_date = client.get(f"/admin/finance/case/{case_id}").data.decode()
        self.assertIn(f"/admin/finance/case/{case_id}/follow-up/complete", with_due_date)


if __name__ == "__main__":
    unittest.main()
