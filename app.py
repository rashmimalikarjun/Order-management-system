from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import sqlite3

app = Flask(__name__)

# Database setup
DATABASE = 'catering.db'

def get_db_connection():
    """Create a connection to the SQLite database"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    return conn

def init_db():
    """Initialize the database with orders table"""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            menu TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL,
            status_time TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

# Initialize database when app starts
init_db()

# ---------------- INDEX ----------------
# Role selection
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        role = request.form.get("role")
        print("ROLE:", role)
        if role == "user":
            return redirect(url_for("order"))
        elif role == "admin":
            return redirect(url_for("admin"))
    return render_template("index.html")

# ---------------- ORDER ----------------
# User places order
@app.route("/order", methods=["GET", "POST"])
def order():
    if request.method == "POST":
        username = request.form.get("username")
        menu = request.form.get("menu")
        quantity = request.form.get("quantity")
        
        # Create timestamp
        order_time = datetime.now().strftime("%I:%M %p | %d %b %Y")
        
        # Insert order into database
        conn = get_db_connection()
        cursor = conn.execute(
            'INSERT INTO orders (username, menu, quantity, time, status, status_time) VALUES (?, ?, ?, ?, ?, ?)',
            (username, menu, quantity, order_time, "Pending", order_time)
        )
        order_id = cursor.lastrowid  # Get the ID of the inserted order
        conn.commit()
        conn.close()
        
        return redirect(url_for("success", order_id=order_id))
    
    return render_template("order.html")

# ---------------- SUCCESS ----------------
# User sees live status
@app.route("/success", methods=["GET"])
def success():
    order_id = request.args.get("order_id")
    if order_id is None:
        return redirect(url_for("index"))
    
    # Fetch order from database
    conn = get_db_connection()
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    conn.close()
    
    if order is None:
        return redirect(url_for("index"))
    
    return render_template("success.html", order=order)

# ---------------- MY ORDERS (User Order History) ----------------
# User views all their order history
@app.route("/my-orders", methods=["GET", "POST"])
def my_orders():
    user_orders = []
    username = None
    
    if request.method == "POST":
        username = request.form.get("username")
        
        # Get all orders for this user from database
        conn = get_db_connection()
        user_orders = conn.execute(
            'SELECT * FROM orders WHERE username = ? ORDER BY id DESC',
            (username,)
        ).fetchall()
        conn.close()
    
    return render_template("my_orders.html", orders=user_orders, username=username)

# ---------------- CANCEL ORDER ----------------
# User cancels their pending order
@app.route("/cancel-order/<int:order_id>")
def cancel_order(order_id):
    # Get the order to check its status
    conn = get_db_connection()
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    
    if order and order['status'] == 'Pending':
        # Only allow cancellation if status is Pending
        conn.execute('DELETE FROM orders WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()
        return redirect(url_for('my_orders') + '?cancelled=true&username=' + order['username'])
    else:
        conn.close()
        return redirect(url_for('my_orders') + '?error=cannot_cancel&username=' + (order['username'] if order else ''))

# ---------------- STATUS (Quick Status Check) ----------------
# User checks their order status
@app.route("/status", methods=["GET", "POST"])
def status():
    user_orders = []
    if request.method == "POST":
        username = request.form.get("username")
        
        # Filter orders by username from database
        conn = get_db_connection()
        user_orders = conn.execute(
            'SELECT * FROM orders WHERE username = ? ORDER BY id DESC',
            (username,)
        ).fetchall()
        conn.close()
    
    return render_template("status.html", orders=user_orders)

# ---------------- ADMIN ----------------
# Admin dashboard
@app.route("/admin")
def admin():
    # Get all orders from database
    conn = get_db_connection()
    orders = conn.execute('SELECT * FROM orders ORDER BY id DESC').fetchall()
    conn.close()
    
    return render_template("admin.html", orders=orders)

# ---------------- UPDATE STATUS ----------------
# Admin updates order status
@app.route("/update_status/<int:order_id>/<status>")
def update_status(order_id, status):
    # Update order status in database
    conn = get_db_connection()
    status_time = datetime.now().strftime("%I:%M %p | %d %b %Y")
    conn.execute(
        'UPDATE orders SET status = ?, status_time = ? WHERE id = ?',
        (status, status_time, order_id)
    )
    conn.commit()
    conn.close()
    
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True)