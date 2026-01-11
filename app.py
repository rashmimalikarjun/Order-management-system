from flask import Flask, render_template,request,redirect,url_for

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        menu = request.form.get("menu")
        return redirect(url_for("order"))
    return render_template("index.html")

    

@app.route("/order", methods=["GET", "POST"])
def order():
    if request.method == "POST":
        quantity = request.form["quantity"]
        notes = request.form.get("notes")

        print(quantity, notes)

        return redirect(url_for("success"))

    return render_template("order.html")

@app.route("/success")
def success():
    return render_template("success.html")

if __name__ == "__main__":
    app.run(debug=True)
