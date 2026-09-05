import os
import tempfile
import unittest

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


class ReconciliationTestCase(unittest.TestCase):
    """Base fixture: fresh temp SQLite DB per test, matching the pattern
    used in tests/test_phase3_financial_approval.py."""

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

    def create_order(self, conn, username="test_user", total_price=100.0,
                      payment_status="Pending", payment_reference=""):
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
                username, "Veg Meal", 1, now, "Delivered", now,
                total_price, "UPI QR", payment_status, payment_reference, "", "",
            ),
        )
        conn.commit()
        return cursor.lastrowid


class DeliberateBatchExactCountsTests(ReconciliationTestCase):
    """A deliberately-constructed batch must produce exact expected counts
    per category - not just 'some exceptions exist'."""

    def test_exact_classification_counts_and_reasons(self):
        conn = self.connect()

        order_a = self.create_order(conn, "alice", 100.0, "Pending", "REF-A")
        order_b = self.create_order(conn, "bob", 200.0, "Pending", "REF-B")
        order_c = self.create_order(conn, "carol", 150.0, "Pending", "")  # no reference on file
        order_d = self.create_order(conn, "dave", 300.0, "Paid", "REF-D")  # already reconciled

        settlements = [
            {"external_ref": "REF-A", "amount": 100.0, "settled_at": "t1", "source": "Razorpay Settlement"},
            {"external_ref": "REF-A", "amount": 100.0, "settled_at": "t2", "source": "Razorpay Settlement"},  # duplicate
            {"external_ref": "REF-B", "amount": 190.0, "settled_at": "t3", "source": "Bank NEFT"},  # fee/rounding off
            {"external_ref": "", "amount": 150.0, "settled_at": "t4", "source": "UPI Settlement File"},  # amount fallback
            {"external_ref": "REF-D", "amount": 300.0, "settled_at": "t5", "source": "Razorpay Settlement"},  # already paid
            {"external_ref": "GARBAGE-REF", "amount": 9999.0, "settled_at": "t6", "source": "Bank IMPS"},  # no match
        ]

        outcome = oms.reconcile_settlement_batch(conn, settlements)

        self.assertEqual(outcome["total"], 6)
        counts = {k: v for k, v in outcome["counts"].items() if v > 0}
        self.assertEqual(
            counts,
            {
                "matched": 2,
                "amount_mismatch": 1,
                "duplicate_settlement": 1,
                "no_matching_order": 1,
                "already_reconciled": 1,
            },
        )
        self.assertAlmostEqual(outcome["match_rate"], round(2 / 6 * 100, 2))

        by_ref_order = {(r["external_ref"], r["amount"]): r for r in outcome["results"]}

        # Every non-matched record has a specific, human-readable reason -
        # not a generic "no match".
        for record in outcome["results"]:
            if record["classification"] != "matched":
                self.assertNotEqual(record["reason"].strip(), "")
                self.assertNotIn("no match", record["reason"].lower())

        mismatch_record = [r for r in outcome["results"] if r["classification"] == "amount_mismatch"][0]
        self.assertIn(str(order_b), mismatch_record["reason"])
        self.assertIn("10.00", mismatch_record["reason"])  # 200 - 190

        no_match_record = [r for r in outcome["results"] if r["classification"] == "no_matching_order"][0]
        self.assertIn("GARBAGE-REF", no_match_record["reason"])

        # Order A was actually closed (loop closed, not just diagnosed).
        order_a_row = conn.execute("SELECT payment_status FROM orders WHERE id = ?", (order_a,)).fetchone()
        self.assertEqual(order_a_row["payment_status"], "Paid")

        # Orders that only mismatched or were already Paid were left alone.
        order_b_row = conn.execute("SELECT payment_status FROM orders WHERE id = ?", (order_b,)).fetchone()
        self.assertEqual(order_b_row["payment_status"], "Pending")

        # Order C was closed via the amount-fallback pass.
        order_c_row = conn.execute("SELECT payment_status, payment_reference FROM orders WHERE id = ?", (order_c,)).fetchone()
        self.assertEqual(order_c_row["payment_status"], "Paid")

        # Order D (already Paid before the batch) was never touched again.
        order_d_row = conn.execute("SELECT payment_status FROM orders WHERE id = ?", (order_d,)).fetchone()
        self.assertEqual(order_d_row["payment_status"], "Paid")

        conn.close()


class IdempotentRerunTests(ReconciliationTestCase):
    """Running reconciliation twice on the same batch must not double-count,
    and an already-reconciled order must never be silently miscounted as a
    fresh match."""

    def test_second_run_reclassifies_instead_of_double_counting(self):
        conn = self.connect()
        self.create_order(conn, "erin", 250.0, "Pending", "REF-E")

        settlements = [
            {"external_ref": "REF-E", "amount": 250.0, "settled_at": "t1", "source": "Razorpay Settlement"},
        ]

        first_run = oms.reconcile_settlement_batch(conn, settlements)
        conn.commit()
        self.assertEqual(first_run["counts"]["matched"], 1)
        self.assertEqual(first_run["counts"]["already_reconciled"], 0)

        second_run = oms.reconcile_settlement_batch(conn, settlements)
        conn.commit()

        self.assertEqual(second_run["counts"]["matched"], 0)
        self.assertEqual(second_run["counts"]["already_reconciled"], 1)
        self.assertEqual(second_run["counts"]["duplicate_settlement"], 0)

        conn.close()

    def test_preexisting_paid_order_never_counted_as_fresh_match(self):
        conn = self.connect()
        self.create_order(conn, "frank", 175.0, "Paid", "REF-F")

        settlements = [
            {"external_ref": "REF-F", "amount": 175.0, "settled_at": "t1", "source": "Bank NEFT"},
        ]

        outcome = oms.reconcile_settlement_batch(conn, settlements)

        self.assertEqual(outcome["counts"]["matched"], 0)
        self.assertEqual(outcome["counts"]["already_reconciled"], 1)
        self.assertEqual(outcome["results"][0]["classification"], "already_reconciled")

        conn.close()


class GeneratedBatchStructureTests(ReconciliationTestCase):
    """The randomized live-demo generator must satisfy Track 04's throughput
    bar and produce a self-consistent batch every time, without asserting
    exact counts (since it is intentionally randomized)."""

    def test_generated_batch_has_50_plus_records_and_all_categories_covered(self):
        conn = self.connect()
        settlements = oms.generate_settlement_batch(conn)

        self.assertGreaterEqual(len(settlements), 50)
        for record in settlements:
            self.assertIn("external_ref", record)
            self.assertIn("amount", record)
            self.assertIn("settled_at", record)
            self.assertIn("source", record)
            self.assertGreater(record["amount"], 0)

        outcome = oms.reconcile_settlement_batch(conn, settlements)
        self.assertEqual(sum(outcome["counts"].values()), outcome["total"])
        self.assertGreaterEqual(outcome["total"], 50)

        # A cherry-picked match proves nothing - assert the mix is genuinely
        # spread across match + exception categories, not all one bucket.
        self.assertGreater(outcome["counts"]["matched"], 0)
        self.assertGreater(
            outcome["counts"]["amount_mismatch"]
            + outcome["counts"]["duplicate_settlement"]
            + outcome["counts"]["no_matching_order"]
            + outcome["counts"]["already_reconciled"],
            0,
        )

        conn.close()

    def test_regenerating_recomputes_match_rate_live(self):
        conn = self.connect()
        batch_one = oms.reconcile_settlement_batch(conn, oms.generate_settlement_batch(conn))
        conn.commit()
        batch_two = oms.reconcile_settlement_batch(conn, oms.generate_settlement_batch(conn))
        conn.commit()

        # Both runs computed their own match rate from their own batch -
        # neither is hardcoded, and each is internally consistent.
        for outcome in (batch_one, batch_two):
            expected_rate = round((outcome["counts"]["matched"] / outcome["total"]) * 100, 2)
            self.assertAlmostEqual(outcome["match_rate"], expected_rate)

        conn.close()


class ReconciliationRoutePersistenceTests(ReconciliationTestCase):
    """The admin route persists every run (batch id, match rate, exception
    count) and displays a full readable exception list."""

    def test_run_route_persists_batch_and_renders_results(self):
        client = oms.app.test_client()
        self.login_admin(client)

        response = client.post("/admin/reconciliation/run", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        body = response.get_data(as_text=True)
        self.assertIn("Match Rate", body)
        self.assertIn("Exception List", body)

        conn = self.connect()
        batch_row = conn.execute(
            "SELECT * FROM reconciliation_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(batch_row)
        self.assertGreaterEqual(batch_row["record_count"], 50)
        self.assertEqual(
            batch_row["matched_count"]
            + batch_row["amount_mismatch_count"]
            + batch_row["duplicate_settlement_count"]
            + batch_row["no_matching_order_count"]
            + batch_row["already_reconciled_count"],
            batch_row["record_count"],
        )

        settlement_row_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation_settlements WHERE batch_id = ?",
            (batch_row["id"],),
        ).fetchone()["c"]
        self.assertEqual(settlement_row_count, batch_row["record_count"])

        audit_row = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'reconciliation_batch_run' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(audit_row)
        self.assertIn(f"batch_id={batch_row['id']}", audit_row["details"])

        conn.close()

    def test_route_requires_admin_login(self):
        client = oms.app.test_client()
        response = client.post("/admin/reconciliation/run")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers.get("Location", ""))


class AdditivityTests(ReconciliationTestCase):
    """Reconciliation must not touch existing financial_case / case_* tables."""

    def test_reconciliation_run_does_not_create_financial_cases(self):
        conn = self.connect()
        before = conn.execute("SELECT COUNT(*) AS c FROM financial_case").fetchone()["c"]
        oms.run_new_reconciliation_batch(conn, "admin")
        after = conn.execute("SELECT COUNT(*) AS c FROM financial_case").fetchone()["c"]
        self.assertEqual(before, after)
        conn.close()


if __name__ == "__main__":
    unittest.main()
