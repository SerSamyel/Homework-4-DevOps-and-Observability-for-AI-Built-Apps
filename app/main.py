import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.telemetry import setup_telemetry

DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

tracer, order_lookup_counter = setup_telemetry()
logger = logging.getLogger("order_tracker")


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        # Bug fix: `placed_at.replace(day=placed_at.day + 2)` raised
        # ValueError whenever the order was placed in the last two days of
        # a month (e.g. day 30 + 2 = 32, which does not exist in any
        # month). Adding a timedelta instead correctly rolls over into the
        # next month.
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


ORDER_LOOKUP_ROUTE = "/api/orders/{order_id}"


@app.get(ORDER_LOOKUP_ROUTE)
def get_order(order_id: str):
    """Look up a single order.

    Instrumented per Question 2/3: a span covering the lookup, a counter
    (`order_lookup_requests_total` once exported) labeled by route and HTTP
    status code, and a structured log line with the same fields.
    """
    status_code = 200
    with tracer.start_as_current_span("order_lookup") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("http.route", ORDER_LOOKUP_ROUTE)
        try:
            with connect() as db:
                row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                status_code = 404
                raise HTTPException(404, "Order not found")
            return order_detail(row)
        except HTTPException as exc:
            status_code = exc.status_code
            raise
        except Exception:
            status_code = 500
            logger.exception("order lookup failed for order_id=%s", order_id)
            raise
        finally:
            span.set_attribute("http.status_code", status_code)
            order_lookup_counter.add(
                1,
                {"http.route": ORDER_LOOKUP_ROUTE, "http.status_code": status_code},
            )
            logger.info(
                "order lookup order_id=%s route=%s status_code=%s",
                order_id,
                ORDER_LOOKUP_ROUTE,
                status_code,
            )


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
