import os
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy

# Initialize Flask App
app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///oms.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# =================================================================
# MODELS (Kept intact)
# =================================================================
class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(100))

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50))

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    total_amount = db.Column(db.Float)
    status = db.Column(db.String(50)) # Pending, Confirmed, Preparing, Ready, Delivered, Paid, Cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
    reference = db.Column(db.String(50))
    amount = db.Column(db.Float)

class Settlement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(50))
    amount = db.Column(db.Float)
    status = db.Column(db.String(50)) # Matched, Mismatched, Duplicate, Unmatched

class FinancialCase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reason = db.Column(db.String(200))
    risk_score = db.Column(db.String(20)) # Low, Medium, High, Critical
    status = db.Column(db.String(50)) # Open, Resolved

# =================================================================
# SEEDING MECHANISM
# =================================================================
def seed_corporate_data():
    if Customer.query.count() > 0:
        return # Avoid double seeding
        
    print("Seeding realistic corporate catering data...")
    
    # 1. Customers
    clients = [
        ("Infosys", "Electronic City"), ("Wipro", "Sarjapur"), ("TCS", "Electronic City"),
        ("Accenture", "Whitefield"), ("Deloitte", "Outer Ring Road"), ("IBM", "Manyata Tech Park"),
        ("EY", "Whitefield"), ("Bosch", "Adugodi"), ("SAP Labs", "Whitefield"),
        ("Oracle", "Devanahalli"), ("Target", "Koramangala"), ("Cisco", "Marathahalli"),
        ("Capgemini", "Whitefield")
    ]
    for name, loc in clients:
        db.session.add(Customer(name=name, location=loc))
    db.session.commit()

    # 2. Menu
    menu_items = [
        ("South Indian Veg Meals", 185.50, "Lunch"), ("North Indian Thali", 210.00, "Lunch"),
        ("Executive Veg Lunch", 250.00, "Lunch"), ("Chicken Biryani Meal", 290.00, "Lunch"),
        ("Masala Dosa", 85.00, "Breakfast"), ("Continental Breakfast Box", 150.00, "Breakfast"),
        ("Samosa & Tea", 65.00, "Snacks"), ("Corporate Vegetarian Buffet", 450.00, "Events")
    ]
    for name, price, cat in menu_items:
        db.session.add(MenuItem(name=name, price=price, category=cat))
    db.session.commit()

    # 3. Orders, Payments, Settlements (The core business logic)
    # Creating a mix of successful and problematic cases
    customers = Customer.query.all()
    for i in range(20):
        cust = random.choice(customers)
        amt = random.choice([11000, 25000, 5000, 45000])
        
        # Order
        order = Order(customer_id=cust.id, total_amount=amt, status="Paid")
        db.session.add(order)
        db.session.commit()
        
        # Payment
        pay_ref = f"PAY-2026{random.randint(8,9):02d}{random.randint(1,30):02d}-{random.randint(10000,99999)}"
        payment = Payment(order_id=order.id, reference=pay_ref, amount=amt)
        db.session.add(payment)
        
        # Settlement
        settle_ref = f"SET-2026{random.randint(8,9):02d}{random.randint(1,30):02d}-{random.randint(10000,99999)}"
        
        # Create scenarios: 80% Match, 20% Issues
        if random.random() > 0.8:
            # Issue: Amount Mismatch
            settlement = Settlement(reference=settle_ref, amount=amt-500, status="Mismatched")
            db.session.add(settlement)
            db.session.add(FinancialCase(reason="Settlement amount mismatch", risk_score="High", status="Open"))
        else:
            # Success
            settlement = Settlement(reference=settle_ref, amount=amt, status="Matched")
            db.session.add(settlement)
            
    db.session.commit()
    print("Seeding complete.")

# =================================================================
# ROUTES (ALL EXISTING ROUTES PRESERVED)
# =================================================================

@app.route('/')
def index():
    return render_template('index.html', title="Corporate Catering Management System")

@app.route('/admin')
def admin_dashboard():
    return render_template('admin.html')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_corporate_data()
    app.run(debug=True)
