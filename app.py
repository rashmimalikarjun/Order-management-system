import os
import csv
import io
import json
import random
import string
import urllib.request
import urllib.error
from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
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
CASE_ACTION_STATUSES = ("pending", "completed", "overridden")

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

    if ai_data["risk_tier"] not in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]:
        return False

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

    for key in ["hypothesis", "recommended_action", "reasoning_summary"]:
        val = ai_data[key]
        if not isinstance(val, str) or not val.strip():
            return False

    rejected = ai_data["rejected_alternatives"]
    if not isinstance(rejected, list):
        return False
    if not all(isinstance(item, str) for item in rejected):
        return False

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


def queue_controlled_action(conn, case_id, chosen_action, initiated_by, created_at):
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


def seed_realistic_historical_data(conn):
    count = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    if count > 0:
        return

    clients = [
        "Infosys - Electronic City", "Wipro - Sarjapur", "Accenture - Whitefield",
        "TCS - Electronic City", "IBM - Manyata Tech Park", "Deloitte - Outer Ring Road",
        "EY - Whitefield", "Bosch - Adugodi", "SAP Labs - Whitefield",
        "Oracle - Devanahalli", "Goldman Sachs - Koramangala", "Target - Koramangala",
        "Ananya Rao", "Arjun Mehta", "Kavya Nair", "Rahul Menon", "Sneha Iyer",
        "Rohan Kulkarni", "Priya Sharma", "Aditya Verma"
    ]

    menu_rows = conn.execute("SELECT * FROM menu_items").fetchall()
    if not menu_rows:
        return

    now = current_local_datetime()

    for _ in range(75):
        days_ago = random.randint(1, 60)
        order_dt = now - timedelta(days=days_ago)
        hour = random.choice([8, 9, 12, 13, 15, 16])
        order_dt = order_dt.replace(hour=hour, minute=random.randint(0, 59))
        order_time_str = order_dt.strftime(DISPLAY_DATETIME_FORMAT)

        client = random.choice(clients)
        num_items = random.randint(1, 3)
        chosen_items = random.sample(menu_rows, num_items)
        
        cart = []
        subtotal = 0.0
        total_qty = 0
        for item in chosen_items:
            qty = random.choice([5, 8, 12, 18, 25, 35, 50])
            if "Buffet" in item["name"] or "Package" in item["name"]:
                qty = random.choice([25, 50, 100, 150])
            elif "Coffee" in item["name"] or "Water" in item["name"] or "Tea" in item["name"]:
                qty = random.choice([15, 30, 50, 75])

            sub = float(qty * item["price"])
            cart.append({"id": item["id"], "name": item["name"], "price": item["price"], "qty": qty, "sub": sub})
            subtotal += sub
            total_qty += qty

        menu_summary = ", ".join([f"{c['name']} x{c['qty']}" for c in cart])

        status = "Delivered"
        payment_status = "Paid"
        payment_ref = f"PAY-{order_dt.strftime('%Y%m%d')}-{random.randint(10000, 99999)}"

        rand_val = random.random()
        if rand_val < 0.05:
            status = "Cancelled"
            payment_status = "Pending"
            payment_ref = ""
        elif rand_val < 0.15:
            status = "Delivered"
            payment_status = "Pending"
            payment_ref = ""

        cursor = conn.execute(
            """
            INSERT INTO orders (
                username, menu, quantity, time, status, status_time, total_price,
                payment_method, payment_status, payment_reference, contact_number, payment_proof_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (client, menu_summary, total_qty, order_time_str, status, order_time_str,
             subtotal, "UPI QR", payment_status, payment_ref, "9876543210", "")
        )
        order_id = cursor.lastrowid

        for c in cart:
            conn.execute(
                """
                INSERT INTO order_items (order_id, menu_item_id, item_name, item_price, quantity, subtotal)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (order_id, c["id"], c["name"], c["price"], c["qty"], c["sub"])
            )

        record_audit(conn, "user", client, "order_placed", f"order_id={order_id}, total={subtotal:.2f}")
        conn.execute(
            "INSERT INTO order_status_history (order_id, status, changed_at, changed_by, note) VALUES (?, ?, ?, ?, ?)",
            (order_id, status, order_time_str, "system_seed", "Historical record generated.")
        )

        if status == "Delivered" and payment_status == "Pending" and days_ago > 5 and random.random() < 0.4:
            create_financial_case_for_order(conn, order_id, "system_seed")


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
            ("🍱", "South Indian Veg Meals", 160.0, 100, 1, created_time),
            ("🍛", "Executive Non-Veg Lunch", 350.0, 100, 1, created_time),
            ("🥪", "Meeting Snack Package", 120.0, 100, 1, created_time),
            ("🍽️", "Corporate Buffet - Mixed", 650.0, 100, 1, created_time),
            ("☕", "Filter Coffee & Cookies", 80.0, 100, 1, created_time),
            ("🌯", "Paneer Roll Combo", 140.0, 100, 1, created_time),
            ("🥞", "Continental Breakfast Box", 180.0, 100, 1, created_time),
        ]
        conn.executemany(
            "INSERT INTO menu_items (emoji, name, price, stock_qty, available, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            default_items,
        )

    # Automatically generate realistic corporate historical data
    seed_realistic_historical_data(conn)

    conn.commit()
    conn.close()


init_db()
log_startup_warnings()


def generate_reconciliation_order(conn, amount, ref_code, is_paid=False, username_hint="recon_demo"):
    """Used for dynamically topping up the reconciliation pool without using
    dummy sounding names. Replaces the old `recon_demo_ref_0001` format."""
    now = current_local_datetime()
    time_str = now.strftime(DISPLAY_DATETIME_FORMAT)
    
    clients = ["Infosys - EC", "Wipro - SJ", "TCS - EC", "Deloitte - ORR", "EY - WF"]
    username = f"{random.choice(clients)} (Ref: {random.randint(1000, 9999)})"
    
    status = "Delivered"
    payment_status = "Paid" if is_paid else "Pending"
    
    conn.execute(
        """
        INSERT INTO orders (
            username, menu, quantity, time, status, status_time, total_price,
            payment_method, payment_status, payment_reference, contact_number, payment_proof_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (username, "Reconciliation catering batch", 1, time_str, status, time_str,
         amount, "UPI QR", payment_status, ref_code, "", "")
    )


def _recon_reference_code(prefix):
    suffix = "".join(random.choices(string.digits, k=5))
    date_str = current_local_datetime().strftime("%Y%m%d")
    if prefix == "RZP": return f"pay_{suffix}"
    if prefix == "UTR": return f"UTR{date_str}{suffix}"
    return f"SET-{date_str}-{suffix}"


def ensure_reconciliation_seed_orders(conn):
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

    # We need a robust mix of amounts to avoid fallback collisions
    amounts = [float(random.randint(150, 4000)) + (i * 1.5) for i in range(100)]
    
    for _ in range(max(0, RECON_TARGET_OPEN_WITH_REF - open_with_ref)):
        amt = amounts.pop(0) if amounts else random.randint(100, 5000)
        generate_reconciliation_order(conn, amt, _recon_reference_code(random.choice(["RZP", "UTR", "SET"])), False)

    for _ in range(max(0, RECON_TARGET_OPEN_WITHOUT_REF - open_without_ref)):
        amt = amounts.pop(0) if amounts else random.randint(100, 5000)
        generate_reconciliation_order(conn, amt, "", False)

    for _ in range(max(0, RECON_TARGET_PAID - paid_count)):
        amt = amounts.pop(0) if amounts else random.randint(100, 5000)
        generate_reconciliation_order(conn, amt, _recon_reference_code("RZP"), True)

    record_audit(conn, "system", "reconciliation_agent", "reconciliation_seed_orders_topped_up", "Completed")


def generate_settlement_batch(conn):
    ensure_reconciliation_seed_orders(conn)

    orders = conn.execute(
        "SELECT id, total_price, payment_status, payment_reference FROM orders"
    ).fetchall()

    open_with_ref = [o for o in orders if o["payment_status"] != "Paid" and (o["payment_reference"] or "").strip() and float(o["total_price"] or 0) > 0]
    open_without_ref = [o for o in orders if o["payment_status"] != "Paid" and not (o["payment_reference"] or "").strip() and float(o["total_price"] or 0) > 0]
    paid_orders = [o for o in orders if o["payment_status"] == "Paid" and float(o["total_price"] or 0) > 0]

    random.shuffle(open_with_ref)
    random.shuffle(open_without_ref)
    random.shuffle(paid_orders)

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
        settlements.append(dict(row))

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
    }

    for settlement in settlements:
        ext_ref = (settlement.get("external_ref") or "").strip()
        amount = round(float(settlement.get("amount", 0) or 0), 2)
        settled_at = settlement.get("settled_at", "")
        source = settlement.get("source", "")

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
    settlements = generate_settlement_batch(conn)
    outcome = reconcile_settlement_batch(conn, settlements)
    counts = outcome["counts"]
    created_at = now_string()

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
            created_at, triggered_by, outcome["total"], counts["matched"],
            counts["amount_mismatch"], counts["duplicate_settlement"],
            counts["no_matching_order"], counts["already_reconciled"],
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

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"},
    )
