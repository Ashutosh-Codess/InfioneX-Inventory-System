# InfioneX Smart Inventory & Billing Management System

FastAPI and SQLite inventory application for the InfioneX Software Development Internship Program 2026.

## Features

- Admin and staff login with session-based role access.
- Product creation, update, deletion, SKU uniqueness, price/stock validation, and stock tracking.
- Visible low-stock alerts for products with five or fewer units.
- Billing with stock validation, atomic stock deduction, HTML invoices, and downloadable ReportLab PDFs.
- Sales reports for the last 24 hours, 7 days, and 30 days.
- Safe additive migration for legacy `inventory.db` schemas. Existing users, products, and sales are preserved.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. On a new database the initial administrator is `admin` / `Admin@123`; change that password before production use.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The tests use temporary SQLite databases and do not delete or recreate the working `inventory.db`.
