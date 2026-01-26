from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime

app = Flask(__name__)

# In-memory storage (temporary)
orders = []

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
        
        # Fixed: Define order_time and use consistent variable names
        order_time = datetime.now().strftime("%I:%M %p | %d %b %Y")
        
        order_data = {
            "username": username,
            "menu": menu,  # Fixed: was selected_menu
            "quantity": quantity,
            "time": order_time,  # Fixed: now properly defined
            "status": "Pending"
        }
        
        orders.append(order_data)
        index = len(orders) - 1
        return redirect(url_for("success", index=index))
    
    return render_template("order.html")

# ---------------- SUCCESS ----------------
# User sees live status
@app.route("/success", methods=["GET"])
def success():
    index = request.args.get("index")
    if index is None:
        return redirect(url_for("index"))
    
    index = int(index)
    if index < 0 or index >= len(orders):
        return redirect(url_for("index"))
    
    order = orders[index]
    return render_template("success.html", order=order)

# ---------------- STATUS ----------------
# User checks their order status
@app.route("/status", methods=["GET", "POST"])
def status():
    user_orders = []
    if request.method == "POST":
        username = request.form.get("username")
        # filter only THIS user's orders
        user_orders = [o for o in orders if o["username"] == username]
    return render_template("status.html", orders=user_orders)

# ---------------- ADMIN ----------------
# Admin dashboard
@app.route("/admin")
def admin():
    return render_template("admin.html", orders=orders)

# ---------------- UPDATE STATUS ----------------
# Admin updates order status
@app.route("/update_status/<int:index>/<status>")
def update_status(index, status):
    if index < len(orders):  # Added safety check
        orders[index]["status"] = status
        orders[index]["status_time"] = datetime.now().strftime("%I:%M %p | %d %b %Y")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True)