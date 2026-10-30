import re
from functools import wraps
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import mysql.connector
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_connection

app = Flask(__name__)
app.secret_key = "change-this-to-any-long-random-text"

@app.context_processor
def inject_today():
    return {"today": datetime.now().strftime("%b %d, %Y")}

# Hardcoded Admin account
ADMIN_EMAIL = "admin@pawcare.ph"
ADMIN_PASSWORD_HASH = generate_password_hash("admin123")

EMAIL_PATTERN = r"[^@\s]+@[^@\s]+\.[^@\s]+"

# Clinic booking rules
SLOT_HOURS = range(9, 17)   # appointments start 9 AM ... 4 PM, on the hour
SLOT_CAPACITY = 1           # appointments the clinic can take in one slot
CLOSED_WEEKDAYS = (6,)      # 0 = Monday ... 6 = Sunday
MAX_DAYS_AHEAD = 60
CANCEL_MIN_HOURS = 2        # customers must cancel at least this many hours before
ACTIVE_STATUSES = ("Pending", "Confirmed", "In Progress")
CANCELLABLE_STATUSES = ("Pending", "Confirmed")
STATUS_FLOW = {
    "Pending":     ["Confirmed", "Cancelled"],
    "Confirmed":   ["In Progress", "Cancelled"],
    "In Progress": ["Completed"],
    "Completed":   [],
    "Cancelled":   [],
}

# A "lock" for pages: only the given role may enter
def role_required(role):
    def decorator(page):
        @wraps(page)
        def wrapper(*args, **kwargs):
            if session.get("role") != role:
                return redirect(url_for("login"))
            return page(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/")
def home():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("login.html", error="Please enter your email and password.")

        # 1. Admin (hardcoded)
        if email == ADMIN_EMAIL and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.clear()
            session["role"] = "admin"
            session["email"] = email
            return redirect(url_for("admin_dashboard"))

        # 2. Staff and customers (from the database)
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.user_id, u.password_hash, u.role,
                   COALESCE(c.full_name, s.full_name) AS full_name
            FROM users u
            LEFT JOIN customers c ON c.user_id = u.user_id
            LEFT JOIN staff s ON s.user_id = u.user_id
            WHERE u.email = %s
        """, (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["user_id"]
            session["role"] = user["role"]
            session["email"] = email
            session["full_name"] = user["full_name"] or email
            if user["role"] == "staff":
                return redirect(url_for("staff_dashboard"))
            return redirect(url_for("customer_dashboard"))

        return render_template("login.html", error="Incorrect email or password.")

    return render_template("login.html", error=None)


def current_customer_id(cursor):
    cursor.execute("SELECT customer_id FROM customers WHERE user_id = %s", (session["user_id"],))
    row = cursor.fetchone()
    return row["customer_id"] if row else None


def read_pet_form():
    pet_name = request.form.get("pet_name", "").strip()
    species = request.form.get("species", "").strip()
    gender = request.form.get("gender", "").strip()
    age_text = request.form.get("age", "").strip()

    if not pet_name or len(pet_name) > 50:
        return None, "Pet name is required (50 characters maximum)."
    if not species or len(species) > 30:
        return None, "Species is required (30 characters maximum)."
    if gender not in ("Male", "Female"):
        return None, "Please choose a gender."

    age = None
    if age_text:
        if not re.fullmatch(r"[0-9]{1,2}", age_text) or int(age_text) > 50:
            return None, "Age must be a whole number from 0 to 50."
        age = int(age_text)

    return (pet_name, species, gender, age), None


@app.route("/customer/pets")
@role_required("customer")
def customer_pets():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    customer_id = current_customer_id(cursor)
    if customer_id is None:
        cursor.close()
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT p.pet_id, p.pet_name, p.species, p.gender, p.age,
               COUNT(a.appointment_id) AS appointment_count
        FROM pets p
        LEFT JOIN appointments a ON a.pet_id = p.pet_id
        WHERE p.customer_id = %s
        GROUP BY p.pet_id, p.pet_name, p.species, p.gender, p.age
        ORDER BY p.pet_name
    """, (customer_id,))
    pets = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template("customer/pets.html", pets=pets, active_page="pets")


@app.route("/customer/pets/add", methods=["POST"])
@role_required("customer")
def add_pet():
    data, error = read_pet_form()
    if error:
        flash(error, "error")
        return redirect(url_for("customer_pets"))
    pet_name, species, gender, age = data

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))

        cursor.execute(
            "INSERT INTO pets (customer_id, pet_name, species, gender, age) "
            "VALUES (%s, %s, %s, %s, %s)",
            (customer_id, pet_name, species, gender, age)
        )
        conn.commit()
        flash(f"{pet_name} was added.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while adding your pet.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("customer_pets"))


@app.route("/customer/pets/<int:pet_id>/edit", methods=["POST"])
@role_required("customer")
def edit_pet(pet_id):
    data, error = read_pet_form()
    if error:
        flash(error, "error")
        return redirect(url_for("customer_pets"))
    pet_name, species, gender, age = data

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))

        # Make sure this pet belongs to the logged-in customer
        cursor.execute(
            "SELECT pet_id FROM pets WHERE pet_id = %s AND customer_id = %s",
            (pet_id, customer_id)
        )
        if cursor.fetchone() is None:
            flash("Pet not found.", "error")
            return redirect(url_for("customer_pets"))

        cursor.execute(
            "UPDATE pets SET pet_name = %s, species = %s, gender = %s, age = %s "
            "WHERE pet_id = %s AND customer_id = %s",
            (pet_name, species, gender, age, pet_id, customer_id)
        )
        conn.commit()
        flash(f"{pet_name} was updated.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while saving your pet.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("customer_pets"))


@app.route("/customer/pets/<int:pet_id>/delete", methods=["POST"])
@role_required("customer")
def delete_pet(pet_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))

        cursor.execute(
            "SELECT pet_id FROM pets WHERE pet_id = %s AND customer_id = %s",
            (pet_id, customer_id)
        )
        if cursor.fetchone() is None:
            flash("Pet not found.", "error")
            return redirect(url_for("customer_pets"))

        cursor.execute("SELECT COUNT(*) AS n FROM appointments WHERE pet_id = %s", (pet_id,))
        used = cursor.fetchone()["n"]
        if used:
            flash(f"This pet has {used} appointment(s) on record, so it cannot be deleted.", "error")
        else:
            cursor.execute(
                "DELETE FROM pets WHERE pet_id = %s AND customer_id = %s",
                (pet_id, customer_id)
            )
            conn.commit()
            flash("Pet deleted.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Could not delete this pet.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("customer_pets"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return redirect(url_for("login"))

    full_name = request.form.get("full_name", "").strip()
    contact_number = request.form.get("contact_number", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    def show_error(message):
        return render_template("login.html", error=message, active_tab="register", form=request.form)

    # Validation
    if not full_name or not contact_number or not email or not password:
        return show_error("Please fill in all fields.")
    if len(full_name) > 100:
        return show_error("Full name is too long (100 characters maximum).")
    if len(email) > 100 or not re.fullmatch(EMAIL_PATTERN, email):
        return show_error("Enter a valid email address.")
    if email == ADMIN_EMAIL:
        return show_error("That email is not available.")
    if not re.fullmatch(r"\+?[0-9]{7,15}", contact_number):
        return show_error("Enter a valid contact number (digits only, 7-15 digits).")
    if len(password) < 6:
        return show_error("Password must be at least 6 characters.")
    if password != confirm_password:
        return show_error("Passwords do not match.")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            return show_error("That email is already registered.")

        cursor.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'customer')",
            (email, generate_password_hash(password))
        )
        user_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO customers (user_id, full_name, contact_number) VALUES (%s, %s, %s)",
            (user_id, full_name, contact_number)
        )
        conn.commit()
    except mysql.connector.Error:
        conn.rollback()
        return show_error("Something went wrong while creating your account. Please try again.")
    finally:
        cursor.close()
        conn.close()

    return render_template("login.html", success="Account created. You can now sign in.", active_tab="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    queries = {
        "customers": "SELECT COUNT(*) AS n FROM customers",
        "staff": "SELECT COUNT(*) AS n FROM staff",
        "pets": "SELECT COUNT(*) AS n FROM pets",
        "pending": "SELECT COUNT(*) AS n FROM appointments WHERE status = 'Pending'",
        "revenue": """
            SELECT COALESCE(SUM(amount), 0) AS n
            FROM payments
            WHERE payment_status = 'Paid'
              AND YEAR(payment_date) = YEAR(CURDATE())
              AND MONTH(payment_date) = MONTH(CURDATE())
        """,
    }

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    stats = {}
    for key, sql in queries.items():
        cursor.execute(sql)
        stats[key] = cursor.fetchone()["n"]

    cursor.execute("""
        SELECT p.pet_name,
               c.full_name AS owner,
               s.service_name,
               DATE_FORMAT(a.appointment_date, '%b %d, %Y') AS date,
               TIME_FORMAT(a.appointment_time, '%h:%i %p') AS time,
               a.status
        FROM appointments a
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN customers c ON a.customer_id = c.customer_id
        JOIN services s ON a.service_id = s.service_id
        WHERE a.status IN ('Pending', 'Confirmed', 'In Progress')
        ORDER BY a.appointment_date, a.appointment_time
        LIMIT 5
    """)
    upcoming = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("admin/dashboard.html", stats=stats, upcoming=upcoming, active_page="dashboard")


@app.route("/customer")
@role_required("customer")
def customer_dashboard():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT customer_id, full_name FROM customers WHERE user_id = %s",
        (session["user_id"],)
    )
    customer = cursor.fetchone()
    if customer is None:
        cursor.close()
        conn.close()
        session.clear()
        return redirect(url_for("login"))
    customer_id = customer["customer_id"]

    cursor.execute("SELECT COUNT(*) AS n FROM pets WHERE customer_id = %s", (customer_id,))
    pet_count = cursor.fetchone()["n"]

    cursor.execute("SELECT COUNT(*) AS n FROM appointments WHERE customer_id = %s", (customer_id,))
    appointment_count = cursor.fetchone()["n"]

    cursor.execute("""
        SELECT s.service_name, p.pet_name,
               DATE_FORMAT(a.appointment_date, %s) AS date,
               TIME_FORMAT(a.appointment_time, %s) AS time,
               a.status
        FROM appointments a
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        WHERE a.customer_id = %s
          AND a.status IN ('Pending', 'Confirmed')
          AND a.appointment_date >= CURDATE()
        ORDER BY a.appointment_date, a.appointment_time
        LIMIT 1
    """, ("%Y-%m-%d", "%h:%i %p", customer_id))
    next_appointment = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        "customer/home.html",
        first_name=customer["full_name"].split()[0],
        pet_count=pet_count,
        appointment_count=appointment_count,
        next_appointment=next_appointment,
        active_page="home",
    )


def read_service_form():
    name = request.form.get("service_name", "").strip()
    description = request.form.get("description", "").strip()
    category_id = request.form.get("category_id", "").strip()
    price_text = request.form.get("price", "").strip()

    if not name or len(name) > 100:
        return None, "Service name is required (100 characters maximum)."
    if len(description) > 500:
        return None, "Description is too long (500 characters maximum)."
    if not category_id.isdigit():
        return None, "Please choose a category."
    try:
        price = Decimal(price_text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None, "Enter a valid price, for example 350 or 350.50."
    if price <= 0 or price > Decimal("99999999.99"):
        return None, "Price must be greater than 0."

    return (name, description or None, int(category_id), price), None


@app.route("/admin/categories")
@role_required("admin")
def admin_categories():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT c.category_id, c.category_name, COUNT(s.service_id) AS service_count
        FROM categories c
        LEFT JOIN services s ON s.category_id = c.category_id
        GROUP BY c.category_id, c.category_name
        ORDER BY c.category_name
    """)
    categories = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin/categories.html", categories=categories, active_page="categories")


@app.route("/admin/services")
@role_required("admin")
def admin_services():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT s.service_id, s.service_name, s.description, s.price, s.is_available,
               s.category_id, c.category_name,
               COUNT(a.appointment_id) AS appointment_count
        FROM services s
        JOIN categories c ON s.category_id = c.category_id
        LEFT JOIN appointments a ON a.service_id = s.service_id
        GROUP BY s.service_id, s.service_name, s.description, s.price,
                 s.is_available, s.category_id, c.category_name
        ORDER BY c.category_name, s.service_name
    """)
    services = cursor.fetchall()

    cursor.execute("SELECT category_id, category_name FROM categories ORDER BY category_name")
    categories = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template("admin/services.html", services=services,
                           categories=categories, active_page="services")


@app.route("/admin/services/add", methods=["POST"])
@role_required("admin")
def add_service():
    data, error = read_service_form()
    if error:
        flash(error, "error")
        return redirect(url_for("admin_services"))
    name, description, category_id, price = data

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO services (category_id, service_name, description, price) "
            "VALUES (%s, %s, %s, %s)",
            (category_id, name, description, price)
        )
        conn.commit()
        flash(f'Service "{name}" added.', "success")
    except mysql.connector.IntegrityError:
        conn.rollback()
        flash("That category does not exist.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/edit", methods=["POST"])
@role_required("admin")
def edit_service(service_id):
    data, error = read_service_form()
    if error:
        flash(error, "error")
        return redirect(url_for("admin_services"))
    name, description, category_id, price = data

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE services SET category_id = %s, service_name = %s, "
            "description = %s, price = %s WHERE service_id = %s",
            (category_id, name, description, price, service_id)
        )
        conn.commit()
        flash(f'Service "{name}" updated.', "success")
    except mysql.connector.IntegrityError:
        conn.rollback()
        flash("That category does not exist.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_service(service_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE services SET is_available = 1 - is_available WHERE service_id = %s",
            (service_id,)
        )
        conn.commit()
    except mysql.connector.Error:
        conn.rollback()
        flash("Could not change availability.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@role_required("admin")
def delete_service(service_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM appointments WHERE service_id = %s", (service_id,))
        used = cursor.fetchone()[0]
        if used:
            flash(f"This service has {used} appointment(s) on record, so it cannot be deleted. "
                  "Turn it off with the Available switch instead.", "error")
        else:
            cursor.execute("DELETE FROM services WHERE service_id = %s", (service_id,))
            conn.commit()
            flash("Service deleted.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Could not delete this service.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_services"))


@app.route("/admin/categories/add", methods=["POST"])
@role_required("admin")
def add_category():
    name = request.form.get("category_name", "").strip()
    if not name or len(name) > 50:
        flash("Category name is required (50 characters maximum).", "error")
        return redirect(url_for("admin_categories"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO categories (category_name) VALUES (%s)", (name,))
        conn.commit()
        flash(f'Category "{name}" added.', "success")
    except mysql.connector.IntegrityError:
        conn.rollback()
        flash("That category already exists.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/<int:category_id>/edit", methods=["POST"])
@role_required("admin")
def edit_category(category_id):
    name = request.form.get("category_name", "").strip()
    if not name or len(name) > 50:
        flash("Category name is required (50 characters maximum).", "error")
        return redirect(url_for("admin_categories"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE categories SET category_name = %s WHERE category_id = %s",
            (name, category_id)
        )
        conn.commit()
        flash("Category updated.", "success")
    except mysql.connector.IntegrityError:
        conn.rollback()
        flash("Another category already has that name.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/<int:category_id>/delete", methods=["POST"])
@role_required("admin")
def delete_category(category_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM services WHERE category_id = %s", (category_id,))
        in_use = cursor.fetchone()[0]
        if in_use:
            flash(f"This category is used by {in_use} service(s). Move or delete those services first.", "error")
        else:
            cursor.execute("DELETE FROM categories WHERE category_id = %s", (category_id,))
            conn.commit()
            flash("Category deleted.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Could not delete this category.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_categories"))


def read_staff_form(require_password):
    full_name = request.form.get("full_name", "").strip()
    contact_number = request.form.get("contact_number", "").strip()
    email = request.form.get("email", "").strip().lower()
    staff_role = request.form.get("staff_role", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not full_name or len(full_name) > 100:
        return None, "Full name is required (100 characters maximum)."
    if not re.fullmatch(r"\+?[0-9]{7,15}", contact_number):
        return None, "Enter a valid contact number (digits only, 7-15 digits)."
    if len(email) > 100 or not re.fullmatch(EMAIL_PATTERN, email):
        return None, "Enter a valid email address."
    if email == ADMIN_EMAIL:
        return None, "That email is not available."
    if staff_role not in ("Manager", "Groomer"):
        return None, "Please choose a role."
    if require_password or password or confirm_password:
        if len(password) < 6:
            return None, "Password must be at least 6 characters."
        if password != confirm_password:
            return None, "Passwords do not match."

    return (full_name, contact_number, email, staff_role, password), None


@app.route("/admin/staff")
@role_required("admin")
def admin_staff():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT s.staff_id, s.full_name, s.contact_number, s.staff_role, u.email,
               COUNT(p.payment_id) AS payment_count
        FROM staff s
        JOIN users u ON s.user_id = u.user_id
        LEFT JOIN payments p ON p.processed_by = s.staff_id
        GROUP BY s.staff_id, s.full_name, s.contact_number, s.staff_role, u.email
        ORDER BY s.full_name
    """)
    staff = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin/staff.html", staff=staff, active_page="staff")


@app.route("/admin/staff/add", methods=["POST"])
@role_required("admin")
def add_staff():
    data, error = read_staff_form(require_password=True)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_staff"))
    full_name, contact_number, email, staff_role, password = data

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            flash("That email is already in use.", "error")
            return redirect(url_for("admin_staff"))

        cursor.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'staff')",
            (email, generate_password_hash(password))
        )
        user_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO staff (user_id, full_name, contact_number, staff_role) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, full_name, contact_number, staff_role)
        )
        conn.commit()
        flash(f"{full_name} was added as {staff_role}.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while adding the staff member.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_staff"))


@app.route("/admin/staff/<int:staff_id>/edit", methods=["POST"])
@role_required("admin")
def edit_staff(staff_id):
    data, error = read_staff_form(require_password=False)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_staff"))
    full_name, contact_number, email, staff_role, password = data

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT user_id FROM staff WHERE staff_id = %s", (staff_id,))
        row = cursor.fetchone()
        if row is None:
            flash("Staff member not found.", "error")
            return redirect(url_for("admin_staff"))
        user_id = row["user_id"]

        cursor.execute(
            "SELECT user_id FROM users WHERE email = %s AND user_id <> %s",
            (email, user_id)
        )
        if cursor.fetchone():
            flash("That email is already in use.", "error")
            return redirect(url_for("admin_staff"))

        if password:
            cursor.execute(
                "UPDATE users SET email = %s, password_hash = %s WHERE user_id = %s",
                (email, generate_password_hash(password), user_id)
            )
        else:
            cursor.execute("UPDATE users SET email = %s WHERE user_id = %s", (email, user_id))

        cursor.execute(
            "UPDATE staff SET full_name = %s, contact_number = %s, staff_role = %s "
            "WHERE staff_id = %s",
            (full_name, contact_number, staff_role, staff_id)
        )
        conn.commit()
        flash(f"{full_name} was updated.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while saving the changes.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_staff"))


@app.route("/admin/staff/<int:staff_id>/delete", methods=["POST"])
@role_required("admin")
def delete_staff(staff_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT user_id FROM staff WHERE staff_id = %s", (staff_id,))
        row = cursor.fetchone()
        if row is None:
            flash("Staff member not found.", "error")
            return redirect(url_for("admin_staff"))

        cursor.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE processed_by = %s", (staff_id,)
        )
        processed = cursor.fetchone()["n"]
        if processed:
            flash(f"This staff member processed {processed} payment(s), so they cannot be "
                  "deleted. Their payment history needs their name.", "error")
        else:
            # Deleting the login also removes the staff row (ON DELETE CASCADE)
            cursor.execute("DELETE FROM users WHERE user_id = %s", (row["user_id"],))
            conn.commit()
            flash("Staff member deleted.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Could not delete this staff member.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_staff"))


@app.route("/customer/services")
@role_required("customer")
def customer_services():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT c.category_name, s.service_id, s.service_name, s.description, s.price
        FROM services s
        JOIN categories c ON s.category_id = c.category_id
        WHERE s.is_available = 1
        ORDER BY c.category_name, s.service_name
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    # Group the services under their category name
    groups = {}
    for row in rows:
        groups.setdefault(row["category_name"], []).append(row)

    return render_template("customer/services.html", groups=groups, active_page="services")

def parse_booking_date(text):
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None, "Please choose a valid date."

    today = datetime.now().date()
    if day < today:
        return None, "That date has already passed."
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        return None, f"Bookings open up to {MAX_DAYS_AHEAD} days ahead."
    if day.weekday() in CLOSED_WEEKDAYS:
        return None, "We are closed on Sundays."
    return day, None


def get_slots(cursor, day):
    # How many appointments are already booked at each start time that day
    cursor.execute("""
        SELECT TIME_FORMAT(appointment_time, %s) AS slot, COUNT(*) AS n
        FROM appointments
        WHERE appointment_date = %s AND status <> 'Cancelled'
        GROUP BY slot
    """, ("%H:%i", day))
    booked = {row["slot"]: row["n"] for row in cursor.fetchall()}

    now = datetime.now()
    slots = []
    for hour in SLOT_HOURS:
        value = f"{hour:02d}:00"
        label = datetime(2000, 1, 1, hour).strftime("%I:%M %p")
        already_past = datetime(day.year, day.month, day.day, hour) <= now
        available = booked.get(value, 0) < SLOT_CAPACITY and not already_past
        slots.append({"value": value, "label": label, "available": available})
    return slots


def validate_booking(cursor, customer_id):
    pet_id = request.form.get("pet_id", "")
    service_id = request.form.get("service_id", "")
    date_text = request.form.get("date", "")
    slot = request.form.get("slot", "")

    if not re.fullmatch(r"[0-9]+", pet_id) or not re.fullmatch(r"[0-9]+", service_id):
        return None, "Please choose a pet and a service."

    # The pet must belong to this customer
    cursor.execute(
        "SELECT pet_id, pet_name FROM pets WHERE pet_id = %s AND customer_id = %s",
        (int(pet_id), customer_id)
    )
    pet = cursor.fetchone()
    if pet is None:
        return None, "Please choose one of your pets."

    # The service must exist and be turned on
    cursor.execute(
        "SELECT service_id, service_name, price FROM services "
        "WHERE service_id = %s AND is_available = 1",
        (int(service_id),)
    )
    service = cursor.fetchone()
    if service is None:
        return None, "That service is not available."

    day, error = parse_booking_date(date_text)
    if error:
        return None, error

    chosen = {s["value"]: s for s in get_slots(cursor, day)}.get(slot)
    if chosen is None:
        return None, "Please choose a time slot."
    if not chosen["available"]:
        return None, "Sorry, that time slot is no longer available. Please pick another."

    # The customer cannot book two appointments at the same time
    cursor.execute(
        "SELECT appointment_id FROM appointments "
        "WHERE customer_id = %s AND appointment_date = %s AND appointment_time = %s "
        "AND status <> 'Cancelled'",
        (customer_id, day, slot + ":00")
    )
    if cursor.fetchone():
        return None, "You already have an appointment at that time."

    booking = {"pet": pet, "service": service, "day": day,
               "slot": slot, "slot_label": chosen["label"]}
    return booking, None


@app.route("/customer/book")
@role_required("customer")
def book_appointment():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    customer_id = current_customer_id(cursor)
    if customer_id is None:
        cursor.close()
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    cursor.execute(
        "SELECT pet_id, pet_name, species FROM pets WHERE customer_id = %s ORDER BY pet_name",
        (customer_id,)
    )
    pets = cursor.fetchall()

    cursor.execute("""
        SELECT c.category_name, s.service_id, s.service_name, s.price
        FROM services s
        JOIN categories c ON s.category_id = c.category_id
        WHERE s.is_available = 1
        ORDER BY c.category_name, s.service_name
    """)
    groups = {}
    for row in cursor.fetchall():
        groups.setdefault(row["category_name"], []).append(row)

    cursor.close()
    conn.close()

    today = datetime.now().date()
    return render_template(
        "customer/book.html",
        pets=pets,
        groups=groups,
        selected_service=request.args.get("service", ""),
        min_date=today.isoformat(),
        max_date=(today + timedelta(days=MAX_DAYS_AHEAD)).isoformat(),
        active_page="book",
    )


@app.route("/customer/book/slots")
@role_required("customer")
def booking_slots():
    day, error = parse_booking_date(request.args.get("date", ""))
    if error:
        return jsonify({"error": error})

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    slots = get_slots(cursor, day)
    cursor.close()
    conn.close()
    return jsonify({"slots": slots})


@app.route("/customer/book/review", methods=["POST"])
@role_required("customer")
def review_booking():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))
        booking, error = validate_booking(cursor, customer_id)
    finally:
        cursor.close()
        conn.close()

    if error:
        flash(error, "error")
        return redirect(url_for("book_appointment"))
    return render_template("customer/book_review.html", booking=booking, active_page="book")


@app.route("/customer/book/confirm", methods=["POST"])
@role_required("customer")
def confirm_booking():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))

        # Check everything again. Never trust the review page's hidden fields.
        booking, error = validate_booking(cursor, customer_id)
        if error:
            flash(error, "error")
            return redirect(url_for("book_appointment"))

        cursor.execute(
            "INSERT INTO appointments "
            "(customer_id, pet_id, service_id, appointment_date, appointment_time, status) "
            "VALUES (%s, %s, %s, %s, %s, 'Pending')",
            (customer_id, booking["pet"]["pet_id"], booking["service"]["service_id"],
             booking["day"], booking["slot"] + ":00")
        )
        appointment_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO payments (appointment_id, amount, payment_status) "
            "VALUES (%s, %s, 'Pending')",
            (appointment_id, booking["service"]["price"])
        )
        conn.commit()
        flash(f"Booked! {booking['pet']['pet_name']} is scheduled for "
              f"{booking['day'].strftime('%B %d, %Y')} at {booking['slot_label']}. "
              "We will confirm it shortly.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while booking. Please try again.", "error")
        return redirect(url_for("book_appointment"))
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("customer_dashboard"))


def can_cancel(row, now):
    if row["status"] not in CANCELLABLE_STATUSES:
        return False
    if row["payment_status"] == "Paid":
        return False
    return row["starts_at"] - now >= timedelta(hours=CANCEL_MIN_HOURS)


@app.route("/customer/appointments")
@role_required("customer")
def my_appointments():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    customer_id = current_customer_id(cursor)
    if customer_id is None:
        cursor.close()
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT a.appointment_id, a.status, p.pet_name, s.service_name,
               a.appointment_date AS appt_date,
               TIMESTAMP(a.appointment_date, a.appointment_time) AS starts_at,
               DATE_FORMAT(a.appointment_date, %s) AS date_text,
               TIME_FORMAT(a.appointment_time, %s) AS time_text,
               pay.amount, pay.payment_status, pay.payment_method,
               DATE_FORMAT(pay.payment_date, %s) AS paid_on
        FROM appointments a
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        LEFT JOIN payments pay ON pay.appointment_id = a.appointment_id
        WHERE a.customer_id = %s
        ORDER BY a.appointment_date, a.appointment_time
    """, ("%b %d, %Y", "%h:%i %p", "%b %d, %Y", customer_id))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    now = datetime.now()
    today = now.date()
    upcoming = []
    previous = []
    for row in rows:
        row["can_cancel"] = can_cancel(row, now)
        if row["status"] in ACTIVE_STATUSES and row["appt_date"] >= today:
            upcoming.append(row)
        else:
            previous.append(row)
    previous.reverse()   # newest first

    return render_template("customer/appointments.html", upcoming=upcoming,
                           previous=previous, active_page="appointments")


@app.route("/customer/appointments/<int:appointment_id>/cancel", methods=["POST"])
@role_required("customer")
def cancel_appointment(appointment_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        customer_id = current_customer_id(cursor)
        if customer_id is None:
            session.clear()
            return redirect(url_for("login"))

        # Only look at this customer's own appointment
        cursor.execute("""
            SELECT a.status,
                   TIMESTAMP(a.appointment_date, a.appointment_time) AS starts_at,
                   pay.payment_status
            FROM appointments a
            LEFT JOIN payments pay ON pay.appointment_id = a.appointment_id
            WHERE a.appointment_id = %s AND a.customer_id = %s
        """, (appointment_id, customer_id))
        row = cursor.fetchone()

        if row is None:
            flash("Appointment not found.", "error")
        elif not can_cancel(row, datetime.now()):
            flash("This appointment can no longer be cancelled online. Please contact the clinic.", "error")
        else:
            cursor.execute(
                "UPDATE appointments SET status = 'Cancelled' "
                "WHERE appointment_id = %s AND customer_id = %s",
                (appointment_id, customer_id)
            )
            cursor.execute(
                "UPDATE payments SET payment_status = 'Cancelled' "
                "WHERE appointment_id = %s AND payment_status = 'Pending'",
                (appointment_id,)
            )
            conn.commit()
            flash("Your appointment was cancelled.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong while cancelling. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("my_appointments"))


def load_appointments():
    """All appointments with their payment info and allowed next statuses."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT a.appointment_id, a.status,
               c.full_name AS customer_name, c.contact_number,
               p.pet_name, s.service_name,
               DATE_FORMAT(a.appointment_date, %s) AS date_text,
               TIME_FORMAT(a.appointment_time, %s) AS time_text,
               pay.amount, pay.payment_status
        FROM appointments a
        JOIN customers c ON a.customer_id = c.customer_id
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        LEFT JOIN payments pay ON pay.appointment_id = a.appointment_id
        ORDER BY a.appointment_date DESC, a.appointment_time DESC
    """, ("%b %d, %Y", "%h:%i %p"))
    appointments = cursor.fetchall()
    cursor.close()
    conn.close()

    for a in appointments:
        a["next_statuses"] = STATUS_FLOW.get(a["status"], [])
    return appointments


def change_appointment_status(appointment_id, new_status):
    """Apply a status change if the flow allows it, and flash the result."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT status FROM appointments WHERE appointment_id = %s",
                       (appointment_id,))
        row = cursor.fetchone()

        if row is None:
            flash("Appointment not found.", "error")
        elif new_status not in STATUS_FLOW.get(row["status"], []):
            flash("That status change is not allowed.", "error")
        else:
            cursor.execute("UPDATE appointments SET status = %s WHERE appointment_id = %s",
                           (new_status, appointment_id))
            if new_status == "Cancelled":
                cursor.execute(
                    "UPDATE payments SET payment_status = 'Cancelled' "
                    "WHERE appointment_id = %s AND payment_status = 'Pending'",
                    (appointment_id,))
            conn.commit()
            flash("Appointment marked as " + new_status + ".", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()


@app.route("/admin/appointments")
@role_required("admin")
def admin_appointments():
    return render_template("admin/appointments.html",
                           appointments=load_appointments(),
                           layout="admin/base.html",
                           status_endpoint="update_appointment_status",
                           active_page="appointments")


@app.route("/admin/appointments/<int:appointment_id>/status", methods=["POST"])
@role_required("admin")
def update_appointment_status(appointment_id):
    change_appointment_status(appointment_id, request.form.get("status", ""))
    return redirect(url_for("admin_appointments"))


@app.route("/staff/appointments")
@role_required("staff")
def staff_appointments():
    return render_template("admin/appointments.html",
                           appointments=load_appointments(),
                           layout="staff/base.html",
                           status_endpoint="staff_update_appointment_status",
                           active_page="appointments")


@app.route("/staff/appointments/<int:appointment_id>/status", methods=["POST"])
@role_required("staff")
def staff_update_appointment_status(appointment_id):
    change_appointment_status(appointment_id, request.form.get("status", ""))
    return redirect(url_for("staff_appointments"))


@app.route("/admin/customers")
@role_required("admin")
def admin_customers():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT c.customer_id, c.full_name, c.contact_number, u.email,
               DATE_FORMAT(u.created_at, %s) AS joined,
               (SELECT COUNT(*) FROM pets p
                 WHERE p.customer_id = c.customer_id) AS pet_count,
               (SELECT COUNT(*) FROM appointments a
                 WHERE a.customer_id = c.customer_id) AS appointment_count
        FROM customers c
        JOIN users u ON c.user_id = u.user_id
        ORDER BY c.full_name
    """, ("%b %d, %Y",))
    customers = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("admin/customers.html", customers=customers,
                           active_page="customers")


def load_pets():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT p.pet_id, p.pet_name, p.species, p.gender, p.age,
               c.full_name AS owner_name, c.contact_number
        FROM pets p
        JOIN customers c ON p.customer_id = c.customer_id
        ORDER BY p.pet_name
    """)
    pets = cursor.fetchall()
    cursor.close()
    conn.close()
    return pets


def update_pet_for(pet_id):
    data, error = read_pet_form()
    if error:
        flash(error, "error")
        return
    pet_name, species, gender, age = data

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT pet_id FROM pets WHERE pet_id = %s", (pet_id,))
        if cursor.fetchone() is None:
            flash("Pet not found.", "error")
        else:
            cursor.execute(
                "UPDATE pets SET pet_name = %s, species = %s, gender = %s, age = %s "
                "WHERE pet_id = %s",
                (pet_name, species, gender, age, pet_id)
            )
            conn.commit()
            flash("Pet details updated.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()


@app.route("/admin/pets")
@role_required("admin")
def admin_pets():
    return render_template("admin/pets.html", pets=load_pets(),
                           layout="admin/base.html", edit_endpoint="admin_edit_pet",
                           active_page="pets")


@app.route("/admin/pets/<int:pet_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_pet(pet_id):
    update_pet_for(pet_id)
    return redirect(url_for("admin_pets"))


@app.route("/staff/pets")
@role_required("staff")
def staff_pets():
    return render_template("admin/pets.html", pets=load_pets(),
                           layout="staff/base.html", edit_endpoint="staff_edit_pet",
                           active_page="pets")


@app.route("/staff/pets/<int:pet_id>/edit", methods=["POST"])
@role_required("staff")
def staff_edit_pet(pet_id):
    update_pet_for(pet_id)
    return redirect(url_for("staff_pets"))


PAYMENT_METHODS = ("Cash", "GCash", "Card")


def load_payments():
    """All payments with their appointment info, and whether one can be recorded."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT pay.payment_id, pay.amount, pay.payment_method, pay.payment_status,
               DATE_FORMAT(pay.payment_date, %s) AS paid_on,
               a.status AS appointment_status,
               DATE_FORMAT(a.appointment_date, %s) AS date_text,
               c.full_name AS customer_name,
               p.pet_name, s.service_name,
               st.full_name AS processed_by_name
        FROM payments pay
        JOIN appointments a ON pay.appointment_id = a.appointment_id
        JOIN customers c ON a.customer_id = c.customer_id
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        LEFT JOIN staff st ON pay.processed_by = st.staff_id
        ORDER BY FIELD(pay.payment_status, 'Pending', 'Paid', 'Cancelled'),
                 a.appointment_date DESC
    """, ("%b %d, %Y %h:%i %p", "%b %d, %Y"))
    payments = cursor.fetchall()
    cursor.close()
    conn.close()

    for pay in payments:
        pay["can_record"] = (pay["payment_status"] == "Pending"
                             and pay["appointment_status"] == "Completed")
    return payments


def record_payment_for(payment_id, method):
    """Mark a payment as Paid. Staff payments remember who processed them."""
    if method not in PAYMENT_METHODS:
        flash("Please choose a payment method.", "error")
        return

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT pay.payment_status, a.status AS appointment_status
            FROM payments pay
            JOIN appointments a ON pay.appointment_id = a.appointment_id
            WHERE pay.payment_id = %s
        """, (payment_id,))
        row = cursor.fetchone()

        if row is None:
            flash("Payment not found.", "error")
        elif row["payment_status"] != "Pending" or row["appointment_status"] != "Completed":
            flash("This payment can't be recorded right now.", "error")
        else:
            processed_by = None          # the Admin login is not in the staff table
            if session.get("role") == "staff":
                cursor.execute("SELECT staff_id FROM staff WHERE user_id = %s",
                               (session["user_id"],))
                staff_row = cursor.fetchone()
                if staff_row:
                    processed_by = staff_row["staff_id"]

            cursor.execute("""
                UPDATE payments
                SET payment_status = 'Paid', payment_method = %s,
                    payment_date = NOW(), processed_by = %s
                WHERE payment_id = %s AND payment_status = 'Pending'
            """, (method, processed_by, payment_id))
            conn.commit()
            flash("Payment recorded as Paid (" + method + ").", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()


@app.route("/admin/payments")
@role_required("admin")
def admin_payments():
    return render_template("admin/payments.html", payments=load_payments(),
                           methods=PAYMENT_METHODS, layout="admin/base.html",
                           record_endpoint="record_payment", active_page="payments")


@app.route("/admin/payments/<int:payment_id>/record", methods=["POST"])
@role_required("admin")
def record_payment(payment_id):
    record_payment_for(payment_id, request.form.get("payment_method", ""))
    return redirect(url_for("admin_payments"))


@app.route("/staff/payments")
@role_required("staff")
def staff_payments():
    return render_template("admin/payments.html", payments=load_payments(),
                           methods=PAYMENT_METHODS, layout="staff/base.html",
                           record_endpoint="staff_record_payment", active_page="payments")


@app.route("/staff/payments/<int:payment_id>/record", methods=["POST"])
@role_required("staff")
def staff_record_payment(payment_id):
    record_payment_for(payment_id, request.form.get("payment_method", ""))
    return redirect(url_for("staff_payments"))


@app.route("/customer/payments")
@role_required("customer")
def my_payments():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    customer_id = current_customer_id(cursor)
    if customer_id is None:
        cursor.close()
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT pay.amount, pay.payment_method, pay.payment_status,
               DATE_FORMAT(pay.payment_date, %s) AS paid_on,
               DATE_FORMAT(a.appointment_date, %s) AS date_text,
               p.pet_name, s.service_name
        FROM payments pay
        JOIN appointments a ON pay.appointment_id = a.appointment_id
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        WHERE a.customer_id = %s
        ORDER BY a.appointment_date DESC, a.appointment_time DESC
    """, ("%b %d, %Y %h:%i %p", "%b %d, %Y", customer_id))
    payments = cursor.fetchall()
    cursor.close()
    conn.close()

    total_paid = sum(pay["amount"] for pay in payments if pay["payment_status"] == "Paid")
    still_to_pay = sum(pay["amount"] for pay in payments if pay["payment_status"] == "Pending")

    return render_template("customer/payments.html", payments=payments,
                           total_paid=total_paid, still_to_pay=still_to_pay,
                           active_page="payments")


@app.route("/staff")
@role_required("staff")
def staff_dashboard():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            COALESCE(SUM(appointment_date = CURDATE() AND status <> 'Cancelled'), 0) AS today_count,
            COALESCE(SUM(status = 'Pending'), 0) AS pending_count,
            COALESCE(SUM(status = 'In Progress'), 0) AS in_progress_count
        FROM appointments
    """)
    stats = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(*) AS unpaid_count
        FROM payments pay
        JOIN appointments a ON pay.appointment_id = a.appointment_id
        WHERE pay.payment_status = 'Pending' AND a.status = 'Completed'
    """)
    stats["unpaid_count"] = cursor.fetchone()["unpaid_count"]

    cursor.execute("""
        SELECT TIME_FORMAT(a.appointment_time, %s) AS time_text, a.status,
               c.full_name AS customer_name, p.pet_name, s.service_name
        FROM appointments a
        JOIN customers c ON a.customer_id = c.customer_id
        JOIN pets p ON a.pet_id = p.pet_id
        JOIN services s ON a.service_id = s.service_id
        WHERE a.appointment_date = CURDATE() AND a.status <> 'Cancelled'
        ORDER BY a.appointment_time
    """, ("%h:%i %p",))
    schedule = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template("staff/dashboard.html", stats=stats,
                           schedule=schedule, active_page="dashboard")


@app.route("/staff/services")
@role_required("staff")
def staff_services():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT s.service_id, s.service_name, s.price, s.is_available,
               c.category_name
        FROM services s
        JOIN categories c ON s.category_id = c.category_id
        ORDER BY c.category_name, s.service_name
    """)
    services = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template("staff/services.html", services=services,
                           active_page="services")


@app.route("/staff/services/<int:service_id>/toggle", methods=["POST"])
@role_required("staff")
def staff_toggle_service(service_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE services SET is_available = NOT is_available "
                       "WHERE service_id = %s", (service_id,))
        conn.commit()
        if cursor.rowcount:
            flash("Service availability updated.", "success")
        else:
            flash("Service not found.", "error")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("staff_services"))


@app.route("/admin/reports")
@role_required("admin")
def admin_reports():
    today = datetime.now().date()
    start_default = today.replace(day=1)

    try:
        start = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
        end = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        start, end = start_default, today

    if start > end:
        flash("The start date must be on or before the end date.", "error")
        start, end = start_default, today

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    params = (start.isoformat(), end.isoformat())

    cursor.execute("""
        SELECT COUNT(*) AS paid_count, COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE payment_status = 'Paid' AND DATE(payment_date) BETWEEN %s AND %s
    """, params)
    totals = cursor.fetchone()

    cursor.execute("""
        SELECT s.service_name, COUNT(*) AS times_paid, SUM(pay.amount) AS revenue
        FROM payments pay
        JOIN appointments a ON pay.appointment_id = a.appointment_id
        JOIN services s ON a.service_id = s.service_id
        WHERE pay.payment_status = 'Paid' AND DATE(pay.payment_date) BETWEEN %s AND %s
        GROUP BY s.service_id, s.service_name
        ORDER BY revenue DESC
    """, params)
    by_service = cursor.fetchall()

    cursor.execute("""
        SELECT payment_method, COUNT(*) AS payments, SUM(amount) AS revenue
        FROM payments
        WHERE payment_status = 'Paid' AND DATE(payment_date) BETWEEN %s AND %s
        GROUP BY payment_method
        ORDER BY revenue DESC
    """, params)
    by_method = cursor.fetchall()

    cursor.execute("SELECT status, COUNT(*) AS n FROM appointments GROUP BY status")
    status_counts = {name: 0 for name in STATUS_FLOW}
    for row in cursor.fetchall():
        status_counts[row["status"]] = row["n"]

    cursor.execute("""
        SELECT COALESCE(SUM(is_available = 1), 0) AS available,
               COALESCE(SUM(is_available = 0), 0) AS unavailable
        FROM services
    """)
    availability = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template("admin/reports.html", start=start.isoformat(),
                           end=end.isoformat(), totals=totals,
                           by_service=by_service, by_method=by_method,
                           status_counts=status_counts, availability=availability,
                           active_page="reports")


def read_customer_form():
    full_name = request.form.get("full_name", "").strip()
    contact_number = request.form.get("contact_number", "").strip()

    if not full_name or len(full_name) > 100:
        return None, "Full name is required (100 characters maximum)."
    if not re.fullmatch(r"[0-9+()\- ]{7,20}", contact_number):
        return None, "Contact number must be 7 to 20 characters (digits, spaces, + ( ) or -)."
    return (full_name, contact_number), None


@app.route("/admin/customers/<int:customer_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_customer(customer_id):
    data, error = read_customer_form()
    if error:
        flash(error, "error")
        return redirect(url_for("admin_customers"))
    full_name, contact_number = data

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT customer_id FROM customers WHERE customer_id = %s",
                       (customer_id,))
        if cursor.fetchone() is None:
            flash("Customer not found.", "error")
        else:
            cursor.execute(
                "UPDATE customers SET full_name = %s, contact_number = %s "
                "WHERE customer_id = %s",
                (full_name, contact_number, customer_id)
            )
            conn.commit()
            flash("Customer details updated.", "success")
    except mysql.connector.Error:
        conn.rollback()
        flash("Something went wrong. Please try again.", "error")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for("admin_customers"))


if __name__ == "__main__":
    app.run(debug=True)