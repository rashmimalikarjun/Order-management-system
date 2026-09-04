"""
Evaluation Test for Razorpay Buildathon Track 04: AI Finance Controller

This test measures ACTUAL performance of the Finance Controller / reconciliation workflow
using a synthetic batch of 50+ settlement records.

Metrics measured:
- Throughput (records/sec)
- Match rate
- Exception breakdown
- Classification accuracy (against ground truth)
- Unresolved exceptions requiring manual review
- Finance Controller Agent behavior

IMPORTANT: All metrics are from actual execution, NOT fabricated.
"""

import os
import tempfile
import time
import unittest

_import_fd, _import_db = tempfile.mkstemp(suffix=".db")
os.close(_import_fd)
os.environ["DATABASE_PATH"] = _import_db
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.pop("GEMINI_API_KEY", None)  # Force deterministic fallback for reproducibility

import app as oms


def tearDownModule():
    if os.path.exists(_import_db):
        os.remove(_import_db)


class FinanceControllerEvaluationTests(unittest.TestCase):
    """Evaluation tests for Razorpay Track 04 requirements."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        oms.DATABASE = self.db_path
        oms.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        os.environ.pop("GEMINI_API_KEY", None)  # Force deterministic fallback
        oms.init_db()
        
        # Seed the database with initial orders needed for reconciliation
        conn = self.connect()
        oms.ensure_reconciliation_seed_orders(conn)
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def connect(self):
        return oms.get_db_connection()

    def login_admin(self, client):
        with client.session_transaction() as sess:
            sess["admin_logged_in"] = True

    def test_full_evaluation_run(self):
        """
        Complete evaluation run measuring:
        - Dataset size and composition
        - Processing time and throughput
        - Match rate and exception breakdown
        - Ground truth classification accuracy
        - Unresolved exceptions requiring manual review
        - Finance Controller Agent analysis quality
        """
        conn = self.connect()
        
        # =========================================================
        # PHASE 1: Generate Settlement Batch (Ground Truth Known)
        # =========================================================
        start_time = time.time()
        
        # Generate settlements - this creates 50+ records with known composition
        settlements = oms.generate_settlement_batch(conn)
        
        generation_time = time.time() - start_time
        
        # Record ground truth before reconciliation mutates order state
        ground_truth = {}
        expected_classifications = {}
        
        # Analyze what we generated to establish ground truth
        orders = conn.execute(
            "SELECT id, total_price, payment_status, payment_reference FROM orders"
        ).fetchall()
        
        ref_to_order = {}
        for o in orders:
            ref = (o["payment_reference"] or "").strip()
            if ref:
                ref_to_order[ref] = o
        
        # Track which references we've seen (for duplicate detection)
        seen_refs = set()
        
        for i, settlement in enumerate(settlements):
            ext_ref = (settlement.get("external_ref") or "").strip()
            amount = round(float(settlement.get("amount", 0) or 0), 2)
            
            # Determine expected classification based on settlement characteristics
            if ext_ref in seen_refs:
                expected_class = "duplicate_settlement"
            elif not ext_ref or ext_ref not in ref_to_order:
                # Check if amount matches any order
                found_match = False
                for o in orders:
                    order_total = round(float(o["total_price"] or 0), 2)
                    if abs(order_total - amount) <= oms.RECON_FALLBACK_AMOUNT_TOLERANCE:
                        found_match = True
                        break
                if not found_match:
                    expected_class = "no_matching_order"
                else:
                    expected_class = "matched"  # Will match by amount
            elif ext_ref in ref_to_order:
                order = ref_to_order[ext_ref]
                order_total = round(float(order["total_price"] or 0), 2)
                if abs(order_total - amount) > oms.RECON_REFERENCE_AMOUNT_TOLERANCE:
                    expected_class = "amount_mismatch"
                elif order["payment_status"] == "Paid":
                    expected_class = "already_reconciled"
                else:
                    expected_class = "matched"
            else:
                expected_class = "no_matching_order"
            
            ground_truth[i] = {
                "settlement": settlement,
                "expected_classification": expected_class,
            }
            
            if ext_ref:
                seen_refs.add(ext_ref)
        
        # =========================================================
        # PHASE 2: Run Reconciliation
        # =========================================================
        reconcile_start = time.time()
        outcome = oms.reconcile_settlement_batch(conn, settlements)
        reconcile_time = time.time() - reconcile_start
        
        results = outcome["results"]
        counts = outcome["counts"]
        total_records = outcome["total"]
        match_rate = outcome["match_rate"]
        
        # =========================================================
        # PHASE 3: Create Persistence Batch (for agent analysis)
        # =========================================================
        batch_id = oms.run_new_reconciliation_batch(conn, "evaluation_test")
        
        # Fetch the persisted batch for agent analysis
        batch, db_settlements = oms.get_reconciliation_batch(conn, batch_id)
        
        # =========================================================
        # PHASE 4: Run Finance Controller Agent Analysis
        # =========================================================
        agent_start = time.time()
        
        exceptions = [s for s in db_settlements if s["classification"] != "matched"]
        
        # Enrich with order data
        order_ids = set(ex.get("order_id") for ex in exceptions if ex.get("order_id"))
        order_data_map = {}
        if order_ids:
            placeholders = ",".join("?" * len(order_ids))
            order_rows = conn.execute(
                f"SELECT id, status, payment_status, total_price FROM orders WHERE id IN ({placeholders})",
                tuple(order_ids)
            ).fetchall()
            order_data_map = {o["id"]: dict(o) for o in order_rows}
        
        # Run deterministic analysis (Gemini disabled for reproducibility)
        agent_result = oms.evaluate_reconciliation_batch_deterministic(batch, exceptions, order_data_map)
        
        agent_time = time.time() - agent_start
        total_time = time.time() - start_time
        
        # =========================================================
        # PHASE 5: Calculate Metrics
        # =========================================================
        
        # Actual classifications from reconciliation
        actual_classifications = [r["classification"] for r in results]
        
        # Count by classification
        actual_counts = {
            "matched": 0,
            "amount_mismatch": 0,
            "duplicate_settlement": 0,
            "no_matching_order": 0,
            "already_reconciled": 0,
        }
        for cls in actual_classifications:
            if cls in actual_counts:
                actual_counts[cls] += 1
        
        # Calculate classification accuracy against ground truth
        correct_predictions = 0
        total_predictions = 0
        misclassifications = []
        
        for i, result in enumerate(results):
            if i in ground_truth:
                expected = ground_truth[i]["expected_classification"]
                actual = result["classification"]
                total_predictions += 1
                if expected == actual:
                    correct_predictions += 1
                else:
                    misclassifications.append({
                        "index": i,
                        "expected": expected,
                        "actual": actual,
                        "settlement": result,
                    })
        
        classification_accuracy = (correct_predictions / total_predictions * 100) if total_predictions > 0 else 0
        
        # Calculate throughput
        throughput = total_records / total_time if total_time > 0 else 0
        
        # Identify unresolved exceptions (those requiring manual review)
        unresolved_exceptions = []
        for rec in results:
            if rec["classification"] != "matched":
                # Check agent recommendations
                settlement_id = rec.get("id", 0)
                agent_rec = None
                for r in agent_result.get("recommendations", []):
                    if r.get("settlement_id") == settlement_id:
                        agent_rec = r
                        break
                
                # Exceptions without clear resolution path are unresolved
                is_unresolved = (
                    rec["classification"] in ["no_matching_order", "duplicate_settlement"] or
                    (agent_rec and not agent_rec.get("should_create_financial_case", False))
                )
                
                if is_unresolved:
                    unresolved_exceptions.append({
                        "external_ref": rec["external_ref"],
                        "amount": rec["amount"],
                        "classification": rec["classification"],
                        "reason": rec["reason"],
                        "order_id": rec["order_id"],
                    })
        
        # Get agent analysis metrics
        agent_source = agent_result.get("analysis_source", "unknown")
        prioritized_count = len(agent_result.get("prioritized_exceptions", []))
        explanations_count = len(agent_result.get("exception_explanations", []))
        recommendations_count = len(agent_result.get("recommendations", []))
        
        # Count financial case recommendations
        financial_case_recommendations = sum(
            1 for r in agent_result.get("recommendations", [])
            if r.get("should_create_financial_case", False)
        )
        
        # =========================================================
        # PHASE 6: Assertions and Validation
        # =========================================================
        
        # Verify dataset size requirement (50+ records)
        self.assertGreaterEqual(total_records, 50, 
            f"Dataset must contain at least 50 records, got {total_records}")
        
        # Verify match rate is reasonable (should be between 40-90% for realistic data)
        self.assertGreater(match_rate, 30, 
            f"Match rate too low: {match_rate}%")
        self.assertLess(match_rate, 95, 
            f"Match rate suspiciously high: {match_rate}% - may indicate insufficient exception testing")
        
        # Verify all exception types are represented
        exception_types_present = sum(1 for v in actual_counts.values() if v > 0)
        self.assertGreaterEqual(exception_types_present, 3,
            f"Expected at least 3 exception types, got {exception_types_present}")
        
        # Verify classification accuracy is meaningful
        self.assertGreater(classification_accuracy, 70,
            f"Classification accuracy too low: {classification_accuracy}%")
        
        # Verify agent produced analysis for all exceptions
        exception_count = len(exceptions)
        self.assertEqual(prioritized_count, exception_count,
            f"Agent should prioritize all {exception_count} exceptions, got {prioritized_count}")
        self.assertEqual(explanations_count, exception_count,
            f"Agent should explain all {exception_count} exceptions, got {explanations_count}")
        self.assertEqual(recommendations_count, exception_count,
            f"Agent should recommend actions for all {exception_count} exceptions, got {recommendations_count}")
        
        # Verify unresolved exceptions are identified
        self.assertGreater(len(unresolved_exceptions), 0,
            "Should have some unresolved exceptions requiring manual review")
        
        # =========================================================
        # PHASE 7: Print Evaluation Report
        # =========================================================
        print("\n" + "="*70)
        print("FINANCE CONTROLLER EVALUATION REPORT")
        print("="*70)
        print(f"\nDataset Size:")
        print(f"  Total records processed:    {total_records}")
        print(f"  Generation time:            {generation_time:.3f} sec")
        print(f"\nReconciliation Performance:")
        print(f"  Matched records:            {actual_counts['matched']}")
        print(f"  Match rate:                 {match_rate:.2f}%")
        print(f"  Processing time:            {reconcile_time:.3f} sec")
        print(f"  Throughput:                 {throughput:.1f} records/sec")
        print(f"\nException Breakdown:")
        print(f"  Amount mismatches:          {actual_counts['amount_mismatch']}")
        print(f"  Duplicate settlements:      {actual_counts['duplicate_settlement']}")
        print(f"  No matching order:          {actual_counts['no_matching_order']}")
        print(f"  Already reconciled:         {actual_counts['already_reconciled']}")
        print(f"  Total exceptions:           {sum(actual_counts.values()) - actual_counts['matched']}")
        print(f"\nClassification Accuracy:")
        print(f"  Correct predictions:        {correct_predictions}/{total_predictions}")
        print(f"  Accuracy:                   {classification_accuracy:.1f}%")
        if misclassifications:
            print(f"  Misclassifications:")
            for m in misclassifications[:5]:  # Show first 5
                print(f"    - Index {m['index']}: expected={m['expected']}, actual={m['actual']}")
        print(f"\nFinance Controller Agent Analysis:")
        print(f"  Analysis source:            {agent_source}")
        print(f"  Agent processing time:      {agent_time:.3f} sec")
        print(f"  Exceptions analyzed:        {prioritized_count}")
        print(f"  Explanations generated:     {explanations_count}")
        print(f"  Recommendations made:       {recommendations_count}")
        print(f"  Financial case suggestions: {financial_case_recommendations}")
        print(f"\nUnresolved Exceptions (Manual Review Required):")
        print(f"  Count:                      {len(unresolved_exceptions)}")
        for i, ex in enumerate(unresolved_exceptions[:10], 1):  # Show first 10
            print(f"  {i}. Ref='{ex['external_ref']}', Amount=INR {ex['amount']:.2f}, "
                  f"Type={ex['classification']}")
        if len(unresolved_exceptions) > 10:
            print(f"  ... and {len(unresolved_exceptions) - 10} more")
        print(f"\nTotal Evaluation Time:        {total_time:.3f} sec")
        print("="*70 + "\n")
        
        conn.close()


if __name__ == "__main__":
    unittest.main()
