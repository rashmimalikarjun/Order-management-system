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


class FinanceControllerAgentTests(unittest.TestCase):
    """Test the Finance Controller Agent for reconciliation exception analysis."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        oms.DATABASE = self.db_path
        oms.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        os.environ.pop("GEMINI_API_KEY", None)  # Force deterministic fallback
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


class DeterministicAnalysisTests(FinanceControllerAgentTests):
    """Test the deterministic fallback analysis (no Gemini API)."""

    def test_deterministic_analysis_returns_correct_batch_metrics(self):
        """Verify batch metrics are computed from actual data, not fabricated."""
        conn = self.connect()

        # Create orders for testing
        order_a = self.create_order(conn, "alice", 500.0, "Pending", "REF-A")
        order_b = self.create_order(conn, "bob", 1000.0, "Pending", "REF-B")

        # Create a settlement batch manually
        settlements = [
            {"external_ref": "REF-A", "amount": 500.0, "settled_at": "t1", "source": "Razorpay"},
            {"external_ref": "REF-B", "amount": 950.0, "settled_at": "t2", "source": "Bank NEFT"},  # mismatch
            {"external_ref": "ORPHAN", "amount": 750.0, "settled_at": "t3", "source": "UPI"},  # no match
        ]

        outcome = oms.reconcile_settlement_batch(conn, settlements)
        
        # Get batch info
        batch_info = {
            "id": 1,
            "created_at": "now",
            "triggered_by": "test",
            "record_count": outcome["total"],
            "matched_count": outcome["counts"]["matched"],
            "match_rate": outcome["match_rate"],
        }
        
        # Get exceptions
        exceptions = [r for r in outcome["results"] if r["classification"] != "matched"]
        
        # Order data map
        order_data_map = {
            order_a: {"status": "Delivered", "payment_status": "Pending", "total_price": 500.0},
        }

        # Run deterministic analysis
        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, exceptions, order_data_map)

        # Verify batch metrics are correct
        self.assertEqual(result["batch_metrics"]["total_records"], 3)
        self.assertEqual(result["batch_metrics"]["matched_records"], 1)
        self.assertEqual(result["batch_metrics"]["exception_records"], 2)
        self.assertAlmostEqual(result["batch_metrics"]["match_rate_percent"], 33.33, places=1)
        
        # Unresolved amount should be sum of exception amounts (not fabricated)
        expected_unresolved = 950.0 + 750.0  # mismatch + orphan
        self.assertEqual(result["batch_metrics"]["unresolved_amount_inr"], expected_unresolved)

        conn.close()

    def test_deterministic_analysis_exception_breakdown_is_accurate(self):
        """Verify exception breakdown counts match actual classifications."""
        conn = self.connect()

        # Create an order that is already Paid (before batch)
        order_a = self.create_order(conn, "alice", 500.0, "Paid", "REF-A")
        
        # Create a Pending order for duplicate test
        order_b = self.create_order(conn, "bob", 300.0, "Pending", "REF-B")
        
        settlements = [
            {"external_ref": "REF-A", "amount": 500.0, "settled_at": "t1", "source": "Razorpay"},  # already_reconciled (order was Paid)
            {"external_ref": "REF-A", "amount": 500.0, "settled_at": "t2", "source": "Razorpay"},  # already_reconciled (order was Paid)
            {"external_ref": "REF-B", "amount": 300.0, "settled_at": "t3", "source": "Bank"},      # matched (first in batch)
            {"external_ref": "REF-B", "amount": 300.0, "settled_at": "t4", "source": "Bank"},      # duplicate_settlement (same batch)
            {"external_ref": "MISMATCH", "amount": 450.0, "settled_at": "t5", "source": "UPI"},    # no_matching_order
        ]

        outcome = oms.reconcile_settlement_batch(conn, settlements)
        
        batch_info = {
            "id": 1,
            "created_at": "now",
            "triggered_by": "test",
            "record_count": outcome["total"],
            "matched_count": outcome["counts"]["matched"],
            "match_rate": outcome["match_rate"],
        }
        
        exceptions = [r for r in outcome["results"] if r["classification"] != "matched"]

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, exceptions, {})

        # Verify breakdown matches actual counts
        # Both REF-A settlements are already_reconciled (order was Paid before batch)
        self.assertEqual(result["exception_breakdown"]["already_reconciled"], 2)
        # Second REF-B is duplicate within same batch
        self.assertEqual(result["exception_breakdown"]["duplicate_settlement"], 1)
        # MISMATCH has no matching order
        self.assertEqual(result["exception_breakdown"]["no_matching_order"], 1)
        self.assertEqual(result["exception_breakdown"]["amount_mismatch"], 0)

        conn.close()

    def test_deterministic_prioritization_orders_by_financial_impact(self):
        """Verify exceptions are prioritized by amount and severity."""
        conn = self.connect()

        settlements = [
            {"id": 1, "external_ref": "SMALL", "amount": 50.0, "settled_at": "t1", 
             "source": "UPI", "order_id": None, "classification": "no_matching_order", "reason": "test"},
            {"id": 2, "external_ref": "LARGE", "amount": 2000.0, "settled_at": "t2", 
             "source": "Razorpay", "order_id": None, "classification": "no_matching_order", "reason": "test"},
            {"id": 3, "external_ref": "MED", "amount": 500.0, "settled_at": "t3", 
             "source": "Bank", "order_id": None, "classification": "amount_mismatch", "reason": "test"},
        ]

        batch_info = {"id": 1, "created_at": "now", "triggered_by": "test", 
                      "record_count": 3, "matched_count": 0, "match_rate": 0.0}

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, settlements, {})

        # Verify prioritization: largest amount first
        prioritized = result["prioritized_exceptions"]
        self.assertEqual(len(prioritized), 3)
        self.assertEqual(prioritized[0]["settlement_id"], 2)  # 2000
        self.assertEqual(prioritized[0]["priority_level"], "HIGH")
        self.assertEqual(prioritized[1]["settlement_id"], 3)  # 500
        self.assertEqual(prioritized[1]["priority_level"], "MEDIUM")
        self.assertEqual(prioritized[2]["settlement_id"], 1)  # 50
        self.assertEqual(prioritized[2]["priority_level"], "LOW")

        conn.close()

    def test_deterministic_duplicate_gets_high_priority_regardless_of_amount(self):
        """Verify duplicate settlements are always HIGH priority."""
        conn = self.connect()

        settlements = [
            {"id": 1, "external_ref": "DUP", "amount": 100.0, "settled_at": "t1", 
             "source": "Razorpay", "order_id": 1, "classification": "duplicate_settlement", "reason": "test"},
        ]

        batch_info = {"id": 1, "created_at": "now", "triggered_by": "test", 
                      "record_count": 1, "matched_count": 0, "match_rate": 0.0}

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, settlements, {})

        prioritized = result["prioritized_exceptions"]
        self.assertEqual(prioritized[0]["priority_level"], "HIGH")
        self.assertIn("Duplicate", prioritized[0]["reason_for_priority"])

        conn.close()

    def test_deterministic_recommendations_match_classification(self):
        """Verify recommended actions match exception type."""
        conn = self.connect()

        settlements = [
            {"id": 1, "external_ref": "MISMATCH", "amount": 500.0, "settled_at": "t1", 
             "source": "Razorpay", "order_id": 1, "classification": "amount_mismatch", "reason": "test"},
            {"id": 2, "external_ref": "DUP", "amount": 300.0, "settled_at": "t2", 
             "source": "Bank", "order_id": 1, "classification": "duplicate_settlement", "reason": "test"},
            {"id": 3, "external_ref": "ORPHAN", "amount": 200.0, "settled_at": "t3", 
             "source": "UPI", "order_id": None, "classification": "no_matching_order", "reason": "test"},
            {"id": 4, "external_ref": "PAID", "amount": 400.0, "settled_at": "t4", 
             "source": "Razorpay", "order_id": 1, "classification": "already_reconciled", "reason": "test"},
        ]

        batch_info = {"id": 1, "created_at": "now", "triggered_by": "test", 
                      "record_count": 4, "matched_count": 0, "match_rate": 0.0}

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, settlements, {})

        recommendations = {r["settlement_id"]: r for r in result["recommendations"]}
        
        self.assertEqual(recommendations[1]["recommended_action"], "review_amount_mismatch")
        self.assertEqual(recommendations[2]["recommended_action"], "check_duplicate")
        self.assertEqual(recommendations[3]["recommended_action"], "investigate")
        self.assertEqual(recommendations[4]["recommended_action"], "verify_reference")

        conn.close()

    def test_deterministic_financial_case_recommendation_threshold(self):
        """Verify financial case is recommended only for significant issues."""
        conn = self.connect()

        settlements = [
            {"id": 1, "external_ref": "BIG", "amount": 1500.0, "settled_at": "t1", 
             "source": "Razorpay", "order_id": None, "classification": "no_matching_order", "reason": "test"},
            {"id": 2, "external_ref": "SMALL", "amount": 100.0, "settled_at": "t2", 
             "source": "UPI", "order_id": None, "classification": "no_matching_order", "reason": "test"},
            {"id": 3, "external_ref": "DUP", "amount": 200.0, "settled_at": "t3", 
             "source": "Bank", "order_id": 1, "classification": "duplicate_settlement", "reason": "test"},
        ]

        batch_info = {"id": 1, "created_at": "now", "triggered_by": "test", 
                      "record_count": 3, "matched_count": 0, "match_rate": 0.0}

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, settlements, {})

        recommendations = {r["settlement_id"]: r for r in result["recommendations"]}
        
        # High value orphan -> should create case
        self.assertTrue(recommendations[1]["should_create_financial_case"])
        # Low value orphan -> no case
        self.assertFalse(recommendations[2]["should_create_financial_case"])
        # Duplicate -> always create case
        self.assertTrue(recommendations[3]["should_create_financial_case"])

        conn.close()

    def test_deterministic_explanations_use_actual_data(self):
        """Verify explanations cite actual reconciliation data, not fabricated facts."""
        conn = self.connect()

        settlements = [
            {"id": 1, "external_ref": "REF-123", "amount": 750.0, "settled_at": "2024-01-15", 
             "source": "Razorpay Settlement", "order_id": None, 
             "classification": "no_matching_order", 
             "reason": "No order found with payment_reference 'REF-123'"},
        ]

        batch_info = {"id": 1, "created_at": "now", "triggered_by": "test", 
                      "record_count": 1, "matched_count": 0, "match_rate": 0.0}

        result = oms.evaluate_reconciliation_batch_deterministic(batch_info, settlements, {})

        explanation = result["exception_explanations"][0]
        
        # Verify explanation uses actual data
        self.assertIn("750.00", explanation["what_happened"])
        self.assertIn("REF-123", explanation["what_happened"])
        self.assertIn("Razorpay Settlement", explanation["what_happened"])
        self.assertIn("No matching order", explanation["relevant_order_info"])

        conn.close()


class ValidationTests(FinanceControllerAgentTests):
    """Test response validation for AI agent output."""

    def test_validate_reconciliation_agent_response_accepts_valid_schema(self):
        """Verify valid response schema passes validation."""
        valid_response = {
            "batch_metrics": {
                "total_records": 10,
                "matched_records": 7,
                "exception_records": 3,
                "match_rate_percent": 70.0,
                "reconciled_amount_inr": 700.0,
                "unresolved_amount_inr": 300.0
            },
            "exception_breakdown": {
                "amount_mismatch": 1,
                "duplicate_settlement": 1,
                "no_matching_order": 1,
                "already_reconciled": 0
            },
            "prioritized_exceptions": [
                {
                    "settlement_id": 1,
                    "priority_rank": 1,
                    "priority_level": "HIGH",
                    "financial_impact_inr": 500.0,
                    "reason_for_priority": "High value transaction"
                }
            ],
            "exception_explanations": [
                {
                    "settlement_id": 1,
                    "what_happened": "Settlement processed",
                    "why_reconciliation_failed": "Amount mismatch detected",
                    "financial_impact": "INR 500 unresolved",
                    "relevant_order_info": "Order #1 found"
                }
            ],
            "recommendations": [
                {
                    "settlement_id": 1,
                    "recommended_action": "review_amount_mismatch",
                    "action_justification": "Verify fee agreement",
                    "should_create_financial_case": False
                }
            ]
        }

        self.assertTrue(oms.validate_reconciliation_agent_response(valid_response))

    def test_validate_reconciliation_agent_response_rejects_invalid_priority_level(self):
        """Verify invalid priority level fails validation."""
        invalid_response = {
            "batch_metrics": {"total_records": 1, "matched_records": 0, "exception_records": 1,
                              "match_rate_percent": 0.0, "reconciled_amount_inr": 0.0, "unresolved_amount_inr": 100.0},
            "exception_breakdown": {"amount_mismatch": 0, "duplicate_settlement": 0, 
                                    "no_matching_order": 1, "already_reconciled": 0},
            "prioritized_exceptions": [
                {"settlement_id": 1, "priority_rank": 1, "priority_level": "INVALID",
                 "financial_impact_inr": 100.0, "reason_for_priority": "test"}
            ],
            "exception_explanations": [
                {"settlement_id": 1, "what_happened": "test", "why_reconciliation_failed": "test",
                 "financial_impact": "test", "relevant_order_info": "test"}
            ],
            "recommendations": [
                {"settlement_id": 1, "recommended_action": "test", "action_justification": "test",
                 "should_create_financial_case": False}
            ]
        }

        self.assertFalse(oms.validate_reconciliation_agent_response(invalid_response))

    def test_validate_reconciliation_agent_response_rejects_non_boolean_case_flag(self):
        """Verify non-boolean should_create_financial_case fails validation."""
        invalid_response = {
            "batch_metrics": {"total_records": 1, "matched_records": 0, "exception_records": 1,
                              "match_rate_percent": 0.0, "reconciled_amount_inr": 0.0, "unresolved_amount_inr": 100.0},
            "exception_breakdown": {"amount_mismatch": 0, "duplicate_settlement": 0, 
                                    "no_matching_order": 1, "already_reconciled": 0},
            "prioritized_exceptions": [
                {"settlement_id": 1, "priority_rank": 1, "priority_level": "LOW",
                 "financial_impact_inr": 100.0, "reason_for_priority": "test"}
            ],
            "exception_explanations": [
                {"settlement_id": 1, "what_happened": "test", "why_reconciliation_failed": "test",
                 "financial_impact": "test", "relevant_order_info": "test"}
            ],
            "recommendations": [
                {"settlement_id": 1, "recommended_action": "test", "action_justification": "test",
                 "should_create_financial_case": "yes"}  # Should be boolean
            ]
        }

        self.assertFalse(oms.validate_reconciliation_agent_response(invalid_response))


class RouteTests(FinanceControllerAgentTests):
    """Test the analyze route endpoint."""

    def test_analyze_route_requires_admin_login(self):
        """Verify analyze route requires admin authentication."""
        client = oms.app.test_client()
        response = client.post("/admin/reconciliation/1/analyze")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/admin", response.headers.get("Location", ""))

    def test_analyze_route_returns_404_for_nonexistent_batch(self):
        """Verify 404 returned for batch that doesn't exist."""
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post("/admin/reconciliation/99999/analyze")
        self.assertEqual(response.status_code, 404)

    def test_analyze_route_returns_valid_json_structure(self):
        """Verify analyze route returns properly structured JSON."""
        conn = self.connect()
        
        # Create a simple batch using run_new_reconciliation_batch which persists data
        order = self.create_order(conn, "test", 100.0, "Pending", "TEST-REF")
        settlements = [
            {"external_ref": "TEST-REF", "amount": 100.0, "settled_at": "now", "source": "Test"},
        ]
        
        # Reconcile and persist the batch (this inserts into DB)
        outcome = oms.reconcile_settlement_batch(conn, settlements)
        
        # Manually insert batch header and settlements (mimicking run_new_reconciliation_batch)
        now = oms.now_string()
        counts = outcome["counts"]
        cursor = conn.execute(
            """
            INSERT INTO reconciliation_batches (
                created_at, triggered_by, record_count, matched_count,
                amount_mismatch_count, duplicate_settlement_count,
                no_matching_order_count, already_reconciled_count, match_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, "test", outcome["total"], counts["matched"],
                counts["amount_mismatch"], counts["duplicate_settlement"],
                counts["no_matching_order"], counts["already_reconciled"],
                outcome["match_rate"],
            ),
        )
        batch_id = cursor.lastrowid
        
        # Insert settlement records
        conn.executemany(
            """
            INSERT INTO reconciliation_settlements (
                batch_id, external_ref, amount, settled_at, source,
                order_id, classification, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (batch_id, r["external_ref"], r["amount"], r["settled_at"], r["source"],
                 r.get("order_id"), r["classification"], r["reason"])
                for r in outcome["results"]
            ],
        )
        conn.commit()
        
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/reconciliation/{batch_id}/analyze")
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/json")
        
        data = response.get_json()
        
        # Verify required keys present
        self.assertIn("batch_metrics", data)
        self.assertIn("exception_breakdown", data)
        self.assertIn("prioritized_exceptions", data)
        self.assertIn("exception_explanations", data)
        self.assertIn("recommendations", data)
        self.assertIn("batch_id", data)
        self.assertEqual(data["batch_id"], batch_id)

        conn.close()

    def test_analyze_route_does_not_modify_reconciliation_records(self):
        """Verify analyze route is read-only on reconciliation data."""
        conn = self.connect()
        
        order = self.create_order(conn, "test", 100.0, "Pending", "TEST-REF")
        settlements = [
            {"external_ref": "TEST-REF", "amount": 100.0, "settled_at": "now", "source": "Test"},
        ]
        outcome = oms.reconcile_settlement_batch(conn, settlements)
        conn.commit()
        
        # Capture state before analysis
        batch_row_before = conn.execute(
            "SELECT * FROM reconciliation_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        settlement_count_before = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_settlements WHERE batch_id = ?", 
            (batch_row_before["id"],)
        ).fetchone()[0]
        
        batch_id = batch_row_before["id"]
        conn.close()

        # Call analyze route
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/reconciliation/{batch_id}/analyze")
        self.assertEqual(response.status_code, 200)

        # Verify state unchanged
        conn = self.connect()
        batch_row_after = conn.execute(
            "SELECT * FROM reconciliation_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        settlement_count_after = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_settlements WHERE batch_id = ?", 
            (batch_id,)
        ).fetchone()[0]
        
        self.assertEqual(batch_row_before["record_count"], batch_row_after["record_count"])
        self.assertEqual(batch_row_before["matched_count"], batch_row_after["matched_count"])
        self.assertEqual(batch_row_before["match_rate"], batch_row_after["match_rate"])
        self.assertEqual(settlement_count_before, settlement_count_after)
        
        conn.close()

    def test_analyze_route_does_not_create_financial_cases(self):
        """Verify analyze route does NOT auto-create financial cases."""
        conn = self.connect()
        
        order = self.create_order(conn, "test", 1000.0, "Pending", "TEST-REF")
        settlements = [
            {"external_ref": "TEST-REF", "amount": 950.0, "settled_at": "now", "source": "Test"},  # mismatch
        ]
        oms.reconcile_settlement_batch(conn, settlements)
        conn.commit()
        
        # Count financial cases before
        cases_before = conn.execute("SELECT COUNT(*) FROM financial_case").fetchone()[0]
        
        batch_row = conn.execute("SELECT id FROM reconciliation_batches ORDER BY id DESC LIMIT 1").fetchone()
        batch_id = batch_row["id"]
        conn.close()

        # Call analyze route
        client = oms.app.test_client()
        self.login_admin(client)
        response = client.post(f"/admin/reconciliation/{batch_id}/analyze")
        self.assertEqual(response.status_code, 200)

        # Verify no new financial cases created
        conn = self.connect()
        cases_after = conn.execute("SELECT COUNT(*) FROM financial_case").fetchone()[0]
        self.assertEqual(cases_before, cases_after)
        conn.close()


if __name__ == "__main__":
    unittest.main()
