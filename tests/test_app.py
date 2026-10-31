import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
	test_engine = create_engine(
		f"sqlite:///{tmp_path / 'test.db'}",
		connect_args={"check_same_thread": False},
	)
	test_session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
	monkeypatch.setattr(main, "engine", test_engine)
	monkeypatch.setattr(main, "SessionLocal", test_session)
	with TestClient(main.app, follow_redirects=False) as test_client:
		yield test_client
	test_engine.dispose()


def login(client):
	response = client.post("/login", data={"username": "admin", "password": "Admin@123"})
	assert response.status_code == 303


def add_product(client, sku="SKU-1", stock="5"):
	return client.post(
		"/products",
		data={"name": "Widget", "sku": sku, "price": "12.50", "stock": stock},
	)


def test_login_and_dashboard_render(client):
	response = client.get("/login")
	assert response.status_code == 200
	login(client)
	response = client.get("/dashboard")
	assert response.status_code == 200
	assert "Add Product" in response.text
	logout = client.get("/logout")
	assert logout.status_code == 303
	assert logout.headers["location"] == "/login"
	assert client.get("/dashboard").status_code == 303


def test_product_creation_and_duplicate_sku(client):
	login(client)
	response = add_product(client)
	assert response.status_code == 303
	dashboard = client.get("/dashboard")
	assert "Widget" in dashboard.text
	assert "5" in dashboard.text
	with main.SessionLocal() as db:
		product = db.query(main.Product).filter(main.Product.sku == "SKU-1").one()
		assert product.stock == 5
		assert product.quantity == 5

	duplicate = add_product(client)
	assert duplicate.status_code == 400
	assert "SKU already exists" in duplicate.text
	assert "detail" not in duplicate.text


def test_product_update_and_low_stock_alert(client):
	login(client)
	add_product(client, stock="2")
	edit_page = client.get("/products/1/edit")
	assert edit_page.status_code == 200
	updated = client.post("/products/1/edit", data={
		"name": "Updated Widget", "sku": "SKU-UPDATED", "price": "15.75", "stock": "4"
	})
	assert updated.status_code == 303
	dashboard = client.get("/dashboard")
	assert "Updated Widget" in dashboard.text
	assert "SKU-UPDATED" in dashboard.text
	assert "Low-stock alert" in dashboard.text


def test_reports_page_shows_sales_periods(client):
	login(client)
	add_product(client, stock="5")
	sale = client.post("/billing", data={"product_id": "1", "quantity": "1"})
	assert sale.status_code == 303
	reports = client.get("/reports")
	assert reports.status_code == 200
	assert "Last 24 hours" in reports.text
	assert "Last 7 days" in reports.text
	assert "Last 30 days" in reports.text
	assert "#1" in reports.text


def test_staff_can_bill_but_cannot_manage_products(client):
	with main.SessionLocal() as db:
		db.add(main.User(
			username="staff",
			password_hash=main.pwd_context.hash("Staff@123"),
			role="staff",
		))
		db.commit()
	response = client.post("/login", data={"username": "staff", "password": "Staff@123"})
	assert response.status_code == 303
	dashboard = client.get("/dashboard")
	assert "Add Product" not in dashboard.text
	forbidden = client.post("/products", data={
		"name": "Unauthorized", "sku": "NOPE", "price": "1", "stock": "1"
	})
	assert forbidden.status_code == 403
	assert "Only administrators" in forbidden.text
	assert client.get("/billing").status_code == 200


def test_invalid_product_validation_is_html(client):
	login(client)
	response = client.post(
		"/products",
		data={"name": "", "sku": "BAD", "price": "-1", "stock": "-2"},
	)
	assert response.status_code == 400
	assert "Product name is required" in response.text
	assert response.headers["content-type"].startswith("text/html")


def test_billing_rejects_insufficient_stock(client):
	login(client)
	add_product(client, stock="2")
	response = client.post("/billing", data={"product_id": "1", "quantity": "3"})
	assert response.status_code == 400
	assert "Only 2 units available" in response.text


def test_successful_billing_deducts_stock_and_returns_invoice_pdf(client):
	login(client)
	add_product(client, stock="5")
	response = client.post("/billing", data={"product_id": "1", "quantity": "2"})
	assert response.status_code == 303
	assert response.headers["location"] == "/invoices/1"

	invoice = client.get(response.headers["location"])
	assert invoice.status_code == 200
	assert "Invoice #1" in invoice.text
	assert "Widget" in invoice.text

	pdf = client.get("/invoices/1/pdf")
	assert pdf.status_code == 200
	assert pdf.headers["content-type"] == "application/pdf"
	assert pdf.content.startswith(b"%PDF")

	dashboard = client.get("/dashboard")
	assert ">3<" in dashboard.text
