from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
app = Flask(__name__)
orders=[]
# ---------------- INDEX ----------------
# Select menu
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        role=request.form.get("role")
        if role=="user":
            return redirect(url_for("order"))
        elif role=="admin":
            return redirect(url_for("admin"))
    return render_template("index.html")



# ---------------- ORDER ----------------
# Enter quantity
@app.route("/order", methods=["GET", "POST"])
def order():
    if request.method == "POST":
        selected_menu = request.form.get("menu")     # FROM hidden input
        quantity = request.form.get("quantity")      # FROM form
        order_time=datetime.now().strftime("%I:%M:%p | %d %b %Y")
        order_data={
            "menu":selected_menu,
            "quantity":quantity,
            "time":order_time
        }
        orders.append(order_data)
        return redirect(
            url_for(
                "success",
                menu=selected_menu,
                quantity=quantity,
                time=order_time
            )
        )
    return render_template("order.html")


# ---------------- SUCCESS ----------------
# Show confirmation
@app.route("/success")
def success():
    menu = request.args.get("menu")
    quantity = request.args.get("quantity")
    time=request.args.get("time")

    return render_template(
        "success.html",
        menu=menu,
        quantity=quantity,
        time=time
    )

@app.route('/admin')
def admin():
    return render_template("admin.html",orders=orders)

if __name__ == "__main__":
    app.run(debug=True)
