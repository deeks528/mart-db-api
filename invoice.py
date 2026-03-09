"""
invoice.py — Invoice store, models, and all /invoice* routes.
Mounted into main.py via:  app.include_router(invoice.router)
"""

import os
import json
from collections import defaultdict
from datetime import datetime, date as date_type
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

router    = APIRouter()
templates = Jinja2Templates(directory="templates")

# ── Memory-limited invoice store ──────────────────────────────────────────────
INVOICES: list[dict] = []
INVOICE_LIMIT_BYTES = 512 * 1024   # default 512 KB

_env = os.environ.get("INVOICE_MEMORY_LIMIT", "").strip().upper()
if _env.endswith("MB"):
    INVOICE_LIMIT_BYTES = int(float(_env[:-2]) * 1024 * 1024)
elif _env.endswith("KB"):
    INVOICE_LIMIT_BYTES = int(float(_env[:-2]) * 1024)
elif _env.isdigit():
    INVOICE_LIMIT_BYTES = int(_env)


def _size_bytes() -> int:
    """JSON-serialised byte size of the invoice store."""
    return len(json.dumps(INVOICES).encode())


def _evict_oldest() -> int:
    """Drop oldest invoices until the store is under the byte limit."""
    evicted = 0
    while INVOICES and _size_bytes() > INVOICE_LIMIT_BYTES:
        dropped = INVOICES.pop(0)
        evicted += 1
        print(f"⚠  Invoice store over limit — evicted {dropped['invoice_id']}")
    return evicted


# ── Pydantic models ───────────────────────────────────────────────────────────
class InvoiceItem(BaseModel):
    barcode:    str
    title:      str
    quantity:   int   = Field(..., gt=0)
    unit_price: float = Field(..., gt=0)


class InvoiceIn(BaseModel):
    items:          list[InvoiceItem]    = Field(..., min_length=1)
    total_quantity: int                  = Field(..., gt=0)
    total:          float                = Field(..., gt=0)
    date:           datetime | date_type = Field(default_factory=datetime.utcnow)
    branch:         Optional[str]        = None


class InvoiceOut(InvoiceIn):
    invoice_id:  str
    received_at: datetime


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/invoice", response_model=InvoiceOut, status_code=201,
             summary="Accept a completed invoice from the POS app")
def create_invoice(payload: InvoiceIn):
    derived_qty   = sum(i.quantity for i in payload.items)
    derived_total = round(sum(i.quantity * i.unit_price for i in payload.items), 2)

    if derived_qty != payload.total_quantity:
        raise HTTPException(
            422,
            detail=f"total_quantity mismatch: sent {payload.total_quantity}, computed {derived_qty}",
        )
    if abs(derived_total - round(payload.total, 2)) > 0.50:
        raise HTTPException(
            422,
            detail=f"total mismatch: sent {payload.total}, computed {derived_total}",
        )

    invoice = InvoiceOut(
        **payload.model_dump(),
        invoice_id  = f"INV-{uuid4().hex[:8].upper()}",
        received_at = datetime.utcnow(),
    )
    INVOICES.append(invoice.model_dump())
    evicted = _evict_oldest()

    size_kb  = _size_bytes() / 1024
    limit_kb = INVOICE_LIMIT_BYTES / 1024
    print(
        f"✓ Invoice {invoice.invoice_id} | {invoice.total_quantity} items | ₹{invoice.total} "
        f"| store {size_kb:.1f}/{limit_kb:.0f} KB ({len(INVOICES)} invoices)"
        + (f" | evicted {evicted}" if evicted else "")
    )
    return invoice


@router.get("/invoices", response_class=JSONResponse,
            summary="List all received invoices (latest first)")
def list_invoices(limit: int = Query(50, ge=1, le=500)):
    return INVOICES[-limit:][::-1]


@router.get("/invoices/stats", response_class=JSONResponse,
            summary="Invoice store memory usage and stats")
def invoice_stats():
    size_bytes = _size_bytes()
    return {
        "count":          len(INVOICES),
        "size_bytes":     size_bytes,
        "size_kb":        round(size_bytes / 1024, 2),
        "limit_bytes":    INVOICE_LIMIT_BYTES,
        "limit_kb":       round(INVOICE_LIMIT_BYTES / 1024, 2),
        "used_pct":       round(size_bytes / INVOICE_LIMIT_BYTES * 100, 1),
        "oldest_invoice": INVOICES[0]["invoice_id"]  if INVOICES else None,
        "newest_invoice": INVOICES[-1]["invoice_id"] if INVOICES else None,
    }


@router.get("/invoices/ui", summary="Invoice dashboard UI")
def invoices_ui(request: Request):
    size_bytes    = _size_bytes()
    revenue       = sum(inv["total"] for inv in INVOICES)
    qty           = sum(inv["total_quantity"] for inv in INVOICES)
    branch_totals: dict = defaultdict(lambda: {"count": 0, "total": 0.0})
    for inv in INVOICES:
        b = inv.get("branch") or "—"
        branch_totals[b]["count"] += 1
        branch_totals[b]["total"] += inv["total"]

    return templates.TemplateResponse("invoices.html", {
        "request":       request,
        "invoices":      list(reversed(INVOICES)),
        "now":           datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "stats": {
            "count":    len(INVOICES),
            "size_kb":  round(size_bytes / 1024, 2),
            "limit_kb": round(INVOICE_LIMIT_BYTES / 1024, 2),
            "used_pct": round(size_bytes / INVOICE_LIMIT_BYTES * 100, 1),
            "revenue":  round(revenue, 2),
            "qty":      qty,
        },
        "branch_totals": sorted(
            branch_totals.items(),
            key=lambda x: x[1]["total"],
            reverse=True,
        ),
    })


@router.get("/invoice/{invoice_id}", response_class=JSONResponse,
            summary="Fetch a single invoice by ID")
def get_invoice(invoice_id: str):
    for inv in INVOICES:
        if inv["invoice_id"] == invoice_id.upper():
            return inv
    raise HTTPException(404, detail=f"Invoice '{invoice_id}' not found")