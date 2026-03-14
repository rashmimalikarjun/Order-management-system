import os
import csv
import io
from flask import Flask, render_template, request, redirect, url_for, session, Response
from datetime import datetime, timedelta
import sqlite3
from urllib.parse import urlencode
from functools import wraps
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
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
ALLOWED_QR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
APP_TIMEZONE = ZoneInfo("Asia/Kolkata")
DISPLAY_DATETIME_FORMAT = "%I:%M %p | %d %b %Y"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def running_on_render():
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))


def admin_uses_default_credentials():
    return (
        ADMIN_USERNAME == DEFAULT_ADMIN_USERNAME
        and ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD
    )


def admin_login_enabled():
    return app.debug or not admin_uses_default_credentials()


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
            ("VEG", "Veg Meal", 120.0, 100, 1, created_time),
            ("NV", "Non-Veg Meal", 180.0, 100, 1, created_time),
            ("PR", "Paneer Rice", 140.0, 100, 1, created_time),
            ("CB", "Chicken Biryani", 200.0, 100, 1, created_time),
            ("MM", "Mini Meals", 90.0, 100, 1, created_time),
        ]
        conn.executemany(
            "INSERT INTO menu_items (emoji, name, price, stock_qty, available, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            default_items,
        )

    conn.commit()
    conn.close()


init_db()
log_startup_warnings()


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
        payment_reference = (request.form.get("payment_reference") or "").strip()
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
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        if payment_mode == "upi" and not payment_reference:
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
        else:
            payment_method = "UPI QR"
            payment_status = "Paid"

        cursor = conn.execute(
            """
            INSERT INTO orders (username, menu, quantity, time, status, status_time, total_price, payment_method, payment_status, payment_reference)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        order_id = cursor.lastrowid

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
    conn.close()

    order = normalize_datetime_fields(order, ["time", "status_time"])
    return render_template("success.html", order=order, order_items=items)


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
    logs = conn.execute(
        "SELECT * FROM audit_logs WHERE details LIKE ? ORDER BY id DESC",
        (f"%order_id={order_id}%",),
    ).fetchall()
    conn.close()
    order = normalize_datetime_fields(order, ["time", "status_time"])
    logs = [normalize_datetime_fields(log, ["created_at"]) for log in logs]
    return render_template("admin_order_detail.html", order=order, order_items=items, logs=logs)


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
            "status",
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
                order["status"],
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

    if not emoji:
        emoji = "ITEM"

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

    if not emoji:
        emoji = "ITEM"

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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"},
    )
