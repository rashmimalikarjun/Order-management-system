import os
import csv
import io
import json
import random
import string
import urllib.request
import urllib.error
from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify, flash
from flask_cors import CORS
from datetime import datetime, timedelta
import sqlite3
from urllib.parse import urlencode
from functools import wraps
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {
    "1",
    "true",
    "yes",
}

DATABASE = os.environ.get("DATABASE_PATH", "catering.db")
UPI_ID = os.environ.get("UPI_ID", "your-upi-id@okbank")
UPI_NAME = os.environ.get("UPI_NAME", "Order Management System")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
UPLOAD_FOLDER = os.path.join("static", "uploads", "qr")
PAYMENT_PROOF_UPLOAD_FOLDER = os.path.join("static", "uploads", "payment_proofs")
ALLOWED_QR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_PAYMENT_PROOF_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}
APP_TIMEZONE = ZoneInfo("Asia/Kolkata")
DISPLAY_DATETIME_FORMAT = "%I:%M %p | %d %b %Y"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
FINANCIAL_CASE_STATUSES = ("Open", "Investigating", "Escalated", "Resolved", "Closed")
FINANCIAL_CASE_CLOSED_STATUSES = {"Resolved", "Closed"}
FINANCIAL_RISK_TIERS = ("Unscored", "Low", "Medium", "High", "Critical")
REASONING_APPROVAL_STATES = ("PENDING", "APPROVED", "REJECTED")
# Phase 5: lifecycle states for case_action rows. Existing rows (Phase 2-4) default
# to "completed" since they always represented a finished, one-shot event.
CASE_ACTION_STATUSES = ("pending", "completed", "overridden")

# Track 04 - AI Finance Controller: multi-source settlement reconciliation.
# Purely additive - does not read or write financial_case/case_* tables.
# Targets are topped up before every batch (not just seeded once) because a
# "matched" settlement closes its order to Paid, permanently removing it
# from the open pool - without a top-up, a second live demo click would
# find nothing left to match and the rate would collapse toward 0%.
RECON_TARGET_OPEN_WITH_REF = 29
RECON_TARGET_OPEN_WITHOUT_REF = 18
RECON_TARGET_PAID = 5
RECON_REFERENCE_AMOUNT_TOLERANCE = 0.01
RECON_FALLBACK_AMOUNT_TOLERANCE = 1.00
RECON_SETTLEMENT_SOURCES = (
    "Razorpay Settlement",
    "Bank NEFT",
    "Bank IMPS",
    "UPI Settlement File",
)

# ---------------------------------------------------------------------------
# Demo data: corporate catering menu + realistic historical order seeding.
# Purely additive, cosmetic/demo data - not application logic. Menu list
# feeds the existing "insert default menu items if empty" block in init_db().
# (category, emoji, name, price)
# ---------------------------------------------------------------------------
DEMO_MENU_ITEMS = (
    ("breakfast", "🍘", "Idli & Vada Combo", 89),
    ("breakfast", "🫓", "Masala Dosa", 79),
    ("breakfast", "🥣", "Vegetable Upma", 69),
    ("breakfast", "🍛", "Pongal & Vada", 85),
    ("breakfast", "🍽️", "Poori Masala", 75),
    ("breakfast", "🍱", "South Indian Breakfast Combo", 95),
    ("breakfast", "🥐", "Continental Breakfast Box", 145),
    ("lunch", "🍽️", "South Indian Veg Meals", 130),
    ("lunch", "🍛", "North Indian Thali", 165),
    ("lunch", "🥗", "Executive Veg Lunch", 175),
    ("lunch", "🍗", "Executive Non-Veg Lunch", 215),
    ("lunch", "🧀", "Paneer Butter Masala Combo", 195),
    ("lunch", "🍚", "Chicken Biryani Meal", 225),
    ("lunch", "🌾", "Vegetable Biryani Meal", 165),
    ("lunch", "🍱", "Corporate Lunch Box", 220),
    ("snacks", "🥟", "Samosa & Tea", 45),
    ("snacks", "🥪", "Veg Sandwich", 55),
    ("snacks", "🌯", "Paneer Roll", 65),
    ("snacks", "🌯", "Chicken Roll", 85),
    ("snacks", "☕", "Cutlet & Coffee", 50),
    ("snacks", "🍪", "Biscuit & Tea Combo", 35),
    ("beverages", "☕", "Masala Tea", 20),
    ("beverages", "☕", "Filter Coffee", 25),
    ("beverages", "🍵", "Green Tea", 30),
    ("beverages", "🍋", "Fresh Lime", 35),
    ("beverages", "🥛", "Buttermilk", 30),
    ("beverages", "💧", "Packaged Water", 20),
    ("events", "🍛", "Corporate Vegetarian Buffet", 285),
    ("events", "🍽️", "Corporate Mixed Buffet", 345),
    ("events", "🍿", "Meeting Snack Package", 125),
    ("events", "🍱", "Conference Lunch Package", 245),
)

# Synthetic corporate clients for demo orders only - not real customers.
DEMO_CORPORATE_CLIENTS = (
    "Infosys - Electronic City",
    "Wipro - Sarjapur",
    "TCS - Electronic City",
    "Accenture - Whitefield",
    "Deloitte - Outer Ring Road",
    "IBM - Manyata Tech Park",
    "EY - Whitefield",
    "Bosch - Adugodi",
    "SAP Labs - Whitefield",
    "Oracle - Devanahalli",
    "Target - Koramangala",
    "Cisco - Marathahalli",
    "Capgemini - Whitefield",
)
# A few flagship clients recur more often than the rest (repeat contracts).
DEMO_CORPORATE_CLIENT_WEIGHTS = (4, 4, 3, 3, 2, 2, 2, 1, 2, 1, 1, 2, 2)

DEMO_CONTACT_FIRST_NAMES = (
    "Rohan", "Priya", "Anil", "Kavya", "Suresh", "Ananya", "Vikram", "Meera",
    "Arjun", "Divya", "Karthik", "Sneha", "Manoj", "Pooja", "Rajesh", "Nisha",
    "Sandeep", "Lakshmi", "Varun", "Ritu", "Naveen", "Shalini", "Deepak", "Aparna",
)
DEMO_CONTACT_LAST_NAMES = (
    "Sharma", "Reddy", "Iyer", "Nair", "Gupta", "Rao", "Menon", "Kulkarni",
    "Patil", "Krishnan", "Verma", "Pillai", "Shetty", "Bhat", "Desai", "Joshi",
)

DEMO_QUANTITY_CHOICES = (10, 15, 20, 25, 35, 40, 50, 75, 100, 150, 250, 300)
DEMO_QUANTITY_WEIGHTS = (14, 14, 12, 12, 10, 10, 10, 6, 5, 4, 2, 1)
DEMO_ADDON_QUANTITY_CHOICES = (10, 15, 20, 25, 35, 40)
DEMO_ADDON_QUANTITY_WEIGHTS = (20, 20, 20, 16, 14, 10)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PAYMENT_PROOF_UPLOAD_FOLDER, exist_ok=True)


def running_on_render():
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))


def admin_uses_default_credentials():
    return (
        ADMIN_USERNAME == DEFAULT_ADMIN_USERNAME
        and ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD
    )


def admin_login_enabled():
    return not (running_on_render() and admin_uses_default_credentials())


def log_startup_warnings():
    if running_on_render() and not os.path.isabs(DATABASE):
        print(
            "WARNING: DATABASE_PATH is using a relative path. On Render this is ephemeral. "
            "Use a persistent disk path like /var/data/catering.db."
        )
    if not app.debug and admin_uses_default_credentials():
        print(
            "WARNING: Default admin credentials are configured. "
            "Set ADMIN_USERNAME and ADMIN_PASSWORD before using admin login."
        )


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def current_local_datetime():
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def now_string():
    return current_local_datetime().strftime(DISPLAY_DATETIME_FORMAT)


def parse_order_datetime(order_time):
    if not order_time:
        return datetime.min

    try:
        return datetime.strptime(order_time, DISPLAY_DATETIME_FORMAT)
    except (TypeError, ValueError):
        normalized_time = str(order_time).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized_time)
        except ValueError:
            return datetime.min

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(APP_TIMEZONE).replace(tzinfo=None)
        return parsed


def is_follow_up_overdue(follow_up_due_at, now_dt=None):
    """Missing/unparseable follow_up_due_at is never overdue (matches the
    datetime.min sentinel already used by admin_finance() for the same check)."""
    if now_dt is None:
        now_dt = current_local_datetime()
    due = parse_order_datetime(follow_up_due_at)
    return due != datetime.min and due <= now_dt


def format_display_datetime(order_time):
    parsed = parse_order_datetime(order_time)
    if parsed == datetime.min:
        return order_time or ""
    return parsed.strftime(DISPLAY_DATETIME_FORMAT)


def normalize_datetime_fields(row, fields):
    if row is None:
        return None

    normalized = dict(row)
    for field in fields:
        if field in normalized:
            normalized[field] = format_display_datetime(normalized.get(field))
    return normalized


def build_upi_link(amount, note):
    params = {
        "pa": UPI_ID,
        "pn": UPI_NAME,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note[:50],
    }
    return "upi://pay?" + urlencode(params)


def build_qr_url(data):
    return "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + urlencode({"": data})[1:]


def allowed_qr_file(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_QR_EXTENSIONS


def allowed_payment_proof_file(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_PAYMENT_PROOF_EXTENSIONS


def save_payment_proof(file_storage, order_id):
    if file_storage is None or not file_storage.filename:
        return ""

    filename = secure_filename(file_storage.filename)
    if not allowed_payment_proof_file(filename):
        return None

    ext = filename.rsplit(".", 1)[1].lower()
    final_filename = f"payment_proof_order_{order_id}_{int(datetime.now().timestamp())}.{ext}"
    save_path = os.path.join(PAYMENT_PROOF_UPLOAD_FOLDER, final_filename)
    file_storage.save(save_path)
    return os.path.join("uploads", "payment_proofs", final_filename).replace("\\", "/")


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def record_audit(conn, actor_type, actor_name, action, details=""):
    conn.execute(
        """
        INSERT INTO audit_logs (actor_type, actor_name, action, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (actor_type, actor_name, action, details, now_string()),
    )


def record_ai_analysis_failure(conn, case_id, initiated_by, trigger, reason):
    """Persist a Gemini/validation failure via the existing case_action table so it
    survives a restart, instead of only being printed to the server log. Uses the
    same insertion shape as every other case_action write in this module. `reason`
    must already have any secrets redacted by the caller."""
    outcome = f"AI analysis fallback to deterministic scoring (trigger={trigger}). {reason}"
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, "ai_analysis_fallback", initiated_by, outcome[:500], now_string()),
    )


def record_status_history(conn, order_id, status, changed_by, note=""):
    conn.execute(
        """
        INSERT INTO order_status_history (order_id, status, changed_at, changed_by, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (order_id, status, now_string(), changed_by, note),
    )


def get_order_timeline(conn, order_id):
    return conn.execute(
        """
        SELECT * FROM order_status_history
        WHERE order_id = ?
        ORDER BY id ASC
        """,
        (order_id,),
    ).fetchall()


def normalize_financial_choice(value, allowed_values, default_value):
    value = (value or "").strip()
    return value if value in allowed_values else default_value


def normalize_risk_tier(value, default_value="Unscored"):
    value = (value or "").strip()
    for tier in FINANCIAL_RISK_TIERS:
        if value.lower() == tier.lower():
            return tier
    return default_value


def parse_percentage(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(100.0, parsed))


def normalize_follow_up_datetime(value):
    value = (value or "").strip()
    if not value:
        return ""

    try:
        return datetime.fromisoformat(value).strftime(DISPLAY_DATETIME_FORMAT)
    except ValueError:
        return value


def row_to_dict(row):
    return dict(row) if row is not None else {}


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_finance_order_items(conn, order):
    order_data = row_to_dict(order)
    if not order_data.get("id"):
        return []

    order_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC",
        (order_data["id"],),
    ).fetchall()
    if order_items:
        return [
            {
                **dict(item),
                "source": "order_items",
                "is_legacy_fallback": False,
                "has_reliable_price": True,
                "pricing_note": "",
            }
            for item in order_items
        ]

    menu_summary = (order_data.get("menu") or "").strip()
    quantity = safe_int(order_data.get("quantity"), 0)
    if not menu_summary and quantity <= 0:
        return []

    return [
        {
            "id": None,
            "order_id": order_data["id"],
            "menu_item_id": None,
            "item_name": menu_summary or "Legacy order summary",
            "item_price": None,
            "quantity": quantity,
            "subtotal": None,
            "source": "legacy_order_fields",
            "is_legacy_fallback": True,
            "has_reliable_price": False,
            "pricing_note": "Legacy order row has menu/quantity but no item-level price.",
        }
    ]


def summarize_finance_order_items(items):
    return {
        "count": len(items),
        "source": items[0]["source"] if items else "none",
        "has_live_order_items": any(not item.get("is_legacy_fallback") for item in items),
        "uses_legacy_fallback": any(item.get("is_legacy_fallback") for item in items),
        "has_reliable_line_prices": any(item.get("has_reliable_price") for item in items),
    }


def get_payment_analysis(order, items):
    order_data = row_to_dict(order)
    expected_amount = round(safe_float(order_data.get("total_price")), 2)
    payment_status = order_data.get("payment_status") or ""
    expected_amount_recorded = expected_amount > 0
    received_amount = expected_amount if payment_status == "Paid" and expected_amount_recorded else 0.0
    shortfall_amount = round(max(0.0, expected_amount - received_amount), 2)
    item_summary = summarize_finance_order_items(items)

    return {
        "expected_amount": expected_amount,
        "expected_amount_recorded": expected_amount_recorded,
        "received_amount": received_amount,
        "shortfall_amount": shortfall_amount,
        "payment_status": payment_status,
        "payment_method": order_data.get("payment_method") or "",
        "payment_reference": order_data.get("payment_reference") or "",
        "payment_proof_path": order_data.get("payment_proof_path") or "",
        "financial_data_incomplete": not expected_amount_recorded
        or item_summary["uses_legacy_fallback"]
        or not items,
        "zero_amount_note": (
            "total_price is recorded as 0; this is not treated as a verified zero-rupee order."
            if not expected_amount_recorded
            else ""
        ),
    }


def build_financial_case_evidence_snapshot(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return None

    finance_items = get_finance_order_items(conn, order)
    item_summary = summarize_finance_order_items(finance_items)
    payment_analysis = get_payment_analysis(order, finance_items)
    timeline = get_order_timeline(conn, order_id)
    audit_logs = conn.execute(
        "SELECT * FROM audit_logs WHERE details LIKE ? ORDER BY id DESC LIMIT 25",
        (f"%order_id={order_id}%",),
    ).fetchall()

    order_data = dict(order)
    payment_status = order_data.get("payment_status") or ""

    return {
        "phase": "Phase 2 AI foundation",
        "captured_at": now_string(),
        "order": {
            "id": order_data.get("id"),
            "username": order_data.get("username"),
            "contact_number": order_data.get("contact_number"),
            "menu": order_data.get("menu"),
            "quantity": order_data.get("quantity"),
            "status": order_data.get("status"),
            "time": order_data.get("time"),
            "status_time": order_data.get("status_time"),
        },
        "payment": {
            "total_price": payment_analysis["expected_amount"],
            "payment_method": order_data.get("payment_method"),
            "payment_status": payment_status,
            "payment_reference": order_data.get("payment_reference"),
            "payment_proof_path": order_data.get("payment_proof_path"),
            "expected_amount_recorded": payment_analysis["expected_amount_recorded"],
            "zero_amount_note": payment_analysis["zero_amount_note"],
        },
        "shortfall": {
            "expected_amount": payment_analysis["expected_amount"],
            "received_amount": payment_analysis["received_amount"],
            "estimated_shortfall": payment_analysis["shortfall_amount"],
            "needs_review": payment_analysis["shortfall_amount"] > 0
            or payment_analysis["financial_data_incomplete"],
            "basis": "Existing OMS order/payment fields only.",
        },
        "item_summary": item_summary,
        "items": finance_items,
        "order_items": [
            item for item in finance_items if not item.get("is_legacy_fallback")
        ],
        "legacy_items": [
            item for item in finance_items if item.get("is_legacy_fallback")
        ],
        "order_status_history": [dict(item) for item in timeline],
        "audit_logs": [dict(log) for log in audit_logs],
    }


def capture_financial_case_evidence(conn, case_id, order_id, evidence_type="order_snapshot"):
    snapshot = build_financial_case_evidence_snapshot(conn, order_id)
    if snapshot is None:
        return False

    conn.execute(
        """
        INSERT INTO case_evidence (case_id, evidence_type, evidence_snapshot, captured_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            case_id,
            evidence_type,
            json.dumps(snapshot, indent=2, sort_keys=True),
            now_string(),
        ),
    )
    return True


def risk_tier_from_score(score):
    if score >= 90:
        return "Critical"
    if score >= 65:
        return "High"
    if score >= 35:
        return "Medium"
    return "Low"


# =============================================================================
# Finance Controller Agent: Reconciliation Exception Analysis
# =============================================================================
# This agent analyzes already-persisted reconciliation batches and provides:
# - Batch-level metrics (total, matched, exceptions, match rate, amounts)
# - Exception prioritization based on financial impact and severity
# - Human-readable explanations for each exception
# - Recommended actions (investigate, verify reference, create financial case, etc.)
#
# SAFETY CONSTRAINTS:
# - Does NOT modify reconciliation records
# - Does NOT re-classify exceptions (uses deterministic classification as truth)
# - Does NOT auto-create/approve/dispatch financial cases
# - Does NOT invent financial facts (uses only actual DB data)
# - Falls back to deterministic analysis if Gemini is unavailable
# =============================================================================

def build_reconciliation_agent_prompt(batch, exceptions, order_data_map):
    """Build a prompt for analyzing reconciliation exceptions.
    
    Args:
        batch: dict with batch metadata (id, created_at, counts, match_rate, etc.)
        exceptions: list of exception records from reconciliation_settlements
        order_data_map: dict mapping order_id -> order details for linked orders
    
    Returns:
        str: Prompt text for Gemini API
    """
    exception_summary = []
    for ex in exceptions:
        order_info = ""
        if ex.get("order_id"):
            order = order_data_map.get(ex["order_id"], {})
            order_info = (
                f"Order #{ex['order_id']}: status={order.get('status', 'N/A')}, "
                f"payment_status={order.get('payment_status', 'N/A')}, "
                f"total_price=INR {order.get('total_price', 'N/A')}"
            )
        else:
            order_info = "No matching order found."
        
        exception_summary.append(
            f"- ID: {ex['id']}, Ref: '{ex['external_ref'] or '(blank)'}', "
            f"Amount: INR {ex['amount']:.2f}, Classification: {ex['classification']}, "
            f"{order_info}"
        )
    
    exceptions_text = "\n".join(exception_summary)
    
    return f"""You are a Finance Controller Agent analyzing reconciliation exceptions.
Analyze ONLY the supplied evidence. Do NOT invent facts. Do NOT assume missing evidence.
Do NOT re-classify exceptions - use the provided classification as ground truth.

Your task:
1. Report ACTUAL batch metrics from the data provided.
2. Prioritize exceptions by financial impact and severity.
3. Explain EACH exception using ONLY actual data (no fabrication).
4. Recommend an action for each exception.

CLASSIFICATION TYPES (use these exactly):
- matched: Settlement correctly matched order, order marked Paid.
- amount_mismatch: Reference matched but amounts differ (e.g., fee deduction).
- duplicate_settlement: Same order claimed twice in same batch.
- no_matching_order: Orphan settlement with no corresponding order.
- already_reconciled: Order was already Paid before this batch.

PRIORITY GUIDELINES:
- HIGH: Large monetary value, duplicate risk, corporate orders.
- MEDIUM: Moderate amounts, potential fee discrepancies.
- LOW: Small amounts, orphan payments awaiting order creation.

RECOMMENDED ACTIONS:
- "investigate": General investigation needed.
- "verify_reference": Check payment reference accuracy.
- "review_amount_mismatch": Verify fee agreement or rounding.
- "check_duplicate": Confirm if duplicate settlement is legitimate.
- "create_financial_case": Create financial case for human review (for significant issues).
- "await_order_creation": For orphan payments where order may be created later.

Return a strictly valid JSON object matching this schema exactly:
{{
    "batch_metrics": {{
        "total_records": <number>,
        "matched_records": <number>,
        "exception_records": <number>,
        "match_rate_percent": <number>,
        "reconciled_amount_inr": <number>,
        "unresolved_amount_inr": <number>
    }},
    "exception_breakdown": {{
        "amount_mismatch": <count>,
        "duplicate_settlement": <count>,
        "no_matching_order": <count>,
        "already_reconciled": <count>
    }},
    "prioritized_exceptions": [
        {{
            "settlement_id": <number>,
            "priority_rank": <number starting from 1>,
            "priority_level": "HIGH" | "MEDIUM" | "LOW",
            "financial_impact_inr": <number>,
            "reason_for_priority": "<string explaining why this priority>"
        }}
    ],
    "exception_explanations": [
        {{
            "settlement_id": <number>,
            "what_happened": "<string factual description>",
            "why_reconciliation_failed": "<string explanation based on classification>",
            "financial_impact": "<string describing monetary impact>",
            "relevant_order_info": "<string or 'No matching order'>"
        }}
    ],
    "recommendations": [
        {{
            "settlement_id": <number>,
            "recommended_action": "<string from allowed actions>",
            "action_justification": "<string explaining why this action>",
            "should_create_financial_case": <boolean>
        }}
    ]
}}

BATCH METADATA:
Batch ID: {batch['id']}
Created At: {batch['created_at']}
Triggered By: {batch['triggered_by']}
Total Records: {batch['record_count']}
Matched: {batch['matched_count']}
Match Rate: {batch['match_rate']}%

EXCEPTIONS ({len(exceptions)} total):
{exceptions_text}

Remember:
- Use ONLY the data provided above.
- Do NOT fabricate amounts, references, or order details.
- If data is missing, state "Data not available" rather than guessing.
- Financial case creation should be recommended only for significant issues requiring human review."""


def validate_reconciliation_agent_response(ai_data):
    """Validate the AI agent's response schema.
    
    Args:
        ai_data: dict parsed from AI response
    
    Returns:
        bool: True if valid, False otherwise
    """
    if not isinstance(ai_data, dict):
        return False
    
    # Check batch_metrics
    if "batch_metrics" not in ai_data or not isinstance(ai_data["batch_metrics"], dict):
        return False
    batch_metrics = ai_data["batch_metrics"]
    required_metrics = ["total_records", "matched_records", "exception_records", 
                        "match_rate_percent", "reconciled_amount_inr", "unresolved_amount_inr"]
    for key in required_metrics:
        if key not in batch_metrics:
            return False
        if not isinstance(batch_metrics[key], (int, float)):
            return False
    
    # Check exception_breakdown
    if "exception_breakdown" not in ai_data or not isinstance(ai_data["exception_breakdown"], dict):
        return False
    breakdown = ai_data["exception_breakdown"]
    required_breakdown = ["amount_mismatch", "duplicate_settlement", "no_matching_order", "already_reconciled"]
    for key in required_breakdown:
        if key not in breakdown:
            return False
        if not isinstance(breakdown[key], int):
            return False
    
    # Check prioritized_exceptions
    if "prioritized_exceptions" not in ai_data or not isinstance(ai_data["prioritized_exceptions"], list):
        return False
    for item in ai_data["prioritized_exceptions"]:
        if not isinstance(item, dict):
            return False
        if "settlement_id" not in item or "priority_rank" not in item:
            return False
        if "priority_level" not in item or item["priority_level"] not in ["HIGH", "MEDIUM", "LOW"]:
            return False
        if "financial_impact_inr" not in item:
            return False
        if "reason_for_priority" not in item or not isinstance(item["reason_for_priority"], str):
            return False
    
    # Check exception_explanations
    if "exception_explanations" not in ai_data or not isinstance(ai_data["exception_explanations"], list):
        return False
    for item in ai_data["exception_explanations"]:
        if not isinstance(item, dict):
            return False
        required_keys = ["settlement_id", "what_happened", "why_reconciliation_failed", 
                         "financial_impact", "relevant_order_info"]
        for key in required_keys:
            if key not in item:
                return False
        # settlement_id can be int, others must be strings
        if not isinstance(item["what_happened"], str):
            return False
        if not isinstance(item["why_reconciliation_failed"], str):
            return False
        if not isinstance(item["financial_impact"], str):
            return False
        if not isinstance(item["relevant_order_info"], str):
            return False
    
    # Check recommendations
    if "recommendations" not in ai_data or not isinstance(ai_data["recommendations"], list):
        return False
    for item in ai_data["recommendations"]:
        if not isinstance(item, dict):
            return False
        required_keys = ["settlement_id", "recommended_action", "action_justification", 
                         "should_create_financial_case"]
        for key in required_keys:
            if key not in item:
                return False
        if not isinstance(item["recommended_action"], str):
            return False
        if not isinstance(item["action_justification"], str):
            return False
        if not isinstance(item["should_create_financial_case"], bool):
            return False
    
    return True


def evaluate_reconciliation_batch_deterministic(batch, exceptions, order_data_map):
    """Deterministic fallback analysis when Gemini is unavailable.
    
    This function produces results from ACTUAL reconciliation data only.
    It NEVER fabricates values, amounts, or classifications.
    
    Args:
        batch: dict with batch metadata
        exceptions: list of exception records
        order_data_map: dict mapping order_id -> order details
    
    Returns:
        dict: Analysis result matching AI response schema
    """
    # Compute actual metrics from batch data
    total_records = batch["record_count"]
    matched_records = batch["matched_count"]
    exception_records = total_records - matched_records
    match_rate = batch["match_rate"]
    
    # Calculate reconciled amount from matched settlements
    # We need to fetch matched settlements to get their amounts
    # For now, use exception data only (matched amounts would require additional query)
    reconciled_amount = 0.0  # Would need to sum matched settlement amounts
    unresolved_amount = sum(ex["amount"] for ex in exceptions)
    
    # Build exception breakdown from actual classifications
    breakdown = {
        "amount_mismatch": 0,
        "duplicate_settlement": 0,
        "no_matching_order": 0,
        "already_reconciled": 0
    }
    for ex in exceptions:
        cls = ex["classification"]
        if cls in breakdown:
            breakdown[cls] += 1
    
    # Prioritize exceptions deterministically by financial impact (amount) primarily,
    # with adjustments for high-severity classifications like duplicates.
    # Higher amount = higher priority (should appear first).
    # Duplicates are always HIGH priority regardless of amount.
    
    sorted_exceptions = sorted(exceptions, key=lambda x: -x.get("amount", 0))  # Sort by amount descending
    prioritized = []
    for rank, ex in enumerate(sorted_exceptions, 1):
        amount = ex.get("amount", 0)
        # Use 'id' if available (from DB), otherwise use a synthetic ID based on index
        ex_id = ex.get("id") if ex.get("id") is not None else rank
        
        # Determine priority level based on amount thresholds
        if amount >= 1000:
            priority_level = "HIGH"
            reason = f"High monetary value (INR {amount:.2f})"
        elif amount >= 200:
            priority_level = "MEDIUM"
            reason = f"Moderate monetary value (INR {amount:.2f})"
        else:
            priority_level = "LOW"
            reason = f"Lower monetary value (INR {amount:.2f})"
        
        # Adjust priority based on classification severity
        # Duplicates are always HIGH priority (fraud risk)
        if ex.get("classification") == "duplicate_settlement":
            priority_level = "HIGH"
            reason = "Duplicate settlement requires immediate investigation"
        # Large orphan payments need urgent attention
        elif ex.get("classification") == "no_matching_order" and amount >= 500:
            priority_level = "HIGH"
            reason = f"Large orphan payment (INR {amount:.2f}) needs order lookup"
        
        prioritized.append({
            "settlement_id": ex_id,
            "priority_rank": rank,
            "priority_level": priority_level,
            "financial_impact_inr": amount,
            "reason_for_priority": reason
        })
    
    # Generate explanations for each exception
    explanations = []
    for ex in exceptions:
        # Use 'id' if available (from DB), otherwise use 0 as placeholder
        ex_id = ex.get("id") if ex.get("id") is not None else 0
        order = order_data_map.get(ex.get("order_id"), {}) if ex.get("order_id") else {}
        
        what_happened = (
            f"Settlement of INR {ex.get('amount', 0):.2f} with reference '{ex.get('external_ref') or '(blank)'}' "
            f"from source '{ex.get('source', 'N/A')}' was processed."
        )
        
        classification = ex.get("classification", "unknown")
        reason_text = ex.get("reason", "Unknown reason")
        
        why_failed = {
            "amount_mismatch": (
                f"Reference matched an order, but settled amount differs from order total. "
                f"{reason_text}"
            ),
            "duplicate_settlement": (
                f"This settlement references an order that was already reconciled earlier in the same batch. "
                f"{reason_text}"
            ),
            "no_matching_order": (
                f"No order found with matching payment reference or amount within tolerance. "
                f"{reason_text}"
            ),
            "already_reconciled": (
                f"The referenced order was already marked Paid before this batch ran. "
                f"{reason_text}"
            )
        }.get(classification, reason_text)
        
        financial_impact = f"Unresolved amount: INR {ex.get('amount', 0):.2f}"
        relevant_order_info = (
            f"Order #{ex['order_id']}: status={order.get('status', 'N/A')}, "
            f"payment_status={order.get('payment_status', 'N/A')}, "
            f"total=INR {order.get('total_price', 'N/A')}"
        ) if order else "No matching order found."
        
        explanations.append({
            "settlement_id": ex_id,
            "what_happened": what_happened,
            "why_reconciliation_failed": why_failed,
            "financial_impact": financial_impact,
            "relevant_order_info": relevant_order_info
        })
    
    # Generate recommendations
    recommendations = []
    for ex in exceptions:
        # Use 'id' if available (from DB), otherwise use 0 as placeholder
        ex_id = ex.get("id") if ex.get("id") is not None else 0
        
        action_map = {
            "amount_mismatch": ("review_amount_mismatch", 
                                "Verify fee agreement or check for rounding discrepancies"),
            "duplicate_settlement": ("check_duplicate", 
                                     "Confirm if this is a legitimate duplicate or system error"),
            "no_matching_order": ("investigate", 
                                  "Search for order by customer details or await order creation"),
            "already_reconciled": ("verify_reference", 
                                   "Check if this is a duplicate settlement request")
        }
        
        classification = ex.get("classification", "unknown")
        action, justification = action_map.get(classification, 
                                                ("investigate", "Manual review required"))
        
        # Recommend financial case for high-value or duplicate exceptions
        should_create_case = (
            ex.get("amount", 0) >= 1000 or 
            classification == "duplicate_settlement" or
            (classification == "no_matching_order" and ex.get("amount", 0) >= 500)
        )
        
        recommendations.append({
            "settlement_id": ex_id,
            "recommended_action": action,
            "action_justification": justification,
            "should_create_financial_case": should_create_case
        })
    
    return {
        "analysis_source": "deterministic_v1",
        "batch_metrics": {
            "total_records": total_records,
            "matched_records": matched_records,
            "exception_records": exception_records,
            "match_rate_percent": match_rate,
            "reconciled_amount_inr": reconciled_amount,
            "unresolved_amount_inr": unresolved_amount
        },
        "exception_breakdown": breakdown,
        "prioritized_exceptions": prioritized,
        "exception_explanations": explanations,
        "recommendations": recommendations
    }


def evaluate_financial_case(order, finance_items, evidence_rows):
    order_data = row_to_dict(order)
    if not order_data:
        return {
            "analysis_source": "deterministic_v1",
            "risk_tier": "Unscored",
            "risk_score": 0.0,
            "confidence": 0.0,
            "hypothesis": "No linked OMS order was available for analysis.",
            "chosen_action": "Review the financial case linkage before continuing.",
            "rejected_alternatives": "LLM reasoning not invoked; no valid order context was available.",
            "requires_human_approval": True,
            "reasoning_summary": "Deterministic fallback applied."
        }

    item_summary = summarize_finance_order_items(finance_items)
    payment_analysis = get_payment_analysis(order, finance_items)
    order_status = order_data.get("status") or ""
    payment_status = order_data.get("payment_status") or ""
    delivered = order_status == "Delivered"
    paid = payment_status == "Paid"
    non_paid = payment_status != "Paid"
    expected_amount = payment_analysis["expected_amount"]
    expected_recorded = payment_analysis["expected_amount_recorded"]
    has_payment_reference = bool(payment_analysis["payment_reference"])
    has_payment_proof = bool(payment_analysis["payment_proof_path"])
    evidence_count = len(evidence_rows)

    if paid:
        score = 8.0
        confidence = 90.0 if expected_recorded else 65.0
        hypothesis = "The linked OMS order is marked Paid."
        chosen_action = "No finance shortfall action is needed unless payment proof or reconciliation is disputed."
    elif delivered and non_paid and expected_recorded:
        score = 84.0
        confidence = 85.0
        hypothesis = (
            f"Delivered order remains {payment_status} with a recorded expected amount of "
            f"INR {expected_amount:.2f}."
        )
        chosen_action = "Verify payment evidence, contact the customer, and escalate if the shortfall remains unresolved."
    elif delivered and non_paid:
        score = 70.0
        confidence = 55.0
        hypothesis = (
            f"Delivered order remains {payment_status}, but the expected amount is not reliably recorded. "
            "Legacy menu/quantity data may identify what was ordered, but it does not prove the amount owed."
        )
        chosen_action = "Confirm the expected amount from source records, then follow up on the unpaid delivered order."
    elif non_paid and expected_recorded:
        score = 42.0
        confidence = 72.0
        hypothesis = (
            f"Order is not yet delivered and payment is {payment_status}; a recorded amount exists."
        )
        chosen_action = "Monitor until delivery or payment confirmation before escalating."
    elif non_paid:
        score = 36.0
        confidence = 45.0
        hypothesis = (
            f"Order payment is {payment_status}, but financial data is incomplete."
        )
        chosen_action = "Review the order record and capture missing payment/amount evidence."
    else:
        score = 15.0
        confidence = 55.0
        hypothesis = "The order does not currently match a delivered unpaid shortfall pattern."
        chosen_action = "Keep the case open only if there is external evidence of a shortfall."

    if delivered and non_paid and not has_payment_reference and not has_payment_proof:
        score += 5.0
    if item_summary["uses_legacy_fallback"]:
        confidence -= 10.0
    if not item_summary["has_live_order_items"]:
        confidence -= 5.0
    if not expected_recorded:
        confidence -= 10.0
    if evidence_count == 0:
        confidence -= 10.0

    score = parse_percentage(score)
    confidence = parse_percentage(confidence)
    financial_notes = []
    if item_summary["uses_legacy_fallback"]:
        financial_notes.append("legacy_order_fields_used")
    if not expected_recorded:
        financial_notes.append("expected_amount_not_recorded")
    if not has_payment_reference:
        financial_notes.append("payment_reference_missing")
    if not has_payment_proof:
        financial_notes.append("payment_proof_missing")

    return {
        "analysis_source": "deterministic_v1",
        "risk_tier": risk_tier_from_score(score),
        "risk_score": score,
        "confidence": confidence,
        "hypothesis": hypothesis,
        "chosen_action": chosen_action,
        "rejected_alternatives": (
            "LLM reasoning not invoked; deterministic analysis used. "
            f"Evidence flags: {', '.join(financial_notes) if financial_notes else 'none'}."
        ),
        "requires_human_approval": True,
        "reasoning_summary": "Deterministic analysis applied based on core OMS parameters."
    }


def redact_secret(text, secret):
    """Strip a known secret value (e.g. an API key embedded in a request URL)
    out of any failure text before it is ever persisted or logged further."""
    if not text:
        return ""
    text = str(text)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def call_gemini_api(prompt, api_key, model_name):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    headers = {'Content-Type': 'application/json'}
    data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.loads(response.read().decode('utf-8'))
        return result["candidates"][0]["content"]["parts"][0]["text"]


def build_ai_prompt(snapshot):
    return f"""You are a financial control assistant for an Order Management System.
Analyze only the supplied evidence. Do not invent facts. Do not assume missing evidence.
Separate observed facts from inference. Recommend the safest appropriate next action.
When evidence is insufficient, explicitly state that evidence is insufficient.

The AI must distinguish between:
1. payment definitely unresolved
2. payment possibly completed but evidence missing
3. payment confirmed
4. data inconsistency

Return a strictly valid JSON object matching this schema exactly:
{{
    "risk_tier": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
    "risk_score": <number 0-100>,
    "confidence": <number 0-100>,
    "hypothesis": "<string detailed hypothesis>",
    "recommended_action": "<string recommended next step>",
    "rejected_alternatives": ["<string>", "<string>"],
    "requires_human_approval": <boolean>,
    "reasoning_summary": "<string brief summary>"
}}

Evidence Snapshot:
{json.dumps(snapshot, indent=2)}
"""


def validate_gemini_response(ai_data):
    if not isinstance(ai_data, dict):
        return False

    required_keys = [
        "risk_tier", "risk_score", "confidence", "hypothesis",
        "recommended_action", "rejected_alternatives",
        "requires_human_approval", "reasoning_summary"
    ]
    for key in required_keys:
        if key not in ai_data:
            return False

    # 4. risk_tier MUST be exactly one of the allowed strings
    if ai_data["risk_tier"] not in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
        return False

    # 5 & 6. risk_score and confidence MUST be numbers between 0 and 100
    for key in ["risk_score", "confidence"]:
        val = ai_data[key]
        if type(val) is bool:
            return False
        if not isinstance(val, (int, float)):
            return False
        if val != val or val == float('inf') or val == float('-inf'):
            return False
        if not (0 <= val <= 100):
            return False

    # 7, 8 & 11. non-empty strings
    for key in ["hypothesis", "recommended_action", "reasoning_summary"]:
        val = ai_data[key]
        if not isinstance(val, str) or not val.strip():
            return False

    # 9. rejected_alternatives MUST be a list of strings
    rejected = ai_data["rejected_alternatives"]
    if not isinstance(rejected, list):
        return False
    if not all(isinstance(item, str) for item in rejected):
        return False

    # 10. requires_human_approval MUST be a strict boolean
    req_approval = ai_data["requires_human_approval"]
    if type(req_approval) is not bool:
        return False

    return True


def save_financial_case_analysis(conn, case, result, initiated_by, trigger):
    created_at = now_string()
    case_id = case["id"]
    risk_tier = normalize_risk_tier(result.get("risk_tier"), "Unscored")
    risk_score = parse_percentage(result.get("risk_score"), 0.0)
    confidence = parse_percentage(result.get("confidence"), 0.0)
    evidence_snapshot_id = result.get("evidence_snapshot_id")
    reasoning_cursor = conn.execute(
        """
        INSERT INTO case_reasoning (
            case_id, evidence_snapshot_id, hypothesis, risk_tier, risk_score, confidence,
            chosen_action, rejected_alternatives, created_at,
            requires_human_approval, reasoning_summary, analysis_source,
            approval_state, reviewed_at, reviewed_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            evidence_snapshot_id,
            result["hypothesis"],
            risk_tier,
            risk_score,
            confidence,
            result["chosen_action"],
            result["rejected_alternatives"],
            created_at,
            1,
            result.get("reasoning_summary", ""),
            result.get("analysis_source", "deterministic_v1"),
            "PENDING",
            "",
            "",
        ),
    )
    reasoning_id = reasoning_cursor.lastrowid
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            case_id,
            "analysis_run",
            initiated_by,
            (
                f"[{result.get('analysis_source', 'deterministic_v1').upper()}] via {trigger}: "
                f"recommendation pending review: {risk_tier} ({risk_score:.1f}, "
                f"confidence {confidence:.1f}). "
                f"Action: {result['chosen_action']}"
            ),
            created_at,
        ),
    )
    record_audit(
        conn,
        "admin",
        initiated_by,
        "financial_case_analysis_run",
        (
            f"financial_case_id={case_id}, reasoning_id={reasoning_id}, order_id={case['order_id']}, "
            f"source={result.get('analysis_source', 'deterministic_v1')}, trigger={trigger}, "
            f"recommended_risk_tier={risk_tier}, risk_score={risk_score:.1f}, "
            f"confidence={confidence:.1f}, approval_state=PENDING"
        ),
    )
    return reasoning_id


def analyze_financial_case(conn, case_id, initiated_by=ADMIN_USERNAME, trigger="manual"):
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return None

    order = conn.execute("SELECT * FROM orders WHERE id = ?", (case["order_id"],)).fetchone()
    finance_items = get_finance_order_items(conn, order)
    evidence_rows = conn.execute(
        "SELECT * FROM case_evidence WHERE case_id = ? ORDER BY id DESC",
        (case_id,),
    ).fetchall()
    latest_evidence = evidence_rows[0] if evidence_rows else None
    
    result = None
    gemini_key = os.environ.get("GEMINI_API_KEY")
    
    if gemini_key:
        try:
            model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            
            if evidence_rows:
                snapshot = json.loads(evidence_rows[0]["evidence_snapshot"])
            else:
                snapshot = build_financial_case_evidence_snapshot(conn, order["id"])
                
            prompt = build_ai_prompt(snapshot)
            ai_text = call_gemini_api(prompt, gemini_key, model_name)
            
            # STRICT VALIDATION PIPELINE
            try:
                ai_data = json.loads(ai_text)
            except Exception:
                ai_data = None
            
            if ai_data is not None and validate_gemini_response(ai_data):
                result = {
                    "analysis_source": "gemini_ai_v1",
                    "risk_tier": normalize_risk_tier(ai_data["risk_tier"], "Unscored"),
                    "risk_score": float(ai_data["risk_score"]),
                    "confidence": float(ai_data["confidence"]),
                    "hypothesis": ai_data["hypothesis"].strip(),
                    "chosen_action": ai_data["recommended_action"].strip(),
                    "rejected_alternatives": json.dumps(ai_data["rejected_alternatives"]),
                    "requires_human_approval": ai_data["requires_human_approval"],
                    "reasoning_summary": ai_data["reasoning_summary"].strip()
                }
            else:
                print("WARNING: AI Validation Failed. Safely falling back to deterministic.")
                record_ai_analysis_failure(
                    conn, case_id, initiated_by, trigger,
                    "Reason: AI response failed schema validation.",
                )
                result = None
                
        except Exception as e:
            failure_reason = redact_secret(str(e), gemini_key)
            print(f"WARNING: AI Analysis Failed ({failure_reason}). Safely falling back to deterministic.")
            record_ai_analysis_failure(
                conn, case_id, initiated_by, trigger,
                f"Reason: {failure_reason}",
            )
            result = None

    # Deterministic fallback seamlessly covers validation failures, connection issues, or missing API keys.
    if result is None:
        result = evaluate_financial_case(order, finance_items, evidence_rows)

    result["evidence_snapshot_id"] = latest_evidence["id"] if latest_evidence else None
    result["risk_tier"] = normalize_risk_tier(result.get("risk_tier"), "Unscored")
    result["reasoning_id"] = save_financial_case_analysis(conn, case, result, initiated_by, trigger)
    return result


def create_financial_case_for_order(conn, order_id, initiated_by):
    order = conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return None, "order_not_found"

    existing = conn.execute(
        "SELECT id FROM financial_case WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if existing:
        return existing["id"], "already_exists"

    created_at = now_string()
    cursor = conn.execute(
        """
        INSERT INTO financial_case (
            order_id, status, risk_tier, risk_score, confidence,
            created_at, updated_at, resolved_at, follow_up_due_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            "Open",
            "Unscored",
            0.0,
            0.0,
            created_at,
            created_at,
            "",
            "",
        ),
    )
    case_id = cursor.lastrowid
    capture_financial_case_evidence(conn, case_id, order_id, "initial_order_snapshot")
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            case_id,
            "case_created",
            initiated_by,
            "Financial case opened for existing OMS order.",
            created_at,
        ),
    )
    analyze_financial_case(conn, case_id, initiated_by, "case_creation")
    return case_id, "created"


def latest_approvable_reasoning_id(conn, case_id):
    row = conn.execute(
        """
        SELECT id FROM case_reasoning
        WHERE case_id = ?
          AND requires_human_approval = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    return row["id"] if row else None


def review_financial_case_reasoning(conn, case_id, reasoning_id, decision, reviewer):
    if decision not in REASONING_APPROVAL_STATES[1:]:
        return "invalid_decision"

    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return "case_not_found"

    reasoning = conn.execute(
        "SELECT * FROM case_reasoning WHERE id = ?",
        (reasoning_id,),
    ).fetchone()
    if reasoning is None:
        return "reasoning_not_found"
    if reasoning["case_id"] != case_id:
        return "reasoning_case_mismatch"
    if not reasoning["requires_human_approval"]:
        return "reasoning_not_approvable"
    if reasoning["approval_state"] != "PENDING":
        return "reasoning_not_pending"

    latest_reasoning_id = latest_approvable_reasoning_id(conn, case_id)
    if decision == "APPROVED" and reasoning_id != latest_reasoning_id:
        record_audit(
            conn,
            "admin",
            reviewer,
            "financial_case_reasoning_approval_blocked",
            (
                f"financial_case_id={case_id}, attempted_reasoning_id={reasoning_id}, "
                f"latest_reasoning_id={latest_reasoning_id}, order_id={case['order_id']}, "
                "reason=stale_reasoning"
            ),
        )
        return "stale_reasoning"

    reviewed_at = now_string()
    risk_tier = normalize_risk_tier(reasoning["risk_tier"], "Unscored")
    risk_score = parse_percentage(reasoning["risk_score"], 0.0)
    confidence = parse_percentage(reasoning["confidence"], 0.0)

    if decision == "APPROVED":
        conn.execute(
            """
            UPDATE financial_case
            SET risk_tier = ?,
                risk_score = ?,
                confidence = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (risk_tier, risk_score, confidence, reviewed_at, case_id),
        )
        action_type = "reasoning_approved"
        audit_action = "financial_case_reasoning_approved"
        outcome = (
            f"Approved reasoning #{reasoning_id}: {risk_tier} "
            f"({risk_score:.1f}, confidence {confidence:.1f})."
        )
        route_outcome = "approved"
    else:
        action_type = "reasoning_rejected"
        audit_action = "financial_case_reasoning_rejected"
        outcome = (
            f"Rejected reasoning #{reasoning_id}: {risk_tier} "
            f"({risk_score:.1f}, confidence {confidence:.1f})."
        )
        route_outcome = "rejected"

    conn.execute(
        """
        UPDATE case_reasoning
        SET approval_state = ?,
            reviewed_at = ?,
            reviewed_by = ?
        WHERE id = ?
        """,
        (decision, reviewed_at, reviewer, reasoning_id),
    )
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, action_type, reviewer, outcome, reviewed_at),
    )
    if decision == "APPROVED":
        # Phase 5: approving reasoning queues its recommended action for controlled,
        # mocked/internal dispatch instead of applying it automatically.
        queue_controlled_action(conn, case_id, reasoning["chosen_action"], reviewer, reviewed_at)
    record_audit(
        conn,
        "admin",
        reviewer,
        audit_action,
        (
            f"financial_case_id={case_id}, reasoning_id={reasoning_id}, order_id={case['order_id']}, "
            f"reviewer={reviewer}, decision={decision}, recommended_risk_tier={risk_tier}, "
            f"risk_score={risk_score:.1f}, confidence={confidence:.1f}"
        ),
    )
    return route_outcome


def financial_reasoning_review_redirect(case_id, outcome):
    if outcome == "approved":
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?approved=1")
    if outcome == "rejected":
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?rejected=1")
    if outcome == "case_not_found":
        return redirect(url_for("admin_finance") + "?error=case_not_found")
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + f"?error={outcome}")


def handle_financial_case_reasoning_review(case_id, reasoning_id, decision):
    conn = get_db_connection()
    try:
        outcome = review_financial_case_reasoning(conn, case_id, reasoning_id, decision, ADMIN_USERNAME)
        if outcome in {"approved", "rejected", "stale_reasoning"}:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        outcome = "approval_failed" if decision == "APPROVED" else "rejection_failed"
    finally:
        conn.close()

    return financial_reasoning_review_redirect(case_id, outcome)


# ==========================================
# PHASE 5: CONTROLLED ACTION + FOLLOW-UP LIFECYCLE
# Mocked/internal dispatch only. No refunds, payments, notifications, or
# external integrations. Every transition is auditable and admin-initiated.
# ==========================================

def queue_controlled_action(conn, case_id, chosen_action, initiated_by, created_at):
    """Queue the recommended action from an approved reasoning as a pending,
    controlled case_action. Nothing is dispatched automatically."""
    if not chosen_action:
        return None
    cursor = conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, "controlled_action_queued", initiated_by, chosen_action, created_at, "pending", ""),
    )
    return cursor.lastrowid


def dispatch_case_action(conn, case_id, action_id, reviewer):
    """Mock/internal dispatch of a pending action: pending -> completed."""
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return "case_not_found"

    action = conn.execute("SELECT * FROM case_action WHERE id = ?", (action_id,)).fetchone()
    if action is None:
        return "action_not_found"
    if action["case_id"] != case_id:
        return "action_case_mismatch"
    if action["status"] != "pending":
        return "action_not_pending"

    dispatched_at = now_string()
    outcome = f"{action['outcome']} | Dispatched (mocked/internal) by {reviewer}."
    conn.execute(
        "UPDATE case_action SET status = ?, outcome = ?, updated_at = ? WHERE id = ?",
        ("completed", outcome, dispatched_at, action_id),
    )
    record_audit(
        conn,
        "admin",
        reviewer,
        "financial_case_action_dispatched",
        f"financial_case_id={case_id}, action_id={action_id}, order_id={case['order_id']}",
    )
    return "dispatched"


def override_case_action(conn, case_id, action_id, reason, reviewer):
    """Explicit human override of a pending action: pending -> overridden.
    Requires a stated reason for a full audit trail."""
    if not reason:
        return "empty_override_reason"

    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return "case_not_found"

    action = conn.execute("SELECT * FROM case_action WHERE id = ?", (action_id,)).fetchone()
    if action is None:
        return "action_not_found"
    if action["case_id"] != case_id:
        return "action_case_mismatch"
    if action["status"] != "pending":
        return "action_not_pending"

    overridden_at = now_string()
    outcome = f"{action['outcome']} | Overridden by {reviewer}: {reason}"
    conn.execute(
        "UPDATE case_action SET status = ?, outcome = ?, updated_at = ? WHERE id = ?",
        ("overridden", outcome, overridden_at, action_id),
    )
    record_audit(
        conn,
        "admin",
        reviewer,
        "financial_case_action_overridden",
        f"financial_case_id={case_id}, action_id={action_id}, order_id={case['order_id']}, reason={reason}",
    )
    return "overridden"


def complete_case_follow_up(conn, case_id, reviewer):
    """Mark the case's current follow-up as done. Clears follow_up_due_at, which
    is_follow_up_overdue() already treats as not-overdue."""
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return "case_not_found"
    if not case["follow_up_due_at"]:
        return "no_follow_up_to_complete"

    completed_at = now_string()
    previous_due = case["follow_up_due_at"]
    conn.execute(
        "UPDATE financial_case SET follow_up_due_at = ?, updated_at = ? WHERE id = ?",
        ("", completed_at, case_id),
    )
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            "follow_up_completed",
            reviewer,
            f"Follow-up due {previous_due} marked complete.",
            completed_at,
            "completed",
            completed_at,
        ),
    )
    record_audit(
        conn,
        "admin",
        reviewer,
        "financial_case_follow_up_completed",
        f"financial_case_id={case_id}, order_id={case['order_id']}, previous_due={previous_due}",
    )
    return "followup_completed"


def escalate_financial_case(conn, case_id, reason, reviewer):
    """Guarded escalation: blocked once a case is already Escalated or closed.
    Requires a stated reason. Distinct from the general-purpose status dropdown."""
    if not reason:
        return "empty_escalation_reason"

    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        return "case_not_found"
    if case["status"] == "Escalated" or case["status"] in FINANCIAL_CASE_CLOSED_STATUSES:
        return "escalation_blocked"

    escalated_at = now_string()
    previous_status = case["status"]
    conn.execute(
        "UPDATE financial_case SET status = ?, updated_at = ? WHERE id = ?",
        ("Escalated", escalated_at, case_id),
    )
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, "case_escalated", reviewer, reason, escalated_at, "completed", escalated_at),
    )
    record_audit(
        conn,
        "admin",
        reviewer,
        "financial_case_escalated",
        f"financial_case_id={case_id}, order_id={case['order_id']}, reason={reason}, previous_status={previous_status}",
    )
    return "escalated"


def financial_case_action_redirect(case_id, outcome):
    success_params = {
        "dispatched": "dispatched=1",
        "overridden": "overridden=1",
        "followup_completed": "followup_completed=1",
        "escalated": "escalated=1",
    }
    if outcome in success_params:
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + f"?{success_params[outcome]}")
    if outcome == "case_not_found":
        return redirect(url_for("admin_finance") + "?error=case_not_found")
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + f"?error={outcome}")


def user_logged_in():
    return bool(session.get("user_logged_in"))


def admin_logged_in():
    return bool(session.get("admin_logged_in"))


def login_required_user(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not user_logged_in():
            return redirect(url_for("login_user"))
        return view_func(*args, **kwargs)

    return wrapped


def login_required_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not admin_logged_in():
            return redirect(url_for("login_admin"))
        return view_func(*args, **kwargs)

    return wrapped


def get_cart():
    return session.get("cart", {})


def save_cart(cart):
    session["cart"] = cart
    session.modified = True


def cart_count(cart):
    return sum(int(qty) for qty in cart.values())


def get_cart_items(conn):
    cart = get_cart()
    if not cart:
        return [], 0.0

    item_ids = [int(item_id) for item_id in cart.keys()]
    placeholders = ",".join(["?"] * len(item_ids))
    rows = conn.execute(
        f"SELECT * FROM menu_items WHERE id IN ({placeholders}) AND available = 1",
        item_ids,
    ).fetchall()

    row_map = {str(row["id"]): row for row in rows}
    items = []
    total = 0.0

    for item_id, qty_raw in cart.items():
        row = row_map.get(str(item_id))
        if not row:
            continue

        stock_qty = max(0, int(row["stock_qty"] or 0))
        if stock_qty <= 0:
            continue

        qty = max(1, int(qty_raw))
        qty = min(qty, stock_qty)
        price = float(row["price"])
        subtotal = price * qty
        total += subtotal

        items.append(
            {
                "id": row["id"],
                "emoji": row["emoji"],
                "name": row["name"],
                "price": price,
                "stock_qty": stock_qty,
                "quantity": qty,
                "subtotal": subtotal,
            }
        )

    return items, round(total, 2)


def init_db():
    conn = get_db_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            menu TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL,
            status_time TEXT NOT NULL
        )
        """
    )

    order_cols = [row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "total_price" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN total_price REAL NOT NULL DEFAULT 0")
    if "payment_method" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'UPI QR'")
    if "payment_status" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'Pending'")
    if "payment_reference" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_reference TEXT NOT NULL DEFAULT ''")
    if "contact_number" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN contact_number TEXT NOT NULL DEFAULT ''")
    if "payment_proof_path" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_proof_path TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emoji TEXT NOT NULL,
            name TEXT NOT NULL UNIQUE,
            price REAL NOT NULL,
            stock_qty INTEGER NOT NULL DEFAULT 100,
            available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    menu_cols = [row["name"] for row in conn.execute("PRAGMA table_info(menu_items)").fetchall()]
    if "stock_qty" not in menu_cols:
        conn.execute("ALTER TABLE menu_items ADD COLUMN stock_qty INTEGER NOT NULL DEFAULT 100")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            menu_item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            item_price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            subtotal REAL NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_type TEXT NOT NULL,
            actor_name TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financial_case (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'Open',
            risk_tier TEXT NOT NULL DEFAULT 'Unscored',
            risk_score REAL NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT '',
            follow_up_due_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            evidence_type TEXT NOT NULL DEFAULT 'order_snapshot',
            evidence_snapshot TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES financial_case(id)
        )
        """
    )
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_reasoning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            evidence_snapshot_id INTEGER,
            hypothesis TEXT NOT NULL DEFAULT '',
            risk_tier TEXT NOT NULL DEFAULT 'Unscored',
            risk_score REAL NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            chosen_action TEXT NOT NULL DEFAULT '',
            rejected_alternatives TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            requires_human_approval INTEGER NOT NULL DEFAULT 1,
            reasoning_summary TEXT NOT NULL DEFAULT '',
            analysis_source TEXT NOT NULL DEFAULT 'deterministic_v1',
            approval_state TEXT NOT NULL DEFAULT 'PENDING',
            reviewed_at TEXT NOT NULL DEFAULT '',
            reviewed_by TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(evidence_snapshot_id) REFERENCES case_evidence(id),
            FOREIGN KEY(case_id) REFERENCES financial_case(id)
        )
        """
    )
    
    # Phase 2/3: AI architecture and approval metadata updates.
    case_reasoning_cols = [row["name"] for row in conn.execute("PRAGMA table_info(case_reasoning)").fetchall()]
    if "evidence_snapshot_id" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN evidence_snapshot_id INTEGER")
    if "risk_tier" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN risk_tier TEXT NOT NULL DEFAULT 'Unscored'")
    if "requires_human_approval" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN requires_human_approval INTEGER NOT NULL DEFAULT 1")
    if "reasoning_summary" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN reasoning_summary TEXT NOT NULL DEFAULT ''")
    if "analysis_source" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN analysis_source TEXT NOT NULL DEFAULT 'deterministic_v1'")
    if "approval_state" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN approval_state TEXT NOT NULL DEFAULT 'APPROVED'")
    if "reviewed_at" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN reviewed_at TEXT NOT NULL DEFAULT ''")
    if "reviewed_by" not in case_reasoning_cols:
        conn.execute("ALTER TABLE case_reasoning ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_action (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            initiated_by TEXT NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES financial_case(id)
        )
        """
    )

    # Phase 5: action/follow-up lifecycle. Every pre-existing case_action row
    # (case_created, evidence_captured, reasoning_recorded, reasoning_approved,
    # reasoning_rejected, ai_analysis_fallback, manual actions) already represented
    # a finished, one-shot event, so they default to "completed" and keep their
    # existing meaning unchanged.
    case_action_cols = [row["name"] for row in conn.execute("PRAGMA table_info(case_action)").fetchall()]
    if "status" not in case_action_cols:
        conn.execute("ALTER TABLE case_action ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
    if "updated_at" not in case_action_cols:
        conn.execute("ALTER TABLE case_action ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_case_status ON financial_case(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_case_order ON financial_case(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_evidence_case ON case_evidence(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_reasoning_case ON case_reasoning(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_reasoning_approval ON case_reasoning(case_id, approval_state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_action_case ON case_action(case_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case_action_status ON case_action(case_id, status)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            matched_count INTEGER NOT NULL,
            amount_mismatch_count INTEGER NOT NULL,
            duplicate_settlement_count INTEGER NOT NULL,
            no_matching_order_count INTEGER NOT NULL,
            already_reconciled_count INTEGER NOT NULL,
            malformed_incomplete_count INTEGER NOT NULL DEFAULT 0,
            match_rate REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            external_ref TEXT NOT NULL,
            amount REAL NOT NULL,
            settled_at TEXT NOT NULL,
            source TEXT NOT NULL,
            order_id INTEGER,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES reconciliation_batches(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recon_settlements_batch ON reconciliation_settlements(batch_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
        ("admin_qr_path", ""),
    )

    existing_count = conn.execute("SELECT COUNT(*) AS count FROM menu_items").fetchone()["count"]
    if existing_count == 0:
        created_time = now_string()
        default_items = [
            (emoji, name, float(price), 300, 1, created_time)
            for (_category, emoji, name, price) in DEMO_MENU_ITEMS
        ]
        conn.executemany(
            "INSERT INTO menu_items (emoji, name, price, stock_qty, available, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            default_items,
        )

    conn.commit()
    conn.close()


def seed_demo_corporate_orders():
    """Populates realistic historical corporate-catering orders (and, for a
    believable subset, financial cases) so a fresh demo install looks like a
    real Bengaluru corporate catering business instead of an empty database.

    Purely additive demo data:
      - Only ever INSERTs; never reads/modifies/deletes unrelated data.
      - Runs at most once per database, guarded by an app_settings flag,
        the same idempotency pattern already used by init_content_db() and
        ensure_reconciliation_seed_orders() elsewhere in this file.
      - Reuses create_financial_case_for_order()/analyze_financial_case()
        exactly as the admin UI does, so financial-case risk tiers are
        computed by the existing deterministic/Gemini logic, never
        hard-coded here.
      - Does not touch reconciliation_batches/reconciliation_settlements at
        all - it only creates orders (some Paid, some awaiting settlement
        with a reference, some without one). The existing reconciliation
        engine (generate_settlement_batch / reconcile_settlement_batch)
        picks those up unchanged the next time an admin runs a batch.
    """
    conn = get_db_connection()
    try:
        if get_setting(conn, "demo_corporate_orders_seeded", "") == "1":
            return

        menu_rows = conn.execute("SELECT id, name, price FROM menu_items").fetchall()
        menu_by_name = {row["name"]: row for row in menu_rows}
        if not menu_by_name:
            return

        menu_by_category = {}
        for category, _emoji, name, _price in DEMO_MENU_ITEMS:
            if name in menu_by_name:
                menu_by_category.setdefault(category, []).append(menu_by_name[name])

        today = current_local_datetime().date()
        slot_choices = ["breakfast", "lunch", "lunch", "lunch", "snacks", "event"]

        high_risk_candidates = []   # Delivered but not Paid
        medium_risk_candidates = []  # Still in-flight and not Paid
        low_risk_candidates = []    # Cancelled but Paid (refund-pending style)

        total_orders = 160
        for _ in range(total_orders):
            order_date = today - timedelta(days=random.randint(0, 55))
            if order_date.weekday() >= 5 and random.random() < 0.8:
                order_date -= timedelta(days=order_date.weekday() - 4)

            slot = random.choice(slot_choices)
            if slot == "breakfast":
                total_minutes = random.randint(7 * 60, 10 * 60)
            elif slot == "lunch":
                total_minutes = random.randint(11 * 60 + 30, 14 * 60 + 30)
            elif slot == "snacks":
                total_minutes = random.randint(15 * 60, 17 * 60 + 30)
            else:  # corporate event / conference buffet
                total_minutes = random.randint(11 * 60, 14 * 60 + 30)
            hour, minute = divmod(total_minutes, 60)
            order_dt = datetime(order_date.year, order_date.month, order_date.day, hour, minute)
            order_time_str = order_dt.strftime(DISPLAY_DATETIME_FORMAT)

            primary_pool = menu_by_category.get("events" if slot == "event" else slot, [])
            if not primary_pool:
                continue
            beverage_pool = menu_by_category.get("beverages", [])

            num_primary = random.choice([1, 1, 2, 2, 3])
            primary_items = random.sample(primary_pool, k=min(num_primary, len(primary_pool)))

            cart_items = []
            for position, item in enumerate(primary_items):
                weights = DEMO_QUANTITY_WEIGHTS if position == 0 else DEMO_ADDON_QUANTITY_WEIGHTS
                choices = DEMO_QUANTITY_CHOICES if position == 0 else DEMO_ADDON_QUANTITY_CHOICES
                qty = random.choices(choices, weights=weights, k=1)[0]
                price = float(item["price"])
                cart_items.append({
                    "id": item["id"], "name": item["name"], "price": price,
                    "quantity": qty, "subtotal": round(price * qty, 2),
                })

            if beverage_pool and len(cart_items) < 3 and random.random() < 0.5:
                bev = random.choice(beverage_pool)
                qty = random.choices(DEMO_ADDON_QUANTITY_CHOICES, weights=DEMO_ADDON_QUANTITY_WEIGHTS, k=1)[0]
                price = float(bev["price"])
                cart_items.append({
                    "id": bev["id"], "name": bev["name"], "price": price,
                    "quantity": qty, "subtotal": round(price * qty, 2),
                })

            total_quantity = sum(ci["quantity"] for ci in cart_items)
            total_price = round(sum(ci["subtotal"] for ci in cart_items), 2)
            menu_summary = ", ".join(f"{ci['name']} x{ci['quantity']}" for ci in cart_items)

            client = random.choices(
                DEMO_CORPORATE_CLIENTS, weights=DEMO_CORPORATE_CLIENT_WEIGHTS, k=1
            )[0]
            contact_name = (
                f"{random.choice(DEMO_CONTACT_FIRST_NAMES)} {random.choice(DEMO_CONTACT_LAST_NAMES)}"
            )
            contact_number = f"{random.choice('789')}{random.randint(0, 999999999):09d}"
            username = f"{contact_name} - {client}"

            days_old = (today - order_date).days
            if days_old >= 10:
                status = random.choices(["Delivered", "Cancelled"], weights=[92, 8], k=1)[0]
            else:
                status = random.choices(
                    ["Delivered", "Pending", "Preparing", "Ready", "Cancelled"],
                    weights=[35, 22, 18, 18, 7], k=1,
                )[0]

            ref_date_str = order_date.strftime("%Y%m%d")

            def make_ref(prefix):
                return f"{prefix}-{ref_date_str}-{random.randint(1, 99999):05d}"

            if status == "Delivered":
                roll = random.random()
                if roll < 0.78:
                    payment_status = "Paid"
                    payment_method = random.choice(["UPI QR", "NEFT", "Bank Transfer"])
                    payment_reference = make_ref(
                        "UPI" if payment_method == "UPI QR"
                        else "NEFT" if payment_method == "NEFT" else "PAY"
                    )
                elif roll < 0.90:
                    payment_status = "Pending"
                    payment_method = random.choice(["NEFT", "Bank Transfer"])
                    payment_reference = make_ref("NEFT")
                elif roll < 0.96:
                    payment_status = "Pending"
                    payment_method = "Bank Transfer"
                    payment_reference = ""
                else:
                    payment_status = "Unpaid"
                    payment_method = "Bank Transfer"
                    payment_reference = ""
            elif status == "Cancelled":
                if random.random() < 0.35:
                    payment_status = "Paid"
                    payment_method = random.choice(["UPI QR", "NEFT"])
                    payment_reference = make_ref("UPI" if payment_method == "UPI QR" else "NEFT")
                else:
                    payment_status = "Unpaid"
                    payment_method = random.choice(["UPI QR", "Cash"])
                    payment_reference = ""
            else:  # Pending / Preparing / Ready - still in flight
                if random.random() < 0.4:
                    payment_status = "Paid"
                    payment_method = random.choice(["UPI QR", "NEFT"])
                    payment_reference = make_ref("UPI" if payment_method == "UPI QR" else "NEFT")
                else:
                    payment_status = "Pending"
                    payment_method = random.choice(["UPI QR", "Bank Transfer", "Cash"])
                    payment_reference = ""

            cursor = conn.execute(
                """
                INSERT INTO orders (
                    username, menu, quantity, time, status, status_time, total_price,
                    payment_method, payment_status, payment_reference, contact_number,
                    payment_proof_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username, menu_summary, total_quantity, order_time_str, status,
                    order_time_str, total_price, payment_method, payment_status,
                    payment_reference, contact_number, "",
                ),
            )
            order_id = cursor.lastrowid

            conn.executemany(
                """
                INSERT INTO order_items (order_id, menu_item_id, item_name, item_price, quantity, subtotal)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (order_id, ci["id"], ci["name"], ci["price"], ci["quantity"], ci["subtotal"])
                    for ci in cart_items
                ],
            )

            record_status_history(conn, order_id, "Pending", username, "Order placed")
            if status != "Pending":
                record_status_history(
                    conn, order_id, status, ADMIN_USERNAME, f"Status updated to {status}"
                )

            record_audit(
                conn, "system", "demo_seed", "order_placed",
                f"order_id={order_id}, total={total_price:.2f}, payment={payment_method}, client={client}",
            )

            if status == "Delivered" and payment_status != "Paid":
                high_risk_candidates.append(order_id)
            elif status in ("Pending", "Preparing", "Ready") and payment_status != "Paid":
                medium_risk_candidates.append(order_id)
            elif status == "Cancelled" and payment_status == "Paid":
                low_risk_candidates.append(order_id)

        # Open financial cases through the existing, unmodified admin
        # mechanism for a believable subset - not every unresolved order -
        # so risk tiers come out varied (High/Medium/Low) rather than
        # uniformly Critical/High. The engine (not this function) decides
        # each case's actual risk_tier/risk_score.
        random.shuffle(high_risk_candidates)
        random.shuffle(medium_risk_candidates)
        random.shuffle(low_risk_candidates)
        for order_id in high_risk_candidates[:10] + medium_risk_candidates[:10] + low_risk_candidates[:5]:
            create_financial_case_for_order(conn, order_id, ADMIN_USERNAME)

        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("demo_corporate_orders_seeded", "1"),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"WARNING: demo corporate order seeding failed: {exc}")
    finally:
        conn.close()


init_db()
log_startup_warnings()
seed_demo_corporate_orders()


# ===========================================================================
# Track 04 - AI Finance Controller: multi-source settlement reconciliation
#
# Closes one finance-ops loop: an external settlement batch (Razorpay/bank
# files) is matched against `orders`, every record is classified into an
# exact bucket with a specific human-readable reason, matches are applied
# (the matched order is marked Paid), and every run is persisted + audited.
#
# This section only ever reads/writes: orders, app_settings, audit_logs,
# reconciliation_batches, reconciliation_settlements. It never touches
# financial_case / case_evidence / case_reasoning / case_action.
# ===========================================================================

def _recon_reference_code(prefix):
    suffix = "".join(random.choices(string.digits, k=10))
    return f"{prefix}{suffix}"


def ensure_reconciliation_seed_orders(conn):
    """Tops up synthetic order pools to their target sizes before every
    batch, so there is always enough fresh order substrate to reconcile a
    50+ record batch against - across any number of consecutive live demo
    runs, not just the first one. A "matched" settlement closes its order to
    Paid, permanently removing it from the open pool, so without a
    per-call top-up the open pool would run out after one run and every
    subsequent run would show a collapsing match rate. Only ever INSERTs
    new rows - never reads, modifies, or deletes an existing order."""
    created_time = now_string()

    open_with_ref = conn.execute(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE payment_status != 'Paid' AND payment_reference != '' AND total_price > 0"
    ).fetchone()["c"]
    open_without_ref = conn.execute(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE payment_status != 'Paid' AND payment_reference = '' AND total_price > 0"
    ).fetchone()["c"]
    paid_count = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE payment_status = 'Paid' AND total_price > 0"
    ).fetchone()["c"]
    demo_order_count = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE username LIKE 'recon\\_demo\\_%' ESCAPE '\\'"
    ).fetchone()["c"]

    base_amounts = [
        90, 120, 130, 140, 150, 160, 165, 175, 180, 190, 195, 200, 205, 210,
        220, 225, 230, 235, 240, 250, 255, 260, 265, 270, 275, 280, 285, 290,
        295, 300, 305, 310, 315, 320, 325, 330, 335, 340, 345, 350, 355, 360,
        365, 370, 375, 380, 385, 390, 395, 400, 405, 410,
    ]
    idx = demo_order_count

    def next_amount():
        nonlocal idx
        amount = base_amounts[idx % len(base_amounts)] + (idx // len(base_amounts)) * 7
        idx += 1
        return float(amount)

    new_orders = []

    for _ in range(max(0, RECON_TARGET_OPEN_WITH_REF - open_with_ref)):
        new_orders.append((
            f"recon_demo_ref_{idx + 1:04d}", next_amount(), "UPI QR", "Pending",
            _recon_reference_code(random.choice(["RZP", "UTR", "IMPS"])),
        ))

    for _ in range(max(0, RECON_TARGET_OPEN_WITHOUT_REF - open_without_ref)):
        new_orders.append((
            f"recon_demo_noref_{idx + 1:04d}", next_amount(), "UPI QR", "Pending", "",
        ))

    for _ in range(max(0, RECON_TARGET_PAID - paid_count)):
        new_orders.append((
            f"recon_demo_paid_{idx + 1:04d}", next_amount(), "UPI QR", "Paid",
            _recon_reference_code("RZP"),
        ))

    if not new_orders:
        return

    for username, total_price, payment_method, payment_status, payment_reference in new_orders:
        conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time, total_price,
                payment_method, payment_status, payment_reference, contact_number,
                payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username, "Reconciliation demo order", 1, created_time, "Delivered",
                created_time, total_price, payment_method, payment_status,
                payment_reference, "", "",
            ),
        )

    record_audit(
        conn, "system", "reconciliation_agent", "reconciliation_seed_orders_topped_up",
        f"count={len(new_orders)}",
    )


def generate_settlement_batch(conn):
    """Generates a fresh, randomized batch of 50+ synthetic external
    settlement records to reconcile against the current order data.
    Deliberately mixes ~70-85% cleanly matchable records with a realistic
    spread of every required exception type. Regenerates differently on
    every call - never hardcoded."""
    ensure_reconciliation_seed_orders(conn)

    orders = conn.execute(
        "SELECT id, total_price, payment_status, payment_reference FROM orders"
    ).fetchall()

    open_with_ref = [
        o for o in orders
        if o["payment_status"] != "Paid"
        and (o["payment_reference"] or "").strip()
        and float(o["total_price"] or 0) > 0
    ]
    open_without_ref = [
        o for o in orders
        if o["payment_status"] != "Paid"
        and not (o["payment_reference"] or "").strip()
        and float(o["total_price"] or 0) > 0
    ]
    paid_orders = [
        o for o in orders
        if o["payment_status"] == "Paid" and float(o["total_price"] or 0) > 0
    ]

    random.shuffle(open_with_ref)
    random.shuffle(open_without_ref)
    random.shuffle(paid_orders)

    # Cap each bucket to a target size so the batch's proportions stay
    # controlled even as real order data grows over time.
    ref_for_mismatch = open_with_ref[:4]
    ref_for_duplicate = open_with_ref[4:7]
    ref_for_clean = open_with_ref[7:7 + 22]
    amount_fallback_pool = open_without_ref[:18]
    already_reconciled_pool = paid_orders[:5]

    now_dt = current_local_datetime()

    def settled_at_near():
        return (now_dt - timedelta(minutes=random.randint(5, 2400))).strftime(DISPLAY_DATETIME_FORMAT)

    def random_source():
        return random.choice(RECON_SETTLEMENT_SOURCES)

    settlements = []

    for order in ref_for_clean:
        settlements.append({
            "external_ref": order["payment_reference"],
            "amount": float(order["total_price"]),
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    for order in amount_fallback_pool:
        settlements.append({
            "external_ref": "",
            "amount": float(order["total_price"]),
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    for order in ref_for_mismatch:
        fee = round(random.uniform(2.0, 15.0), 2)
        settlements.append({
            "external_ref": order["payment_reference"],
            "amount": round(float(order["total_price"]) - fee, 2),
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    for order in ref_for_duplicate:
        row = {
            "external_ref": order["payment_reference"],
            "amount": float(order["total_price"]),
            "settled_at": settled_at_near(),
            "source": random_source(),
        }
        settlements.append(dict(row))
        settlements.append(dict(row))  # the intentional duplicate settlement

    for order in already_reconciled_pool:
        settlements.append({
            "external_ref": (order["payment_reference"] or "").strip(),
            "amount": float(order["total_price"]),
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    known_amounts = {round(float(o["total_price"] or 0), 2) for o in orders}
    for _ in range(4):
        bogus_amount = round(random.uniform(500.0, 2500.0), 2)
        for _attempt in range(20):
            if all(abs(bogus_amount - amt) > RECON_FALLBACK_AMOUNT_TOLERANCE for amt in known_amounts):
                break
            bogus_amount = round(random.uniform(500.0, 2500.0), 2)
        settlements.append({
            "external_ref": _recon_reference_code("ORPHAN"),
            "amount": bogus_amount,
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    # Track 04's bar requires 50+ records - top up defensively in case the
    # order pool was smaller than expected (should not happen post-seed).
    while len(settlements) < 50:
        settlements.append({
            "external_ref": _recon_reference_code("ORPHAN"),
            "amount": round(random.uniform(500.0, 2500.0), 2),
            "settled_at": settled_at_near(),
            "source": random_source(),
        })

    random.shuffle(settlements)
    return settlements


def reconcile_settlement_batch(conn, settlements):
    """Matches each settlement record against current order data using a
    two-pass strategy - exact payment_reference match first, then an
    amount-within-tolerance fallback - and classifies every record into
    exactly one of: matched / amount_mismatch / duplicate_settlement /
    no_matching_order / already_reconciled, each with a specific reason.

    A "matched" record actually closes the loop: the corresponding order is
    marked Paid. This is what makes re-running the same batch idempotent -
    an order closed by an earlier run (or an earlier record in this same
    run) is correctly reclassified as already_reconciled / duplicate on the
    next pass rather than being silently double-counted as a fresh match.

    Pure with respect to `settlements` (never mutated) and reads order state
    fresh from `conn` at the start of the call."""
    orders = conn.execute(
        "SELECT id, total_price, payment_status, payment_reference FROM orders"
    ).fetchall()

    orders_by_id = {o["id"]: dict(o) for o in orders}
    ref_index = {}
    for o in orders:
        ref = (o["payment_reference"] or "").strip()
        if ref and ref not in ref_index:
            ref_index[ref] = o["id"]

    claimed_order_ids = set()
    results = []
    counts = {
        "matched": 0,
        "amount_mismatch": 0,
        "duplicate_settlement": 0,
        "no_matching_order": 0,
        "already_reconciled": 0,
        "malformed_incomplete": 0,
    }

    for settlement in settlements:
        ext_ref = (settlement.get("external_ref") or "").strip()
        amount_raw = settlement.get("amount")
        settled_at = settlement.get("settled_at", "")
        source = settlement.get("source", "")

        # Validate required fields - malformed/incomplete data must be caught early
        is_malformed = False
        malformed_reason = None
        amount = 0.0

        if amount_raw is None or amount_raw == "":
            is_malformed = True
            malformed_reason = "Missing or blank amount field."
        else:
            try:
                amount = round(float(amount_raw), 2)
            except (ValueError, TypeError):
                is_malformed = True
                malformed_reason = f"Invalid amount value '{amount_raw}' - cannot parse as numeric."
                amount = 0.0

        if is_malformed:
            classification = "malformed_incomplete"
            reason = malformed_reason
            counts[classification] += 1
            results.append({
                "external_ref": ext_ref,
                "amount": amount,
                "settled_at": settled_at,
                "source": source,
                "order_id": None,
                "classification": classification,
                "reason": reason,
            })
            continue

        order_id = None
        match_kind = None

        if ext_ref and ext_ref in ref_index:
            order_id = ref_index[ext_ref]
            match_kind = "reference"
        else:
            best_id, best_diff = None, None
            for o in orders:
                order_total = round(float(o["total_price"] or 0), 2)
                diff = abs(order_total - amount)
                if diff <= RECON_FALLBACK_AMOUNT_TOLERANCE and (
                    best_diff is None or diff < best_diff or (diff == best_diff and o["id"] < best_id)
                ):
                    best_id, best_diff = o["id"], diff
            if best_id is not None:
                order_id, match_kind = best_id, "amount"

        if order_id is None:
            classification = "no_matching_order"
            reason = (
                f"No order found with payment_reference '{ext_ref or '(blank)'}' or a "
                f"total_price within Rs {RECON_FALLBACK_AMOUNT_TOLERANCE:.2f} of Rs {amount:.2f}."
            )
        else:
            order = orders_by_id[order_id]
            order_total = round(float(order["total_price"] or 0), 2)
            amount_diff = round(abs(order_total - amount), 2)

            if order_id in claimed_order_ids:
                classification = "duplicate_settlement"
                reason = (
                    f"Order #{order_id} was already reconciled earlier in this same batch; "
                    f"this settlement (Rs {amount:.2f}, ref '{ext_ref or '(blank)'}') is a duplicate."
                )
            elif match_kind == "reference" and amount_diff > RECON_REFERENCE_AMOUNT_TOLERANCE:
                classification = "amount_mismatch"
                reason = (
                    f"Reference '{ext_ref}' matched order #{order_id}, but the settled amount "
                    f"Rs {amount:.2f} differs from the order total Rs {order_total:.2f} by Rs {amount_diff:.2f}."
                )
            elif order["payment_status"] == "Paid":
                classification = "already_reconciled"
                reason = (
                    f"Order #{order_id} is already marked Paid; this settlement (Rs {amount:.2f}) "
                    f"duplicates a prior reconciliation and was not re-applied."
                )
            else:
                classification = "matched"
                reason = (
                    f"Closed order #{order_id}: settlement Rs {amount:.2f} matches the order "
                    f"total Rs {order_total:.2f} via {match_kind} match."
                )
                claimed_order_ids.add(order_id)
                new_reference = ext_ref if ext_ref else (order["payment_reference"] or "")
                conn.execute(
                    "UPDATE orders SET payment_status = ?, payment_reference = ?, status_time = ? WHERE id = ?",
                    ("Paid", new_reference, now_string(), order_id),
                )
                order["payment_status"] = "Paid"
                order["payment_reference"] = new_reference

        counts[classification] += 1
        results.append({
            "external_ref": ext_ref,
            "amount": amount,
            "settled_at": settled_at,
            "source": source,
            "order_id": order_id,
            "classification": classification,
            "reason": reason,
        })

    total = len(settlements)
    match_rate = round((counts["matched"] / total) * 100, 2) if total else 0.0

    return {"results": results, "total": total, "counts": counts, "match_rate": match_rate}


def run_new_reconciliation_batch(conn, triggered_by):
    """Generates a fresh batch, reconciles it, and persists the batch header
    + every settlement row + an audit log entry, all in one transaction.
    The match rate is always computed live from the batch just generated -
    never hardcoded or precomputed."""
    settlements = generate_settlement_batch(conn)
    outcome = reconcile_settlement_batch(conn, settlements)
    counts = outcome["counts"]
    created_at = now_string()

    cursor = conn.execute(
        """
        INSERT INTO reconciliation_batches (
            created_at, triggered_by, record_count, matched_count,
            amount_mismatch_count, duplicate_settlement_count,
            no_matching_order_count, already_reconciled_count,
            malformed_incomplete_count, match_rate
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at, triggered_by, outcome["total"], counts["matched"],
            counts["amount_mismatch"], counts["duplicate_settlement"],
            counts["no_matching_order"], counts["already_reconciled"],
            counts.get("malformed_incomplete", 0),
            outcome["match_rate"],
        ),
    )
    batch_id = cursor.lastrowid

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
             r["order_id"], r["classification"], r["reason"])
            for r in outcome["results"]
        ],
    )

    record_audit(
        conn, "admin", triggered_by, "reconciliation_batch_run",
        (
            f"batch_id={batch_id}, records={outcome['total']}, "
            f"match_rate={outcome['match_rate']:.2f}%, "
            f"exceptions={outcome['total'] - counts['matched']}"
        ),
    )

    conn.commit()
    return batch_id


def list_reconciliation_batches(conn, limit=15):
    rows = conn.execute(
        "SELECT * FROM reconciliation_batches ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_reconciliation_batch(conn, batch_id):
    batch = conn.execute(
        "SELECT * FROM reconciliation_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if not batch:
        return None, []
    settlements = conn.execute(
        "SELECT * FROM reconciliation_settlements WHERE batch_id = ? ORDER BY id ASC",
        (batch_id,),
    ).fetchall()
    return dict(batch), [dict(s) for s in settlements]


@app.route("/admin/reconciliation")
@login_required_admin
def reconciliation_dashboard():
    conn = get_db_connection()
    batches = list_reconciliation_batches(conn)
    conn.close()
    return render_template("reconciliation_dashboard.html", batches=batches)


@app.route("/admin/reconciliation/run", methods=["POST"])
@login_required_admin
def run_reconciliation_batch():
    conn = get_db_connection()
    batch_id = run_new_reconciliation_batch(conn, ADMIN_USERNAME)
    conn.close()
    return redirect(url_for("reconciliation_batch_detail", batch_id=batch_id))


@app.route("/admin/reconciliation/<int:batch_id>", methods=["GET", "POST"])
@login_required_admin
def reconciliation_batch_detail(batch_id):
    conn = get_db_connection()
    batch, settlements = get_reconciliation_batch(conn, batch_id)
    conn.close()
    if not batch:
        return redirect(url_for("reconciliation_dashboard"))
    exceptions = [s for s in settlements if s["classification"] != "matched"]
    
    # Check if this is a POST from the analyze button
    agent_result = None
    if request.method == "POST":
        # The analyze route will handle the actual analysis
        # This branch should not normally be reached since the form posts to /analyze
        pass
    
    return render_template(
        "reconciliation_detail.html", batch=batch, settlements=settlements, exceptions=exceptions, agent_result=agent_result,
    )


@app.route("/admin/reconciliation/<int:batch_id>/analyze", methods=["POST"])
@login_required_admin
def analyze_reconciliation_batch_route(batch_id):
    """Analyze a reconciliation batch using the Finance Controller Agent.
    
    This endpoint:
    1. Fetches the batch and its exceptions from the database
    2. Enriches exception data with order information
    3. Calls Gemini API (with deterministic fallback)
    4. Returns structured analysis WITHOUT modifying any records
    
    The agent does NOT:
    - Modify reconciliation records
    - Auto-create financial cases
    - Auto-approve or dispatch actions
    """
    conn = get_db_connection()
    try:
        batch, settlements = get_reconciliation_batch(conn, batch_id)
        if not batch:
            return jsonify({"error": "Batch not found"}), 404
        
        # Get only exceptions (non-matched settlements)
        exceptions = [s for s in settlements if s["classification"] != "matched"]
        
        # Enrich with order data for linked orders
        order_ids = set(ex.get("order_id") for ex in exceptions if ex.get("order_id"))
        order_data_map = {}
        if order_ids:
            placeholders = ",".join("?" * len(order_ids))
            orders = conn.execute(
                f"SELECT id, status, payment_status, total_price FROM orders WHERE id IN ({placeholders})",
                tuple(order_ids)
            ).fetchall()
            order_data_map = {o["id"]: dict(o) for o in orders}
        
        # Try AI analysis first if GEMINI_API_KEY is set
        result = None
        gemini_key = os.environ.get("GEMINI_API_KEY")
        
        if gemini_key and exceptions:
            try:
                model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
                prompt = build_reconciliation_agent_prompt(batch, exceptions, order_data_map)
                ai_text = call_gemini_api(prompt, gemini_key, model_name)
                
                # Parse and validate response
                try:
                    ai_data = json.loads(ai_text)
                except Exception:
                    ai_data = None
                
                if ai_data is not None and validate_reconciliation_agent_response(ai_data):
                    result = {
                        "analysis_source": "gemini_ai_v1",
                        **ai_data
                    }
                else:
                    print("WARNING: Reconciliation agent AI validation failed. Using deterministic fallback.")
                    result = None
                    
            except Exception as e:
                failure_reason = redact_secret(str(e), gemini_key)
                print(f"WARNING: Reconciliation agent AI failed ({failure_reason}). Using deterministic fallback.")
                result = None
        
        # Deterministic fallback (always works, never fabricates)
        if result is None:
            result = evaluate_reconciliation_batch_deterministic(batch, exceptions, order_data_map)
        
        # Add batch metadata to response
        result["batch_id"] = batch_id
        result["exception_count"] = len(exceptions)
        
        conn.close()
        return jsonify(result)
        
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        role = (request.form.get("role") or "").strip().lower()
        if role == "admin":
            return redirect(url_for("login_admin"))
        return redirect(url_for("login_user"))
    return render_template("index.html")


@app.route("/login/user", methods=["GET", "POST"])
def login_user():
    if user_logged_in():
        return redirect(url_for("order"))

    error = ""
    username = ""
    if request.method == "POST":
        action = (request.form.get("action") or "login").strip().lower()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username:
            error = "username_required"
        elif not password:
            error = "password_required"
        elif action == "register" and password != confirm_password:
            error = "password_mismatch"
        else:
            conn = get_db_connection()
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

            if action == "register":
                if user:
                    error = "user_exists"
                else:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                        (username, generate_password_hash(password), now_string()),
                    )
                    record_audit(conn, "user", username, "user_registered", "")
                    conn.commit()
                    conn.close()
                    session["user_logged_in"] = True
                    session["last_username"] = username
                    session.pop("admin_logged_in", None)
                    return redirect(url_for("order"))
            else:
                if not user or not check_password_hash(user["password_hash"], password):
                    error = "invalid_credentials"
                else:
                    record_audit(conn, "user", username, "user_logged_in", "")
                    conn.commit()
                    conn.close()
                    session["user_logged_in"] = True
                    session["last_username"] = username
                    session.pop("admin_logged_in", None)
                    return redirect(url_for("order"))

            conn.close()

    return render_template("login_user.html", error=error, username=username)


@app.route("/login/admin", methods=["GET", "POST"])
def login_admin():
    if admin_logged_in():
        return redirect(url_for("admin"))

    error = ""
    login_enabled = admin_login_enabled()
    if request.method == "POST":
        if not login_enabled:
            error = "admin_not_configured"
            return render_template("login_admin.html", error=error, admin_login_enabled=login_enabled)

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            conn = get_db_connection()
            record_audit(conn, "admin", username, "admin_logged_in", "")
            conn.commit()
            conn.close()
            session["admin_logged_in"] = True
            session.pop("user_logged_in", None)
            return redirect(url_for("admin"))
        error = "invalid_credentials"

    return render_template("login_admin.html", error=error, admin_login_enabled=login_enabled)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if user_logged_in():
        return redirect(url_for("order"))

    error = ""
    success = False
    username = ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username:
            error = "username_required"
        elif not new_password:
            error = "password_required"
        elif new_password != confirm_password:
            error = "password_mismatch"
        else:
            conn = get_db_connection()
            user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if not user:
                error = "user_not_found"
            else:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    (generate_password_hash(new_password), username),
                )
                record_audit(conn, "user", username, "password_reset", "")
                conn.commit()
                success = True
            conn.close()

    return render_template("reset_password.html", error=error, success=success, username=username)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/order", methods=["GET", "POST"])
@login_required_user
def order():
    conn = get_db_connection()
    current_username = (session.get("last_username") or "").strip()
    if not current_username:
        conn.close()
        return redirect(url_for("logout"))

    if request.method == "POST":
        username = current_username
        payment_mode = (request.form.get("payment_mode") or "upi").strip().lower()
        if payment_mode not in {"upi", "cash"}:
            payment_mode = "upi"
        contact_number = "".join(
            char for char in (request.form.get("contact_number") or "") if char.isdigit()
        )
        payment_reference = (request.form.get("payment_reference") or "").strip()
        payment_proof_file = request.files.get("payment_proof")
        has_payment_proof = bool(payment_proof_file and payment_proof_file.filename)
        cart_items, total_price = get_cart_items(conn)
        upi_link = build_upi_link(total_price, f"Food order by {username or 'customer'}")
        stored_qr_path = get_setting(conn, "admin_qr_path", "")
        qr_url = url_for("static", filename=stored_qr_path) if stored_qr_path else build_qr_url(upi_link)

        if not cart_items:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=[],
                cart_total=0,
                cart_count=0,
                error="empty_cart",
                username=username,
                contact_number=contact_number,
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        stock_errors = []
        for item in cart_items:
            stock_row = conn.execute(
                "SELECT stock_qty FROM menu_items WHERE id = ? AND available = 1",
                (item["id"],),
            ).fetchone()
            if not stock_row or int(stock_row["stock_qty"] or 0) < int(item["quantity"]):
                stock_errors.append(item["name"])

        if stock_errors:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=cart_items,
                cart_total=total_price,
                cart_count=cart_count(get_cart()),
                error="stock_unavailable",
                username=username,
                contact_number=contact_number,
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        if len(contact_number) != 10:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=cart_items,
                cart_total=total_price,
                cart_count=cart_count(get_cart()),
                error="contact_required",
                username=username,
                contact_number=contact_number,
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        if payment_mode == "upi" and has_payment_proof:
            filename = secure_filename(payment_proof_file.filename)
            if not allowed_payment_proof_file(filename):
                menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
                conn.close()
                return render_template(
                    "order.html",
                    menu_items=menu_items,
                    cart_items=cart_items,
                    cart_total=total_price,
                    cart_count=cart_count(get_cart()),
                    error="payment_proof_type",
                    username=username,
                    contact_number=contact_number,
                    payment_mode=payment_mode,
                    payment_reference=payment_reference,
                    upi_link=upi_link,
                    qr_url=qr_url,
                    upi_id=UPI_ID,
                    upi_name=UPI_NAME,
                )

        if payment_mode == "upi" and not has_payment_proof:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=cart_items,
                cart_total=total_price,
                cart_count=cart_count(get_cart()),
                error="upi_reference_required",
                username=username,
                contact_number=contact_number,
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        total_quantity = sum(item["quantity"] for item in cart_items)
        menu_summary = ", ".join(f"{item['name']} x{item['quantity']}" for item in cart_items)
        order_time = now_string()
        session["last_username"] = username
        if payment_mode == "cash":
            payment_method = "Cash"
            payment_status = "Unpaid"
            payment_reference = ""
            payment_proof_path = ""
        else:
            payment_method = "UPI QR"
            payment_status = "Paid"
            payment_proof_path = ""

        cursor = conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time, total_price,
                payment_method, payment_status, payment_reference, contact_number,
                payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                menu_summary,
                total_quantity,
                order_time,
                "Pending",
                order_time,
                total_price,
                payment_method,
                payment_status,
                payment_reference,
                contact_number,
                payment_proof_path,
            ),
        )
        order_id = cursor.lastrowid

        if payment_mode == "upi" and has_payment_proof:
            saved_proof_path = save_payment_proof(payment_proof_file, order_id)
            if saved_proof_path:
                payment_proof_path = saved_proof_path
                conn.execute(
                    "UPDATE orders SET payment_proof_path = ? WHERE id = ?",
                    (payment_proof_path, order_id),
                )

        conn.executemany(
            """
            INSERT INTO order_items (order_id, menu_item_id, item_name, item_price, quantity, subtotal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    order_id,
                    item["id"],
                    item["name"],
                    item["price"],
                    item["quantity"],
                    item["subtotal"],
                )
                for item in cart_items
            ],
        )
        for item in cart_items:
            conn.execute(
                """
                UPDATE menu_items
                SET stock_qty = stock_qty - ?,
                    available = CASE WHEN stock_qty - ? <= 0 THEN 0 ELSE available END
                WHERE id = ?
                """,
                (item["quantity"], item["quantity"], item["id"]),
            )
        record_audit(
            conn,
            "user",
            username,
            "order_placed",
            f"order_id={order_id}, total={total_price:.2f}, payment={payment_method}",
        )
        record_status_history(conn, order_id, "Pending", username, "Order placed")

        conn.commit()
        conn.close()

        save_cart({})
        return redirect(url_for("success", order_id=order_id))

    menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
    cart_items, total_price = get_cart_items(conn)
    stored_qr_path = get_setting(conn, "admin_qr_path", "")
    conn.close()
    upi_link = build_upi_link(total_price, f"Food order by {(session.get('last_username') or 'customer')}")
    qr_url = url_for("static", filename=stored_qr_path) if stored_qr_path else build_qr_url(upi_link)

    return render_template(
        "order.html",
        menu_items=menu_items,
        cart_items=cart_items,
        cart_total=total_price,
        cart_count=cart_count(get_cart()),
        username=current_username,
        payment_mode="upi",
        payment_reference="",
        contact_number="",
        upi_link=upi_link,
        qr_url=qr_url,
        upi_id=UPI_ID,
        upi_name=UPI_NAME,
    )


@app.route("/cart/add", methods=["POST"])
@login_required_user
def add_to_cart():
    item_id = request.form.get("item_id")
    qty_raw = request.form.get("quantity", "1")

    try:
        item_id_int = int(item_id)
        qty = int(qty_raw)
    except (TypeError, ValueError):
        return redirect(url_for("order"))

    qty = max(1, min(qty, 100))

    conn = get_db_connection()
    item = conn.execute(
        "SELECT id, stock_qty FROM menu_items WHERE id = ? AND available = 1",
        (item_id_int,),
    ).fetchone()
    if not item or int(item["stock_qty"] or 0) <= 0:
        conn.close()
        return redirect(url_for("order"))

    stock_qty = int(item["stock_qty"])
    conn.close()

    cart = get_cart()
    key = str(item_id_int)
    current_qty = int(cart.get(key, 0))
    cart[key] = min(current_qty + qty, stock_qty)
    save_cart(cart)

    return redirect(url_for("order"))


@app.route("/cart/update", methods=["POST"])
@login_required_user
def update_cart():
    item_id = request.form.get("item_id")
    qty_raw = request.form.get("quantity", "1")

    try:
        item_id_int = int(item_id)
        qty = int(qty_raw)
    except (TypeError, ValueError):
        return redirect(url_for("order"))

    cart = get_cart()
    key = str(item_id_int)

    if qty <= 0:
        cart.pop(key, None)
    else:
        conn = get_db_connection()
        item = conn.execute(
            "SELECT stock_qty FROM menu_items WHERE id = ? AND available = 1",
            (item_id_int,),
        ).fetchone()
        conn.close()
        max_stock = int(item["stock_qty"] or 0) if item else 0
        if max_stock <= 0:
            cart.pop(key, None)
        else:
            cart[key] = min(qty, 100, max_stock)

    save_cart(cart)
    return redirect(url_for("order"))


@app.route("/cart/remove/<int:item_id>")
@login_required_user
def remove_from_cart(item_id):
    cart = get_cart()
    cart.pop(str(item_id), None)
    save_cart(cart)
    return redirect(url_for("order"))


@app.route("/success", methods=["GET"])
@login_required_user
def success():
    order_id = request.args.get("order_id")
    if order_id is None:
        return redirect(url_for("index"))

    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    current_user = session.get("last_username", "")
    if order is None or order["username"] != current_user:
        conn.close()
        return redirect(url_for("index"))

    items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,)).fetchall()
    timeline = get_order_timeline(conn, order_id)
    conn.close()

    order = normalize_datetime_fields(order, ["time", "status_time"])
    timeline = [normalize_datetime_fields(item, ["changed_at"]) for item in timeline]
    return render_template("success.html", order=order, order_items=items, timeline=timeline)


@app.route("/receipt/<int:order_id>")
@login_required_user
def receipt(order_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    current_user = session.get("last_username", "")
    if order is None or order["username"] != current_user:
        conn.close()
        return redirect(url_for("my_orders"))

    items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,)).fetchall()
    timeline = get_order_timeline(conn, order_id)
    conn.close()

    order = normalize_datetime_fields(order, ["time", "status_time"])
    timeline = [normalize_datetime_fields(item, ["changed_at"]) for item in timeline]
    return render_template("receipt.html", order=order, order_items=items, timeline=timeline)


@app.route("/my-orders", methods=["GET"])
@login_required_user
def my_orders():
    username = session.get("last_username", "").strip()
    conn = get_db_connection()
    user_orders = conn.execute(
        "SELECT * FROM orders WHERE username = ? ORDER BY id DESC",
        (username,),
    ).fetchall()
    conn.close()

    user_orders = [normalize_datetime_fields(order, ["time", "status_time"]) for order in user_orders]
    return render_template("my_orders.html", orders=user_orders, username=username)


@app.route("/cancel-order/<int:order_id>")
@login_required_user
def cancel_order(order_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    current_user = session.get("last_username", "")

    if order and order["username"] == current_user and order["status"] == "Pending":
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM order_status_history WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("my_orders") + "?cancelled=true")

    conn.close()
    return redirect(url_for("my_orders") + "?error=cannot_cancel")


@app.route("/status", methods=["GET"])
@login_required_user
def status():
    username = session.get("last_username", "").strip()
    conn = get_db_connection()
    user_orders = conn.execute(
        "SELECT * FROM orders WHERE username = ? ORDER BY id DESC",
        (username,),
    ).fetchall()
    conn.close()

    user_orders = [normalize_datetime_fields(order, ["time", "status_time"]) for order in user_orders]
    return render_template("status.html", orders=user_orders, username=username)


@app.route("/admin")
@login_required_admin
def admin():
    username_query = (request.args.get("username") or "").strip()
    status_query = (request.args.get("status") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    sort_by = (request.args.get("sort") or "date_desc").strip()

    valid_statuses = {"Pending", "Preparing", "Ready", "Delivered"}
    if status_query not in valid_statuses:
        status_query = ""

    sql = "SELECT * FROM orders WHERE 1=1"
    params = []

    if username_query:
        sql += " AND username LIKE ?"
        params.append(f"%{username_query}%")

    if status_query:
        sql += " AND status = ?"
        params.append(status_query)

    conn = get_db_connection()
    orders = conn.execute(sql, params).fetchall()
    pending_count = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status = 'Pending'").fetchone()["c"]
    preparing_count = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status = 'Preparing'").fetchone()["c"]
    ready_count = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status = 'Ready'").fetchone()["c"]
    delivered_count = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status = 'Delivered'").fetchone()["c"]
    finance_open_count = conn.execute(
        "SELECT COUNT(*) AS c FROM financial_case WHERE status NOT IN ('Resolved', 'Closed')"
    ).fetchone()["c"]
    finance_candidate_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM orders o
        LEFT JOIN financial_case fc ON fc.order_id = o.id
        WHERE o.status = 'Delivered'
          AND o.payment_status != 'Paid'
          AND COALESCE(o.total_price, 0) > 0
          AND fc.id IS NULL
        """
    ).fetchone()["c"]
    all_orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    low_stock_items = conn.execute(
        """
        SELECT * FROM menu_items
        WHERE stock_qty <= 10
        ORDER BY stock_qty ASC, name ASC
        """
    ).fetchall()
    top_items = conn.execute(
        """
        SELECT item_name, SUM(quantity) AS sold_qty, SUM(subtotal) AS revenue
        FROM order_items
        GROUP BY item_name
        ORDER BY sold_qty DESC, revenue DESC
        LIMIT 5
        """
    ).fetchall()
    qr_image_path = get_setting(conn, "admin_qr_path", "")
    qr_image_url = url_for("static", filename=qr_image_path) if qr_image_path else ""
    recent_logs = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 12").fetchall()
    conn.close()

    orders = [normalize_datetime_fields(order, ["time", "status_time"]) for order in orders]
    recent_logs = [normalize_datetime_fields(log, ["created_at"]) for log in recent_logs]

    from_date_obj = None
    to_date_obj = None
    try:
        if date_from:
            from_date_obj = datetime.strptime(date_from, "%Y-%m-%d").date()
    except ValueError:
        date_from = ""

    try:
        if date_to:
            to_date_obj = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        date_to = ""

    if from_date_obj or to_date_obj:
        filtered_orders = []
        for order in orders:
            order_date = parse_order_datetime(order["time"]).date()
            if from_date_obj and order_date < from_date_obj:
                continue
            if to_date_obj and order_date > to_date_obj:
                continue
            filtered_orders.append(order)
        orders = filtered_orders

    status_order = {"Pending": 1, "Preparing": 2, "Ready": 3, "Delivered": 4}

    if sort_by == "date_asc":
        orders = sorted(orders, key=lambda o: parse_order_datetime(o["time"]))
    elif sort_by == "quantity_asc":
        orders = sorted(orders, key=lambda o: int(o["quantity"]))
    elif sort_by == "quantity_desc":
        orders = sorted(orders, key=lambda o: int(o["quantity"]), reverse=True)
    elif sort_by == "status_asc":
        orders = sorted(orders, key=lambda o: status_order.get(o["status"], 99))
    elif sort_by == "status_desc":
        orders = sorted(orders, key=lambda o: status_order.get(o["status"], 99), reverse=True)
    elif sort_by == "total_asc":
        orders = sorted(orders, key=lambda o: float(o["total_price"] or 0))
    elif sort_by == "total_desc":
        orders = sorted(orders, key=lambda o: float(o["total_price"] or 0), reverse=True)
    else:
        sort_by = "date_desc"
        orders = sorted(orders, key=lambda o: parse_order_datetime(o["time"]), reverse=True)

    total_revenue = round(sum(float(order["total_price"] or 0) for order in orders), 2)
    today = current_local_datetime().date()
    today_orders = [order for order in all_orders if parse_order_datetime(order["time"]).date() == today]
    dashboard_stats = {
        "all_orders": len(all_orders),
        "today_orders": len(today_orders),
        "today_revenue": round(sum(float(order["total_price"] or 0) for order in today_orders), 2),
        "all_revenue": round(sum(float(order["total_price"] or 0) for order in all_orders), 2),
        "pending": pending_count,
        "preparing": preparing_count,
        "ready": ready_count,
        "delivered": delivered_count,
        "low_stock": len(low_stock_items),
        "finance_open": finance_open_count,
        "finance_candidates": finance_candidate_count,
    }
    filters = {
        "username": username_query,
        "status": status_query,
        "date_from": date_from,
        "date_to": date_to,
        "sort": sort_by,
    }

    return render_template(
        "admin.html",
        orders=orders,
        filters=filters,
        total_revenue=total_revenue,
        qr_image_url=qr_image_url,
        pending_count=pending_count,
        dashboard_stats=dashboard_stats,
        low_stock_items=low_stock_items,
        top_items=top_items,
        recent_logs=recent_logs,
    )


@app.route("/admin/order/<int:order_id>")
@login_required_admin
def admin_order_detail(order_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return redirect(url_for("admin"))
    items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,)).fetchall()
    timeline = get_order_timeline(conn, order_id)
    finance_case = conn.execute(
        "SELECT * FROM financial_case WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    logs = conn.execute(
        "SELECT * FROM audit_logs WHERE details LIKE ? ORDER BY id DESC",
        (f"%order_id={order_id}%",),
    ).fetchall()
    conn.close()
    order = normalize_datetime_fields(order, ["time", "status_time"])
    timeline = [normalize_datetime_fields(item, ["changed_at"]) for item in timeline]
    finance_case = normalize_datetime_fields(
        finance_case,
        ["created_at", "updated_at", "resolved_at", "follow_up_due_at"],
    )
    logs = [normalize_datetime_fields(log, ["created_at"]) for log in logs]
    return render_template(
        "admin_order_detail.html",
        order=order,
        order_items=items,
        timeline=timeline,
        finance_case=finance_case,
        logs=logs,
    )


@app.route("/admin/order/<int:order_id>/kitchen-ticket")
@login_required_admin
def kitchen_ticket(order_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return redirect(url_for("admin"))

    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC",
        (order_id,),
    ).fetchall()
    latest_note = conn.execute(
        """
        SELECT note FROM order_status_history
        WHERE order_id = ? AND note != ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (order_id,),
    ).fetchone()
    conn.close()

    order = normalize_datetime_fields(order, ["time", "status_time"])
    return render_template(
        "kitchen_ticket.html",
        order=order,
        order_items=items,
        kitchen_note=latest_note["note"] if latest_note else "",
    )


@app.route("/admin/report.csv")
@login_required_admin
def admin_report_csv():
    report_range = (request.args.get("range") or "daily").strip().lower()
    days = 1 if report_range == "daily" else 7
    start_dt = current_local_datetime() - timedelta(days=days)

    conn = get_db_connection()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()

    filtered = [order for order in orders if parse_order_datetime(order["time"]) >= start_dt]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "order_id",
            "username",
            "items_summary",
            "quantity",
            "total_price",
            "payment_method",
            "payment_status",
            "contact_number",
            "status",
            "payment_proof_path",
            "order_time",
            "last_updated",
        ]
    )
    for order in filtered:
        writer.writerow(
            [
                order["id"],
                order["username"],
                order["menu"],
                order["quantity"],
                f"{float(order['total_price'] or 0):.2f}",
                order["payment_method"],
                order["payment_status"],
                order["contact_number"],
                order["status"],
                order["payment_proof_path"],
                order["time"],
                order["status_time"],
            ]
        )

    filename = f"orders_{report_range}_{current_local_datetime().strftime('%Y%m%d')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/audit-logs")
@login_required_admin
def admin_audit_logs():
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 300").fetchall()
    conn.close()
    logs = [normalize_datetime_fields(log, ["created_at"]) for log in logs]
    return render_template("audit_logs.html", logs=logs)


@app.route("/admin/upload-qr", methods=["POST"])
@login_required_admin
def upload_admin_qr():
    qr_file = request.files.get("qr_image")
    if qr_file is None or not qr_file.filename:
        return redirect(url_for("admin") + "?qr_error=missing")

    filename = secure_filename(qr_file.filename)
    if not allowed_qr_file(filename):
        return redirect(url_for("admin") + "?qr_error=type")

    ext = filename.rsplit(".", 1)[1].lower()
    final_filename = f"admin_qr_{int(datetime.now().timestamp())}.{ext}"
    save_path = os.path.join(UPLOAD_FOLDER, final_filename)
    qr_file.save(save_path)

    relative_path = os.path.join("uploads", "qr", final_filename).replace("\\", "/")
    conn = get_db_connection()
    conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", (relative_path, "admin_qr_path"))
    record_audit(conn, "admin", ADMIN_USERNAME, "qr_updated", f"path={relative_path}")
    conn.commit()
    conn.close()

    return redirect(url_for("admin") + "?qr_success=1")


@app.route("/manage_menu")
@app.route("/manage-menu")
@login_required_admin
def manage_menu():
    conn = get_db_connection()
    menu_items = conn.execute("SELECT * FROM menu_items ORDER BY id ASC").fetchall()
    conn.close()
    return render_template("manage_menu.html", menu_items=menu_items)


@app.route("/add-menu", methods=["POST"])
@login_required_admin
def add_menu():
    menu_id_raw = (request.form.get("menu_id") or "").strip()
    emoji = (request.form.get("emoji") or "").strip()
    name = (request.form.get("name") or "").strip()
    price = request.form.get("price")
    stock_qty_raw = request.form.get("stock_qty")

    if not name or not price or stock_qty_raw is None:
        return redirect(url_for("manage_menu") + "?error=invalid")

    menu_id = None
    if menu_id_raw:
        try:
            menu_id = int(menu_id_raw)
            if menu_id <= 0:
                return redirect(url_for("manage_menu") + "?error=invalid")
        except ValueError:
            return redirect(url_for("manage_menu") + "?error=invalid")

    try:
        price_value = float(price)
        stock_qty = int(stock_qty_raw)
        if stock_qty < 0:
            return redirect(url_for("manage_menu") + "?error=invalid")
    except ValueError:
        return redirect(url_for("manage_menu") + "?error=invalid")

    conn = get_db_connection()
    try:
        created_time = now_string()
        if menu_id is None:
            conn.execute(
                "INSERT INTO menu_items (emoji, name, price, stock_qty, available, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (emoji, name, price_value, stock_qty, 1 if stock_qty > 0 else 0, created_time),
            )
        else:
            conn.execute(
                "INSERT INTO menu_items (id, emoji, name, price, stock_qty, available, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (menu_id, emoji, name, price_value, stock_qty, 1 if stock_qty > 0 else 0, created_time),
            )
        record_audit(conn, "admin", ADMIN_USERNAME, "menu_added", f"name={name}, stock={stock_qty}")
        conn.commit()
        conn.close()
        return redirect(url_for("manage_menu") + "?success=added")
    except sqlite3.IntegrityError:
        conn.close()
        return redirect(url_for("manage_menu") + "?error=duplicate")


@app.route("/edit-menu/<int:menu_id>", methods=["POST"])
@login_required_admin
def edit_menu(menu_id):
    emoji = (request.form.get("emoji") or "").strip()
    name = (request.form.get("name") or "").strip()
    price = request.form.get("price")
    stock_qty_raw = request.form.get("stock_qty")

    if not name or not price or stock_qty_raw is None:
        return redirect(url_for("manage_menu") + "?error=invalid")

    try:
        price_value = float(price)
        stock_qty = int(stock_qty_raw)
        if stock_qty < 0:
            return redirect(url_for("manage_menu") + "?error=invalid")
    except ValueError:
        return redirect(url_for("manage_menu") + "?error=invalid")

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE menu_items SET emoji = ?, name = ?, price = ?, stock_qty = ?, available = ? WHERE id = ?",
            (emoji, name, price_value, stock_qty, 1 if stock_qty > 0 else 0, menu_id),
        )
        record_audit(conn, "admin", ADMIN_USERNAME, "menu_updated", f"menu_id={menu_id}, stock={stock_qty}")
        conn.commit()
        conn.close()
        return redirect(url_for("manage_menu") + "?success=updated")
    except sqlite3.IntegrityError:
        conn.close()
        return redirect(url_for("manage_menu") + "?error=duplicate")


@app.route("/toggle-menu/<int:menu_id>")
@login_required_admin
def toggle_menu(menu_id):
    conn = get_db_connection()
    item = conn.execute("SELECT available, stock_qty FROM menu_items WHERE id = ?", (menu_id,)).fetchone()

    if item:
        next_state = 0 if item["available"] else (1 if int(item["stock_qty"] or 0) > 0 else 0)
        conn.execute("UPDATE menu_items SET available = ? WHERE id = ?", (next_state, menu_id))
        record_audit(conn, "admin", ADMIN_USERNAME, "menu_toggled", f"menu_id={menu_id}, available={next_state}")
        conn.commit()

    conn.close()
    return redirect(url_for("manage_menu") + "?success=updated")


@app.route("/delete-menu/<int:menu_id>")
@login_required_admin
def delete_menu(menu_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM menu_items WHERE id = ?", (menu_id,))
    record_audit(conn, "admin", ADMIN_USERNAME, "menu_deleted", f"menu_id={menu_id}")
    conn.commit()
    conn.close()
    return redirect(url_for("manage_menu") + "?success=deleted")


@app.route("/update_status/<int:order_id>/<status>")
@login_required_admin
def update_status(order_id, status):
    valid_statuses = {"Pending", "Preparing", "Ready", "Delivered"}
    if status not in valid_statuses:
        return redirect(url_for("admin"))

    conn = get_db_connection()
    status_time = now_string()
    current = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.execute(
        "UPDATE orders SET status = ?, status_time = ? WHERE id = ?",
        (status, status_time, order_id),
    )
    record_audit(
        conn,
        "admin",
        ADMIN_USERNAME,
        "order_status_updated",
        f"order_id={order_id}, from={current['status'] if current else ''}, to={status}",
    )
    if current and current["status"] != status:
        record_status_history(conn, order_id, status, ADMIN_USERNAME, "Status updated by admin")
    conn.commit()
    conn.close()

    return redirect(url_for("admin"))


@app.route("/admin/mark-cash-paid/<int:order_id>")
@login_required_admin
def mark_cash_paid(order_id):
    conn = get_db_connection()
    order = conn.execute(
        "SELECT payment_method, payment_status FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if order and order["payment_method"] == "Cash" and order["payment_status"] != "Paid":
        conn.execute(
            "UPDATE orders SET payment_status = ?, status_time = ? WHERE id = ?",
            ("Paid", now_string(), order_id),
        )
        record_audit(conn, "admin", ADMIN_USERNAME, "cash_marked_paid", f"order_id={order_id}")
        conn.commit()

    conn.close()
    return redirect(url_for("admin"))


@app.route("/admin/finance")
@login_required_admin
def admin_finance():
    status_query = (request.args.get("status") or "").strip()
    risk_query = (request.args.get("risk_tier") or "").strip()
    order_id_query = (request.args.get("order_id") or "").strip()

    if status_query not in FINANCIAL_CASE_STATUSES:
        status_query = ""
    if risk_query not in FINANCIAL_RISK_TIERS:
        risk_query = ""

    order_id_filter = None
    if order_id_query:
        try:
            order_id_filter = int(order_id_query)
        except ValueError:
            order_id_query = ""

    sql = """
        SELECT
            fc.*,
            o.username,
            o.contact_number,
            o.menu,
            o.total_price,
            o.payment_method,
            o.payment_status,
            o.status AS order_status,
            o.time AS order_time
        FROM financial_case fc
        JOIN orders o ON o.id = fc.order_id
        WHERE 1=1
    """
    params = []
    if status_query:
        sql += " AND fc.status = ?"
        params.append(status_query)
    if risk_query:
        sql += " AND fc.risk_tier = ?"
        params.append(risk_query)
    if order_id_filter is not None:
        sql += " AND fc.order_id = ?"
        params.append(order_id_filter)
    sql += " ORDER BY fc.id DESC"

    conn = get_db_connection()
    cases = conn.execute(sql, params).fetchall()
    all_cases = conn.execute("SELECT * FROM financial_case").fetchall()
    candidate_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM orders o
        LEFT JOIN financial_case fc ON fc.order_id = o.id
        WHERE o.status = 'Delivered'
          AND o.payment_status != 'Paid'
          AND COALESCE(o.total_price, 0) > 0
          AND fc.id IS NULL
        """
    ).fetchone()["c"]
    candidate_orders = conn.execute(
        """
        SELECT o.*
        FROM orders o
        LEFT JOIN financial_case fc ON fc.order_id = o.id
        WHERE o.status = 'Delivered'
          AND o.payment_status != 'Paid'
          AND COALESCE(o.total_price, 0) > 0
          AND fc.id IS NULL
        ORDER BY o.id DESC
        LIMIT 25
        """
    ).fetchall()
    recent_actions = conn.execute(
        """
        SELECT
            ca.*,
            fc.order_id,
            o.username
        FROM case_action ca
        JOIN financial_case fc ON fc.id = ca.case_id
        JOIN orders o ON o.id = fc.order_id
        ORDER BY ca.id DESC
        LIMIT 12
        """
    ).fetchall()
    pending_action_count = conn.execute(
        "SELECT COUNT(*) AS c FROM case_action WHERE status = 'pending'"
    ).fetchone()["c"]
    conn.close()

    now_dt = current_local_datetime()
    active_cases = [case for case in all_cases if case["status"] not in FINANCIAL_CASE_CLOSED_STATUSES]
    due_followups = [
        case
        for case in active_cases
        if is_follow_up_overdue(case["follow_up_due_at"], now_dt)
    ]
    finance_stats = {
        "total": len(all_cases),
        "active": len(active_cases),
        "escalated": sum(1 for case in all_cases if case["status"] == "Escalated"),
        "resolved": sum(1 for case in all_cases if case["status"] in FINANCIAL_CASE_CLOSED_STATUSES),
        "due_followups": len(due_followups),
        "candidates": candidate_count,
        "pending_actions": pending_action_count,
    }

    cases = [
        normalize_datetime_fields(
            dict(case, is_follow_up_overdue=is_follow_up_overdue(case["follow_up_due_at"], now_dt)),
            ["created_at", "updated_at", "resolved_at", "follow_up_due_at", "order_time"],
        )
        for case in cases
    ]
    candidate_orders = [
        normalize_datetime_fields(order, ["time", "status_time"]) for order in candidate_orders
    ]
    recent_actions = [
        normalize_datetime_fields(action, ["created_at"]) for action in recent_actions
    ]
    filters = {
        "status": status_query,
        "risk_tier": risk_query,
        "order_id": order_id_query,
    }

    return render_template(
        "admin_finance.html",
        cases=cases,
        candidate_orders=candidate_orders,
        recent_actions=recent_actions,
        finance_stats=finance_stats,
        filters=filters,
        case_statuses=FINANCIAL_CASE_STATUSES,
        risk_tiers=FINANCIAL_RISK_TIERS,
    )


@app.route("/admin/finance/case/create", methods=["POST"])
@login_required_admin
def admin_create_financial_case():
    order_id_raw = (request.form.get("order_id") or "").strip()
    try:
        order_id = int(order_id_raw)
    except ValueError:
        return redirect(url_for("admin_finance") + "?error=invalid_order")

    conn = get_db_connection()
    case_id, outcome = create_financial_case_for_order(conn, order_id, ADMIN_USERNAME)
    if outcome == "order_not_found":
        conn.close()
        return redirect(url_for("admin_finance") + "?error=order_not_found")

    if outcome == "created":
        record_audit(
            conn,
            "admin",
            ADMIN_USERNAME,
            "financial_case_opened",
            f"financial_case_id={case_id}, order_id={order_id}",
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?created=1")

    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?existing=1")


@app.route("/admin/finance/case/<int:case_id>")
@login_required_admin
def admin_financial_case_detail(case_id):
    conn = get_db_connection()
    case = conn.execute(
        """
        SELECT
            fc.*,
            o.username,
            o.contact_number,
            o.menu,
            o.total_price,
            o.payment_method,
            o.payment_status,
            o.payment_reference,
            o.payment_proof_path,
            o.status AS order_status,
            o.time AS order_time,
            o.status_time AS order_status_time
        FROM financial_case fc
        JOIN orders o ON o.id = fc.order_id
        WHERE fc.id = ?
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    order_id = case["order_id"]
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    items = get_finance_order_items(conn, order)
    item_summary = summarize_finance_order_items(items)
    timeline = get_order_timeline(conn, order_id)
    evidence = conn.execute(
        "SELECT * FROM case_evidence WHERE case_id = ? ORDER BY id DESC",
        (case_id,),
    ).fetchall()
    reasoning = conn.execute(
        "SELECT * FROM case_reasoning WHERE case_id = ? ORDER BY id DESC",
        (case_id,),
    ).fetchall()
    latest_reasoning_id = latest_approvable_reasoning_id(conn, case_id)
    actions = conn.execute(
        "SELECT * FROM case_action WHERE case_id = ? ORDER BY id DESC",
        (case_id,),
    ).fetchall()
    logs = conn.execute(
        """
        SELECT * FROM audit_logs
        WHERE details LIKE ? OR details LIKE ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (f"%financial_case_id={case_id}%", f"%order_id={order_id}%"),
    ).fetchall()
    conn.close()

    case = normalize_datetime_fields(
        dict(case, is_follow_up_overdue=is_follow_up_overdue(case["follow_up_due_at"])),
        ["created_at", "updated_at", "resolved_at", "follow_up_due_at", "order_time", "order_status_time"],
    )
    order = normalize_datetime_fields(order, ["time", "status_time"])
    timeline = [normalize_datetime_fields(item, ["changed_at"]) for item in timeline]
    evidence = [normalize_datetime_fields(item, ["captured_at"]) for item in evidence]
    reasoning = [normalize_datetime_fields(item, ["created_at", "reviewed_at"]) for item in reasoning]
    actions = [normalize_datetime_fields(item, ["created_at", "updated_at"]) for item in actions]
    logs = [normalize_datetime_fields(log, ["created_at"]) for log in logs]

    return render_template(
        "admin_financial_case_detail.html",
        case=case,
        order=order,
        order_items=items,
        legacy_order_items_used=item_summary["uses_legacy_fallback"],
        timeline=timeline,
        evidence=evidence,
        reasoning=reasoning,
        latest_reasoning_id=latest_reasoning_id,
        actions=actions,
        logs=logs,
        case_statuses=FINANCIAL_CASE_STATUSES,
        risk_tiers=FINANCIAL_RISK_TIERS,
    )


@app.route("/admin/finance/case/<int:case_id>/update", methods=["POST"])
@login_required_admin
def admin_update_financial_case(case_id):
    conn = get_db_connection()
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    status = normalize_financial_choice(request.form.get("status"), FINANCIAL_CASE_STATUSES, case["status"])
    # risk_tier/risk_score/confidence are intentionally NOT accepted here. They may only
    # change via an approved case_reasoning entry (see review_financial_case_reasoning),
    # so every risk-field change stays gated behind that audited approval workflow instead
    # of being freely overwritable from this general lifecycle form.
    follow_up_due_at = normalize_follow_up_datetime(request.form.get("follow_up_due_at"))
    updated_at = now_string()
    resolved_at = case["resolved_at"]

    if status in FINANCIAL_CASE_CLOSED_STATUSES and not resolved_at:
        resolved_at = updated_at
    elif status not in FINANCIAL_CASE_CLOSED_STATUSES:
        resolved_at = ""

    conn.execute(
        """
        UPDATE financial_case
        SET status = ?,
            updated_at = ?,
            resolved_at = ?,
            follow_up_due_at = ?
        WHERE id = ?
        """,
        (
            status,
            updated_at,
            resolved_at,
            follow_up_due_at,
            case_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            case_id,
            "case_updated",
            ADMIN_USERNAME,
            f"status={status}, follow_up_due_at={follow_up_due_at or '-'}",
            updated_at,
        ),
    )
    record_audit(
        conn,
        "admin",
        ADMIN_USERNAME,
        "financial_case_updated",
        f"financial_case_id={case_id}, order_id={case['order_id']}, status={status}",
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?updated=1")


@app.route("/admin/finance/case/<int:case_id>/analysis", methods=["POST"])
@login_required_admin
def admin_run_financial_case_analysis(case_id):
    conn = get_db_connection()
    result = analyze_financial_case(conn, case_id, ADMIN_USERNAME, "manual")
    if result is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    conn.commit()
    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?analysis=1")


@app.route("/admin/finance/case/<int:case_id>/reasoning/<int:reasoning_id>/approve", methods=["POST"])
@login_required_admin
def admin_approve_financial_case_reasoning(case_id, reasoning_id):
    return handle_financial_case_reasoning_review(case_id, reasoning_id, "APPROVED")


@app.route("/admin/finance/case/<int:case_id>/reasoning/<int:reasoning_id>/reject", methods=["POST"])
@login_required_admin
def admin_reject_financial_case_reasoning(case_id, reasoning_id):
    return handle_financial_case_reasoning_review(case_id, reasoning_id, "REJECTED")


@app.route("/admin/finance/case/<int:case_id>/evidence", methods=["POST"])
@login_required_admin
def admin_capture_financial_case_evidence(case_id):
    conn = get_db_connection()
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    if not capture_financial_case_evidence(conn, case_id, case["order_id"], "manual_refresh"):
        conn.close()
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?error=evidence_failed")

    created_at = now_string()
    conn.execute("UPDATE financial_case SET updated_at = ? WHERE id = ?", (created_at, case_id))
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, "evidence_captured", ADMIN_USERNAME, "Captured current OMS order/payment snapshot.", created_at),
    )
    record_audit(
        conn,
        "admin",
        ADMIN_USERNAME,
        "financial_case_evidence_captured",
        f"financial_case_id={case_id}, order_id={case['order_id']}",
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?evidence=1")


@app.route("/admin/finance/case/<int:case_id>/reasoning", methods=["POST"])
@login_required_admin
def admin_add_financial_case_reasoning(case_id):
    hypothesis = (request.form.get("hypothesis") or "").strip()
    chosen_action = (request.form.get("chosen_action") or "").strip()
    rejected_alternatives = (request.form.get("rejected_alternatives") or "").strip()
    risk_tier = normalize_risk_tier(request.form.get("risk_tier"), "Unscored")
    risk_score = parse_percentage(request.form.get("risk_score"), 0.0)
    confidence = parse_percentage(request.form.get("confidence"), 0.0)

    if not any([hypothesis, chosen_action, rejected_alternatives]):
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?error=empty_reasoning")

    conn = get_db_connection()
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    created_at = now_string()
    conn.execute(
        """
        INSERT INTO case_reasoning (
            case_id, evidence_snapshot_id, hypothesis, risk_tier, risk_score, confidence,
            chosen_action, rejected_alternatives, created_at,
            requires_human_approval, reasoning_summary, analysis_source,
            approval_state, reviewed_at, reviewed_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            None,
            hypothesis,
            risk_tier,
            risk_score,
            confidence,
            chosen_action,
            rejected_alternatives,
            created_at,
            0,  # Manual intervention doesn't "require" human approval because it IS human.
            "Manual admin reasoning.",
            "admin_manual",
            "APPROVED",
            created_at,
            ADMIN_USERNAME,
        ),
    )
    conn.execute("UPDATE financial_case SET updated_at = ? WHERE id = ?", (created_at, case_id))
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, "reasoning_recorded", ADMIN_USERNAME, chosen_action or "Manual reasoning note added.", created_at),
    )
    record_audit(
        conn,
        "admin",
        ADMIN_USERNAME,
        "financial_case_reasoning_added",
        f"financial_case_id={case_id}, order_id={case['order_id']}",
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?reasoning=1")


@app.route("/admin/finance/case/<int:case_id>/action", methods=["POST"])
@login_required_admin
def admin_add_financial_case_action(case_id):
    action_type = (request.form.get("action_type") or "").strip()
    outcome = (request.form.get("outcome") or "").strip()
    if not action_type or not outcome:
        return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?error=empty_action")

    conn = get_db_connection()
    case = conn.execute("SELECT * FROM financial_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        conn.close()
        return redirect(url_for("admin_finance") + "?error=case_not_found")

    created_at = now_string()
    conn.execute(
        """
        INSERT INTO case_action (case_id, action_type, initiated_by, outcome, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, action_type, ADMIN_USERNAME, outcome, created_at),
    )
    conn.execute("UPDATE financial_case SET updated_at = ? WHERE id = ?", (created_at, case_id))
    record_audit(
        conn,
        "admin",
        ADMIN_USERNAME,
        "financial_case_action_added",
        f"financial_case_id={case_id}, order_id={case['order_id']}, action={action_type}",
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_financial_case_detail", case_id=case_id) + "?action=1")


@app.route("/admin/finance/case/<int:case_id>/action/<int:action_id>/dispatch", methods=["POST"])
@login_required_admin
def admin_dispatch_case_action(case_id, action_id):
    conn = get_db_connection()
    try:
        outcome = dispatch_case_action(conn, case_id, action_id, ADMIN_USERNAME)
        if outcome == "dispatched":
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        outcome = "dispatch_failed"
    finally:
        conn.close()
    return financial_case_action_redirect(case_id, outcome)


@app.route("/admin/finance/case/<int:case_id>/action/<int:action_id>/override", methods=["POST"])
@login_required_admin
def admin_override_case_action(case_id, action_id):
    reason = (request.form.get("reason") or "").strip()
    conn = get_db_connection()
    try:
        outcome = override_case_action(conn, case_id, action_id, reason, ADMIN_USERNAME)
        if outcome == "overridden":
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        outcome = "override_failed"
    finally:
        conn.close()
    return financial_case_action_redirect(case_id, outcome)


@app.route("/admin/finance/case/<int:case_id>/follow-up/complete", methods=["POST"])
@login_required_admin
def admin_complete_case_follow_up(case_id):
    conn = get_db_connection()
    try:
        outcome = complete_case_follow_up(conn, case_id, ADMIN_USERNAME)
        if outcome == "followup_completed":
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        outcome = "followup_complete_failed"
    finally:
        conn.close()
    return financial_case_action_redirect(case_id, outcome)


@app.route("/admin/finance/case/<int:case_id>/escalate", methods=["POST"])
@login_required_admin
def admin_escalate_financial_case(case_id):
    reason = (request.form.get("reason") or "").strip()
    conn = get_db_connection()
    try:
        outcome = escalate_financial_case(conn, case_id, reason, ADMIN_USERNAME)
        if outcome == "escalated":
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        outcome = "escalation_failed"
    finally:
        conn.close()
    return financial_case_action_redirect(case_id, outcome)

# ==========================================
# WEBSITE CONTENT (CMS) - HERO SECTION
# ==========================================

def init_content_db():
    """Initialize website content table"""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS website_content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            field_key TEXT NOT NULL,
            field_value TEXT NOT NULL,
            field_type TEXT DEFAULT 'text',
            updated_at TEXT NOT NULL,
            UNIQUE(section, field_key)
        )
    ''')
    
    # Insert default Hero content if empty
    existing = conn.execute("SELECT COUNT(*) as c FROM website_content WHERE section='hero'").fetchone()
    if existing["c"] == 0:
        default_hero = [
            ("hero", "badge_text", "ISO 22000:2018 Certified Facilities", "text"),
            ("hero", "title_line1", "Quality Food.", "text"),
            ("hero", "title_line2", "Trusted Service.", "text"),
            ("hero", "description", "Providing hygienic, highly structured, and professionally managed food services for corporate workplaces, IT hubs, and multi-tenant business parks across Bengaluru.", "textarea"),
            ("hero", "btn1_text", "Explore Services", "text"),
            ("hero", "btn1_link", "#services", "text"),
            ("hero", "btn2_text", "Request a Quote", "text"),
            ("hero", "btn2_link", "#contact", "text"),
            ("hero", "stat1_number", "45+", "text"),
            ("hero", "stat1_label", "Tech Parks Managed", "text"),
            ("hero", "stat2_number", "25k+", "text"),
            ("hero", "stat2_label", "Daily Active Portions", "text"),
            ("hero", "bg_image", "https://images.unsplash.com/photo-1497366216548-37526070297c?auto=format&fit=crop&w=1920&q=80", "image"),
            ("hero", "scroll_text", "Scroll to Discover", "text"),
        ]
        now = now_string()
        conn.executemany(
            "INSERT INTO website_content (section, field_key, field_value, field_type, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(s, k, v, t, now) for s, k, v, t in default_hero]
        )
    conn.commit()
    conn.close()

# Call this on startup
init_content_db()


# ==========================================
# CMS API ROUTES
# ==========================================

# Get all content for a section (used by React)
@app.route("/api/content/<section>", methods=["GET"])
def api_get_section_content(section):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT field_key, field_value, field_type FROM website_content WHERE section = ?",
        (section,)
    ).fetchall()
    conn.close()
    
    # Convert to easy dictionary: { "title_line1": "Quality Food.", ... }
    content = {row["field_key"]: row["field_value"] for row in rows}
    
    return jsonify({
        "success": True,
        "section": section,
        "content": content
    })


# Get ALL content (used by admin panel)
@app.route("/api/content", methods=["GET"])
def api_get_all_content():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM website_content ORDER BY section, id").fetchall()
    conn.close()
    
    content = [dict(row) for row in rows]
    return jsonify({"success": True, "content": content})


# Update content (admin only)
@app.route("/api/content/update", methods=["POST"])
@login_required_admin
def api_update_content():
    data = request.json
    section = data.get("section")
    field_key = data.get("field_key")
    field_value = data.get("field_value")
    
    if not section or not field_key:
        return jsonify({"success": False, "error": "Missing fields"}), 400
    
    conn = get_db_connection()
    conn.execute(
        "UPDATE website_content SET field_value = ?, updated_at = ? WHERE section = ? AND field_key = ?",
        (field_value, now_string(), section, field_key)
    )
    record_audit(conn, "admin", ADMIN_USERNAME, "content_updated", f"section={section}, field={field_key}")
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "message": "Content updated"})


# ==========================================
# ADMIN CMS PAGE
# ==========================================

@app.route("/admin/content", methods=["GET"])
@login_required_admin
def admin_content():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM website_content ORDER BY section, id").fetchall()
    conn.close()
    
    # Group by section
    sections = {}
    for row in rows:
        section = row["section"]
        if section not in sections:
            sections[section] = []
        sections[section].append(dict(row))
    
    return render_template("admin_content.html", sections=sections)


# Handle admin form submission
@app.route("/admin/content/save", methods=["POST"])
@login_required_admin
def admin_content_save():
    conn = get_db_connection()
    now = now_string()
    
    # Loop through all submitted fields
    for key, value in request.form.items():
        # Field name format: "hero__title_line1"
        if "__" in key:
            section, field_key = key.split("__", 1)
            conn.execute(
                "UPDATE website_content SET field_value = ?, updated_at = ? WHERE section = ? AND field_key = ?",
                (value, now, section, field_key)
            )
    
    record_audit(conn, "admin", ADMIN_USERNAME, "content_bulk_updated", "Website content updated")
    conn.commit()
    conn.close()
    
    return redirect(url_for("admin_content") + "?saved=1")

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"},
            )
