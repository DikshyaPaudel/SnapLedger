"""
Push confirmed SnapLedger documents to ERPNext as DRAFT invoices.

Needs these environment variables (see .env.example):
    ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_COMPANY
Optional:
    ERPNEXT_ITEM_CODE (default "Scanned Item"), ERPNEXT_ITEM_GROUP, ERPNEXT_UOM,
    ERPNEXT_SUPPLIER_GROUP, ERPNEXT_CUSTOMER_GROUP, ERPNEXT_TERRITORY,
    ERPNEXT_EXPENSE_ACCOUNT, ERPNEXT_INCOME_ACCOUNT

Invoices are created as drafts (docstatus 0) so an accountant makes the final submit.
Amounts are sent as-is, so the document currency is assumed to match the company currency.
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import quote

import httpx


class ERPNextError(Exception):
    """ERPNext could not be reached, or it rejected the request."""


class ERPNextNotConfigured(ERPNextError):
    """Required ERPNEXT_* environment variables are missing."""


class ERPNextInvalidDocument(ERPNextError):
    """This SnapLedger document cannot be pushed as it is."""


# How each SnapLedger document type maps to ERPNext doctypes and field names.
DOCTYPES = {
    "purchase_invoice": {
        "doctype": "Purchase Invoice", "party_doctype": "Supplier", "party_field": "supplier",
        "name_field": "supplier_name", "group_field": "supplier_group", "type_field": "supplier_type",
    },
    "sales_invoice": {
        "doctype": "Sales Invoice", "party_doctype": "Customer", "party_field": "customer",
        "name_field": "customer_name", "group_field": "customer_group", "type_field": "customer_type",
    },
}


@dataclass
class Settings:
    url: str
    key: str
    secret: str
    company: str
    item_code: str = "Scanned Item"
    item_group: str = "All Item Groups"
    uom: str = "Nos"
    supplier_group: str = "All Supplier Groups"
    customer_group: str = "All Customer Groups"
    territory: str = "All Territories"
    expense_account: Optional[str] = None
    income_account: Optional[str] = None
    public_url: Optional[str] = None  # optional override for the public URL of the ERPNext instance

@dataclass
class PushResult:
    name: str
    url: str


def load_settings() -> Settings:
    env = os.environ.get
    required = {k: env(k, "").strip() for k in ("ERPNEXT_URL", "ERPNEXT_API_KEY", "ERPNEXT_API_SECRET", "ERPNEXT_COMPANY")}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ERPNextNotConfigured("ERPNext is not configured. Missing: " + ", ".join(missing))
    return Settings(
        url=required["ERPNEXT_URL"].rstrip("/"), key=required["ERPNEXT_API_KEY"],
        secret=required["ERPNEXT_API_SECRET"], company=required["ERPNEXT_COMPANY"],
        item_code=env("ERPNEXT_ITEM_CODE", "Scanned Item"), item_group=env("ERPNEXT_ITEM_GROUP", "All Item Groups"),
        uom=env("ERPNEXT_UOM", "Nos"), supplier_group=env("ERPNEXT_SUPPLIER_GROUP", "All Supplier Groups"),
        customer_group=env("ERPNEXT_CUSTOMER_GROUP", "All Customer Groups"),
        territory=env("ERPNEXT_TERRITORY", "All Territories"),
        expense_account=env("ERPNEXT_EXPENSE_ACCOUNT") or None, income_account=env("ERPNEXT_INCOME_ACCOUNT") or None,
        public_url=(env("ERPNEXT_PUBLIC_URL") or "").rstrip("/") or None,
    )


def make_client(s: Settings, transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    # ERPNext token auth: "Authorization: token api_key:api_secret"
    return httpx.Client(
        base_url=s.url, transport=transport, timeout=30,
        headers={"Authorization": f"token {s.key}:{s.secret}", "Accept": "application/json"},
    )


def _error_message(resp: httpx.Response) -> str:
    """Frappe puts readable errors in _server_messages (a JSON string of JSON strings)."""
    try:
        body = resp.json()
    except ValueError:
        return f"ERPNext returned HTTP {resp.status_code}."
    texts = []
    try:
        for raw in json.loads(body.get("_server_messages") or "[]"):
            texts.append(json.loads(raw).get("message", ""))
    except (ValueError, AttributeError, TypeError):
        pass
    text = " ".join(t for t in texts if t) or str(body.get("exception") or body.get("message") or "")
    text = re.sub(r"<[^>]+>", "", text).strip()  # strip HTML tags
    return text or f"ERPNext returned HTTP {resp.status_code}."


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        raise ERPNextError("ERPNext did not respond in time.")
    except httpx.HTTPError:
        raise ERPNextError("Could not reach ERPNext. Check ERPNEXT_URL.")
    if resp.status_code in (401, 403):
        raise ERPNextError("ERPNext refused the request. Check the API key, secret and user permissions. " + _error_message(resp))
    if resp.status_code >= 400:
        raise ERPNextError(_error_message(resp))
    return resp.json()


def _find(client: httpx.Client, doctype: str, field: str, value: str) -> Optional[str]:
    """Look up a document by a field value. Returns its ERPNext name, or None."""
    params = {"filters": json.dumps([[field, "=", value]]), "fields": json.dumps(["name"]), "limit_page_length": 1}
    rows = _request(client, "GET", f"/api/resource/{quote(doctype)}", params=params).get("data") or []
    return rows[0]["name"] if rows else None


def _ensure_party(client: httpx.Client, cfg: dict, name: str, s: Settings) -> str:
    existing = _find(client, cfg["party_doctype"], cfg["name_field"], name)
    if existing:
        return existing
    group = s.supplier_group if cfg["party_doctype"] == "Supplier" else s.customer_group
    body = {cfg["name_field"]: name, cfg["group_field"]: group, cfg["type_field"]: "Company"}
    if cfg["party_doctype"] == "Customer":
        body["territory"] = s.territory
    return _request(client, "POST", f"/api/resource/{cfg['party_doctype']}", json=body)["data"]["name"]


def _ensure_item(client: httpx.Client, s: Settings) -> None:
    if _find(client, "Item", "item_code", s.item_code):
        return
    body = {"item_code": s.item_code, "item_name": s.item_code, "item_group": s.item_group, "stock_uom": s.uom,
            "is_stock_item": 0, "is_purchase_item": 1, "is_sales_item": 1}
    _request(client, "POST", "/api/resource/Item", json=body)


def _line(description: str, amount: float, s: Settings, kind: str) -> dict:
    line = {"item_code": s.item_code, "item_name": description[:140], "description": description, "qty": 1, "rate": amount}
    account = s.expense_account if kind == "purchase_invoice" else s.income_account
    if account:
        line["expense_account" if kind == "purchase_invoice" else "income_account"] = account
    return line


def _build_body(doc, kind: str, s: Settings) -> dict:
    lines = []
    for item in doc.line_items or []:
        if item.get("amount") is None:
            continue
        lines.append(_line((item.get("description") or "").strip() or "Scanned line", item["amount"], s, kind))
    extra = {}
    if doc.total_amount is not None:
        diff = round(doc.total_amount - round(sum(l["rate"] for l in lines), 2), 2)
        if not lines:
            lines.append(_line("Scanned document total", doc.total_amount, s, kind))
        elif diff > 0.01:  # lines are short of the total (tax or a missed line)
            lines.append(_line("Adjustment to match document total (tax or missed line)", diff, s, kind))
        elif diff < -0.01:  # lines exceed the total (a discount), ERPNext does not accept negative rates
            extra = {"discount_amount": abs(diff), "apply_discount_on": "Net Total"}
    if not lines:
        raise ERPNextInvalidDocument("This document has no amounts to push.")
    posting = doc.date if doc.date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc.date) else date.today().isoformat()
    body = {"company": s.company, "posting_date": posting, "set_posting_time": 1, "due_date": posting, "items": lines,
            "remarks": f"Created by SnapLedger from document #{doc.id} (currency: {doc.currency or 'not detected'})", **extra}
    if kind == "purchase_invoice":
        body["bill_date"] = posting
    return body


def push_document(doc, settings: Optional[Settings] = None, client: Optional[httpx.Client] = None) -> PushResult:
    kind = doc.document_type or "receipt"
    cfg = DOCTYPES.get(kind)
    if cfg is None:
        raise ERPNextInvalidDocument("Receipts cannot be pushed yet. Save it as a Purchase invoice if it is a supplier bill.")
    party = (doc.vendor or "").strip()
    if not party:
        raise ERPNextInvalidDocument(f"The {cfg['party_doctype'].lower()} name is missing.")
    s = settings or load_settings()
    body = _build_body(doc, kind, s)  # validates amounts before anything is created in ERPNext
    owns_client = client is None
    client = client or make_client(s)
    try:
        body[cfg["party_field"]] = _ensure_party(client, cfg, party, s)
        _ensure_item(client, s)
        name = _request(client, "POST", f"/api/resource/{quote(cfg['doctype'])}", json=body)["data"]["name"]
    finally:
        if owns_client:
            client.close()
    slug = cfg["doctype"].lower().replace(" ", "-")
    base = s.public_url or s.url  # links open in your browser, so they must not use the Docker-only address
    return PushResult(name=name, url=f"{base}/app/{slug}/{quote(name)}")