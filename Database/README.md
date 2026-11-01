# PawCare - Pet Grooming Appointment Management System

Flask + MySQL (XAMPP) final project for IM2.

## How to run
1. Start Apache and MySQL in XAMPP.
2. In phpMyAdmin, create a database named `pet_grooming_db` and import `database/pet_grooming_db.sql`.
3. In a terminal in this folder:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
4. Open http://127.0.0.1:5000

## Test accounts
- Admin: admin@pawcare.ph / admin123
- Customer: customer1@pawcare.ph / customer123
- Staff: (your staff email) / (password)