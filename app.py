from flask import Flask, render_template, request, redirect, url_for, session
from datetime import datetime
import sqlite3
from urllib.parse import urlencode, quote_plus

app = Flask(__name__)
app.secret_key = "order-management-secret"

DATABASE = "catering.db"
UPI_ID = "your-upi-id@okbank"
UPI_NAME = "Order Management System"


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def now_string():
    return datetime.now().strftime("%I:%M %p | %d %b %Y")


def parse_order_datetime(order_time):
    try:
        return datetime.strptime(order_time, "%I:%M %p | %d %b %Y")
    except (TypeError, ValueError):
        return datetime.min


def build_upi_link(amount, note):
    params = {
        "pa": UPI_ID,
        "pn": UPI_NAME,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note[:50],
    }
    return "upi://pay?" + urlencode(params)


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

        qty = max(1, int(qty_raw))
        price = float(row["price"])
        subtotal = price * qty
        total += subtotal

        items.append(
            {
                "id": row["id"],
                "emoji": row["emoji"],
                "name": row["name"],
                "price": price,
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
            available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )

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

    existing_count = conn.execute("SELECT COUNT(*) AS count FROM menu_items").fetchone()["count"]
    if existing_count == 0:
        created_time = now_string()
        default_items = [
            ("VEG", "Veg Meal", 120.0, 1, created_time),
            ("NV", "Non-Veg Meal", 180.0, 1, created_time),
            ("PR", "Paneer Rice", 140.0, 1, created_time),
            ("CB", "Chicken Biryani", 200.0, 1, created_time),
            ("MM", "Mini Meals", 90.0, 1, created_time),
        ]
        conn.executemany(
            "INSERT INTO menu_items (emoji, name, price, available, created_at) VALUES (?, ?, ?, ?, ?)",
            default_items,
        )

    conn.commit()
    conn.close()


init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        role = request.form.get("role")
        if role == "user":
            return redirect(url_for("order"))
        if role == "admin":
            return redirect(url_for("admin"))
    return render_template("index.html")


@app.route("/order", methods=["GET", "POST"])
def order():
    conn = get_db_connection()

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        payment_reference = (request.form.get("payment_reference") or "").strip()
        cart_items, total_price = get_cart_items(conn)
        upi_link = build_upi_link(total_price, f"Food order by {username or 'customer'}")
        qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + quote_plus(upi_link)

        if not username:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=cart_items,
                cart_total=total_price,
                cart_count=cart_count(get_cart()),
                error="username_required",
                username=username,
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

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
                payment_reference=payment_reference,
                upi_link=upi_link,
                qr_url=qr_url,
                upi_id=UPI_ID,
                upi_name=UPI_NAME,
            )

        if not payment_reference:
            menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
            conn.close()
            return render_template(
                "order.html",
                menu_items=menu_items,
                cart_items=cart_items,
                cart_total=total_price,
                cart_count=cart_count(get_cart()),
                error="payment_required",
                username=username,
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
                "UPI QR",
                "Paid",
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

        conn.commit()
        conn.close()

        save_cart({})
        return redirect(url_for("success", order_id=order_id))

    menu_items = conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY id ASC").fetchall()
    cart_items, total_price = get_cart_items(conn)
    conn.close()
    upi_link = build_upi_link(total_price, f"Food order by {(session.get('last_username') or 'customer')}")
    qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + quote_plus(upi_link)

    return render_template(
        "order.html",
        menu_items=menu_items,
        cart_items=cart_items,
        cart_total=total_price,
        cart_count=cart_count(get_cart()),
        username=(session.get("last_username") or ""),
        payment_reference="",
        upi_link=upi_link,
        qr_url=qr_url,
        upi_id=UPI_ID,
        upi_name=UPI_NAME,
    )


@app.route("/cart/add", methods=["POST"])
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
    item = conn.execute("SELECT id FROM menu_items WHERE id = ? AND available = 1", (item_id_int,)).fetchone()
    conn.close()
    if not item:
        return redirect(url_for("order"))

    cart = get_cart()
    key = str(item_id_int)
    current_qty = int(cart.get(key, 0))
    cart[key] = current_qty + qty
    save_cart(cart)

    return redirect(url_for("order"))


@app.route("/cart/update", methods=["POST"])
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
        cart[key] = min(qty, 100)

    save_cart(cart)
    return redirect(url_for("order"))


@app.route("/cart/remove/<int:item_id>")
def remove_from_cart(item_id):
    cart = get_cart()
    cart.pop(str(item_id), None)
    save_cart(cart)
    return redirect(url_for("order"))


@app.route("/success", methods=["GET"])
def success():
    order_id = request.args.get("order_id")
    if order_id is None:
        return redirect(url_for("index"))

    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        conn.close()
        return redirect(url_for("index"))

    items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,)).fetchall()
    conn.close()

    return render_template("success.html", order=order, order_items=items)


@app.route("/my-orders", methods=["GET", "POST"])
def my_orders():
    user_orders = []
    username = request.form.get("username") if request.method == "POST" else request.args.get("username")

    if username:
        username = username.strip()
        conn = get_db_connection()
        user_orders = conn.execute(
            "SELECT * FROM orders WHERE username = ? ORDER BY id DESC",
            (username,),
        ).fetchall()
        conn.close()

    return render_template("my_orders.html", orders=user_orders, username=username)


@app.route("/cancel-order/<int:order_id>")
def cancel_order(order_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    if order and order["status"] == "Pending":
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("my_orders") + "?cancelled=true&username=" + order["username"])

    conn.close()
    return redirect(
        url_for("my_orders") + "?error=cannot_cancel&username=" + (order["username"] if order else "")
    )


@app.route("/status", methods=["GET", "POST"])
def status():
    user_orders = []
    if request.method == "POST":
        username = request.form.get("username")
        conn = get_db_connection()
        user_orders = conn.execute(
            "SELECT * FROM orders WHERE username = ? ORDER BY id DESC",
            (username,),
        ).fetchall()
        conn.close()

    return render_template("status.html", orders=user_orders)


@app.route("/admin")
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
    conn.close()

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

    return render_template("admin.html", orders=orders, filters=filters, total_revenue=total_revenue)


@app.route("/manage_menu")
@app.route("/manage-menu")
def manage_menu():
    conn = get_db_connection()
    menu_items = conn.execute("SELECT * FROM menu_items ORDER BY id ASC").fetchall()
    conn.close()
    return render_template("manage_menu.html", menu_items=menu_items)


@app.route("/add-menu", methods=["POST"])
def add_menu():
    menu_id_raw = (request.form.get("menu_id") or "").strip()
    emoji = (request.form.get("emoji") or "").strip()
    name = (request.form.get("name") or "").strip()
    price = request.form.get("price")

    if not name or not price:
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
    except ValueError:
        return redirect(url_for("manage_menu") + "?error=invalid")

    conn = get_db_connection()
    try:
        created_time = now_string()
        if menu_id is None:
            conn.execute(
                "INSERT INTO menu_items (emoji, name, price, available, created_at) VALUES (?, ?, ?, ?, ?)",
                (emoji, name, price_value, 1, created_time),
            )
        else:
            conn.execute(
                "INSERT INTO menu_items (id, emoji, name, price, available, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (menu_id, emoji, name, price_value, 1, created_time),
            )
        conn.commit()
        conn.close()
        return redirect(url_for("manage_menu") + "?success=added")
    except sqlite3.IntegrityError:
        conn.close()
        return redirect(url_for("manage_menu") + "?error=duplicate")


@app.route("/edit-menu/<int:menu_id>", methods=["POST"])
def edit_menu(menu_id):
    emoji = (request.form.get("emoji") or "").strip()
    name = (request.form.get("name") or "").strip()
    price = request.form.get("price")

    if not name or not price:
        return redirect(url_for("manage_menu") + "?error=invalid")

    if not emoji:
        emoji = "ITEM"

    try:
        price_value = float(price)
    except ValueError:
        return redirect(url_for("manage_menu") + "?error=invalid")

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE menu_items SET emoji = ?, name = ?, price = ? WHERE id = ?",
            (emoji, name, price_value, menu_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("manage_menu") + "?success=updated")
    except sqlite3.IntegrityError:
        conn.close()
        return redirect(url_for("manage_menu") + "?error=duplicate")


@app.route("/toggle-menu/<int:menu_id>")
def toggle_menu(menu_id):
    conn = get_db_connection()
    item = conn.execute("SELECT available FROM menu_items WHERE id = ?", (menu_id,)).fetchone()

    if item:
        next_state = 0 if item["available"] else 1
        conn.execute("UPDATE menu_items SET available = ? WHERE id = ?", (next_state, menu_id))
        conn.commit()

    conn.close()
    return redirect(url_for("manage_menu") + "?success=updated")


@app.route("/delete-menu/<int:menu_id>")
def delete_menu(menu_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM menu_items WHERE id = ?", (menu_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("manage_menu") + "?success=deleted")


@app.route("/update_status/<int:order_id>/<status>")
def update_status(order_id, status):
    conn = get_db_connection()
    status_time = now_string()
    conn.execute(
        "UPDATE orders SET status = ?, status_time = ? WHERE id = ?",
        (status, status_time, order_id),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True)
