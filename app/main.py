from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
import math
import os

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from passlib.context import CryptContext
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

DATABASE_URL = "sqlite:///./inventory.db"
LOW_STOCK_LIMIT = 5
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="staff", nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    sku = Column(String, unique=True, nullable=False)
    price = Column(Float, nullable=False)
    stock = Column(Integer, default=0, nullable=False)
    quantity = Column(Integer, default=0, nullable=False)
    low_stock_threshold = Column(Integer, default=LOW_STOCK_LIMIT, nullable=False)


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )
    total = Column(Float, nullable=False)
    cashier = Column(String, nullable=False)
    items = relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")


class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    line_total = Column(Float, nullable=False)
    sale = relationship("Sale", back_populates="items")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def is_admin(user):
    return bool(user and user.role.lower() == "admin")


def migrate_schema():
    """Apply additive changes needed by databases created by earlier versions."""
    inspector = inspect(engine)
    table_columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
    }
    with engine.begin() as connection:
        product_columns = table_columns.get("products", set())
        if "stock" not in product_columns:
            connection.execute(text("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 0"))
            if "quantity" in product_columns:
                connection.execute(text("UPDATE products SET stock = quantity"))

        sale_item_columns = table_columns.get("sale_items", set())
        if "product_name" not in sale_item_columns:
            connection.execute(text("ALTER TABLE sale_items ADD COLUMN product_name VARCHAR NOT NULL DEFAULT ''"))
            connection.execute(text(
                "UPDATE sale_items SET product_name = "
                "COALESCE((SELECT name FROM products WHERE products.id = sale_items.product_id), '')"
            ))
        if "line_total" not in sale_item_columns:
            connection.execute(text("ALTER TABLE sale_items ADD COLUMN line_total FLOAT NOT NULL DEFAULT 0"))
            connection.execute(text("UPDATE sale_items SET line_total = quantity * unit_price"))


def validate_product_values(name, sku, price, stock):
    name = (name or "").strip()
    sku = (sku or "").strip()
    error = None
    try:
        parsed_price = float(price) if price is not None else None
        parsed_stock = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        parsed_price = parsed_stock = None
        error = "Price must be a number and stock must be a whole number."
    if not name:
        error = "Product name is required."
    elif not sku:
        error = "SKU is required."
    elif parsed_price is None or not math.isfinite(parsed_price) or parsed_price < 0:
        error = "Price must be a non-negative number."
    elif parsed_stock is None or parsed_stock < 0:
        error = "Stock must be a non-negative whole number."
    return name, sku, parsed_price, parsed_stock, error


def dashboard_context(db, user, error=None, form_data=None):
    products = db.query(Product).order_by(Product.name).all()
    low_stock_products = [product for product in products if product.stock <= LOW_STOCK_LIMIT]
    return {
        "user": user,
        "products": products,
        "product_count": len(products),
        "low_stock_count": len(low_stock_products),
        "low_stock_products": low_stock_products,
        "sales_count": db.query(Sale).count(),
        "revenue": round(sum(s.total for s in db.query(Sale).all()), 2),
        "is_admin": is_admin(user),
        "error": error,
        "form_data": form_data or {},
    }


def render_dashboard(request, db, user, error=None, status_code=200, form_data=None):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=dashboard_context(db, user, error, form_data),
        status_code=status_code,
    )


def billing_error(request, db, user, error, status_code=400):
    products = db.query(Product).filter(Product.stock > 0).order_by(Product.name).all()
    return templates.TemplateResponse(
        request=request,
        name="billing.html",
        context={"user": user, "products": products, "error": error},
        status_code=status_code,
    )


def summarize_sales(sales, since):
    matching_sales = [sale for sale in sales if sale.created_at and sale.created_at >= since]
    return {"count": len(matching_sales), "revenue": round(sum(sale.total for sale in matching_sales), 2)}


def report_context(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sales = db.query(Sale).order_by(Sale.created_at.desc()).all()
    return {
        "today": summarize_sales(sales, now - timedelta(days=1)),
        "week": summarize_sales(sales, now - timedelta(days=7)),
        "month": summarize_sales(sales, now - timedelta(days=30)),
        "sales": sales[:20],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(
                username="admin",
                password_hash=pwd_context.hash("Admin@123"),
                role="admin"
            ))
            db.commit()
    finally:
        db.close()
    yield


app = FastAPI(title="InfioneX Inventory & Billing", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "local-development-secret-change-me")
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd_context.verify(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password"},
            status_code=401
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return render_dashboard(request, db, user)


@app.post("/products")
def add_product(
    request: Request,
    name: str | None = Form(None),
    sku: str | None = Form(None),
    price: str | None = Form(None),
    stock: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return render_dashboard(request, db, user, "Only administrators can add products.", 403)
    name, sku, parsed_price, parsed_stock, error = validate_product_values(
        name, sku, price, stock
    )
    if not error and db.query(Product).filter(Product.sku == sku).first():
        error = "That SKU already exists. Please use a unique SKU."
    if error:
        return render_dashboard(request, db, user, error, 400, {
            "name": name, "sku": sku, "price": price or "", "stock": stock or ""
        })

    try:
        db.add(Product(
            name=name,
            sku=sku,
            price=round(parsed_price, 2),
            stock=parsed_stock,
            quantity=parsed_stock,
            low_stock_threshold=LOW_STOCK_LIMIT,
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.query(Product).filter(Product.sku == sku).first():
            error = "That SKU already exists. Please use a unique SKU."
        else:
            error = "Product could not be saved. Please check the values and try again."
        return render_dashboard(request, db, user, error, 400)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/products/{product_id}/delete")
def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/dashboard", status_code=303)
    product = db.query(Product).filter(Product.id == product_id).first()
    if product:
        db.delete(product)
        db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
def edit_product_page(product_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/dashboard", status_code=303)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return templates.TemplateResponse(request=request, name="edit_product.html", context={
        "user": user, "product": product, "error": None, "form_data": {}
    })


@app.post("/products/{product_id}/edit")
def edit_product(
    product_id: int,
    request: Request,
    name: str | None = Form(None),
    sku: str | None = Form(None),
    price: str | None = Form(None),
    stock: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/dashboard", status_code=303)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    name, sku, parsed_price, parsed_stock, error = validate_product_values(name, sku, price, stock)
    if not error and db.query(Product).filter(Product.sku == sku, Product.id != product_id).first():
        error = "That SKU already exists. Please use a unique SKU."
    form_data = {"name": name, "sku": sku, "price": price or "", "stock": stock or ""}
    if error:
        return templates.TemplateResponse(request=request, name="edit_product.html", context={
            "user": user, "product": product, "error": error, "form_data": form_data
        }, status_code=400)
    product.name = name
    product.sku = sku
    product.price = round(parsed_price, 2)
    product.stock = parsed_stock
    product.quantity = parsed_stock
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(request=request, name="edit_product.html", context={
            "user": user, "product": product,
            "error": "That SKU already exists. Please use a unique SKU.", "form_data": form_data
        }, status_code=400)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    products = db.query(Product).filter(Product.stock > 0).order_by(Product.name).all()
    return templates.TemplateResponse(request=request, name="billing.html", context={
        "user": user, "products": products
    })


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="reports.html", context={
        "user": user, **report_context(db)
    })


@app.post("/billing")
def create_sale(
    request: Request,
    product_id: str | None = Form(None),
    quantity: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        parsed_product_id = int(product_id) if product_id is not None else None
        parsed_quantity = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        parsed_product_id = parsed_quantity = None
    if parsed_product_id is None or parsed_quantity is None or parsed_quantity <= 0:
        return billing_error(request, db, user, "Quantity must be a positive whole number.", 400)

    product = db.query(Product).filter(Product.id == parsed_product_id).first()
    if not product:
        return billing_error(request, db, user, "Product not found.", 404)
    if product.stock < parsed_quantity:
        return billing_error(request, db, user, f"Only {product.stock} units available.", 400)

    unit_price = round(product.price, 2)
    total = round(unit_price * parsed_quantity, 2)
    sale = Sale(total=total, cashier=user.username)
    sale.items.append(SaleItem(
        product_id=product.id,
        product_name=product.name,
        quantity=parsed_quantity,
        unit_price=unit_price,
        line_total=total
    ))
    product.stock -= parsed_quantity
    product.quantity = product.stock
    db.add(sale)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return billing_error(request, db, user, "The sale could not be completed. Please try again.", 400)
    db.refresh(sale)
    return RedirectResponse(f"/invoices/{sale.id}", status_code=303)


@app.get("/invoices/{sale_id}", response_class=HTMLResponse)
def invoice_page(sale_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    sale = db.query(Sale).filter(Sale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return templates.TemplateResponse(request=request, name="invoice.html", context={
        "user": user, "sale": sale
    })


@app.get("/invoices/{sale_id}/pdf")
def invoice_pdf(sale_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    sale = db.query(Sale).filter(Sale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Invoice not found")

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("InfioneX Inventory & Billing", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"Invoice #{sale.id}", styles["Heading2"]),
        Paragraph(f"Date: {sale.created_at.strftime('%d-%m-%Y %H:%M')}", styles["Normal"]),
        Paragraph(f"Cashier: {sale.cashier}", styles["Normal"]),
        Spacer(1, 16)
    ]
    data = [["Product", "Qty", "Unit Price", "Amount"]]
    for item in sale.items:
        data.append([
            item.product_name,
            str(item.quantity),
            f"Rs. {item.unit_price:.2f}",
            f"Rs. {item.line_total:.2f}"
        ])
    data.append(["", "", "Total", f"Rs. {sale.total:.2f}"])
    table = Table(data, colWidths=[230, 50, 100, 100])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice-{sale.id}.pdf"'}
    )

