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
        print("ROLE:",role)
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

        order = {
            "username": username,
            "menu": menu,
            "quantity": quantity,
            "status": "Order Placed",
            "status_time": datetime.now().strftime("%I:%M %p | %d %b %Y")
        }
        orders.append(order)
        index = len(orders) - 1
        return redirect(url_for("success", index=index))

    return render_template("order.html")

# ---------------- SUCCESS ----------------
# User sees live status
@app.route("/success")
def success():
    index = int(request.args.get("index"))
    order = orders[index]

    return render_template(
        "success.html",
        order=order
    )


# ---------------- ADMIN ----------------
# Admin dashboard
@app.route("/admin")
def admin():
    return render_template("admin.html", orders=orders)


# ---------------- UPDATE STATUS ----------------
# Admin updates order status
@app.route("/update_status/<int:index>/<status>")
def update_status(index, status):
    orders[index]["status"] = status
    orders[index]["status_time"] = datetime.now().strftime("%I:%M %p | %d %b %Y")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True)
