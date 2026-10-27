"""Finance tracker API: CRUD, filters, summaries, rules, import and export."""

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import routes.finance.finance_routes as finance_routes
from src.finance import csv_io

CURRENT_USER = {"name": "alice"}


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(finance_routes, "engine", engine)
    monkeypatch.setattr(finance_routes, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(finance_routes, "require_user", lambda request: CURRENT_USER["name"])
    CURRENT_USER["name"] = "alice"
    app = FastAPI()
    app.include_router(finance_routes.setup_finance_routes())
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _account(client, name="Current", **kw):
    return _ok(client.post("/api/finance/accounts", json={"name": name, **kw}))


def _categories(client):
    return {c["name"]: c for c in _ok(client.get("/api/finance/categories"))["categories"]}


def _tx(client, account_id, **kw):
    body = {"account_id": account_id, "date": "2026-10-01", "amount": "10", "direction": "out", **kw}
    return _ok(client.post("/api/finance/transactions", json=body))


# ── Accounts ────────────────────────────────────────────────────────────


def test_account_crud_and_balances(client):
    acc = _account(client, opening_balance="100.00")
    assert acc["balance_pence"] == 10000 and acc["account_type"] == "current"
    _tx(client, acc["id"], amount="12.34")
    listed = _ok(client.get("/api/finance/accounts"))
    assert listed["total_pence"] == 10000 - 1234
    renamed = _ok(client.patch(f"/api/finance/accounts/{acc['id']}", json={"name": "Main"}))
    assert renamed["name"] == "Main" and renamed["balance_pence"] == 8766


def test_archive_and_delete_rules(client):
    used = _account(client, "Used")
    empty = _account(client, "Empty")
    _tx(client, used["id"])
    assert client.delete(f"/api/finance/accounts/{used['id']}").status_code == 409
    _ok(client.patch(f"/api/finance/accounts/{used['id']}", json={"archived": True}))
    active = _ok(client.get("/api/finance/accounts"))["accounts"]
    assert [a["name"] for a in active] == ["Empty"]
    everything = _ok(client.get("/api/finance/accounts?include_archived=true"))
    assert {a["name"] for a in everything["accounts"]} == {"Used", "Empty"}
    assert everything["total_pence"] == 0  # archived accounts are left out of the total
    _ok(client.delete(f"/api/finance/accounts/{empty['id']}"))
    assert client.delete(f"/api/finance/accounts/{empty['id']}").status_code == 404


def test_validation_errors_are_400(client):
    assert client.post("/api/finance/accounts", json={"name": ""}).status_code == 400
    acc = _account(client)
    bad = {"account_id": acc["id"], "date": "2026-10-01", "amount": "abc", "direction": "out"}
    assert client.post("/api/finance/transactions", json=bad).status_code == 400
    assert client.get("/api/finance/summary?year=2026&month=13").status_code == 400


# ── Categories ──────────────────────────────────────────────────────────


def test_category_crud_and_delete_reassigns(client):
    acc = _account(client)
    cat = _ok(client.post("/api/finance/categories",
                          json={"name": "Coffee", "kind": "expense", "color": "orange"}))
    tx = _tx(client, acc["id"], category_id=cat["id"])
    assert _ok(client.get(f"/api/finance/categories/{cat['id']}/usage")) == {"transactions": 1}
    _ok(client.patch(f"/api/finance/categories/{cat['id']}", json={"name": "Cafes"}))
    assert _ok(client.delete(f"/api/finance/categories/{cat['id']}"))["reassigned"] == 1
    cats = _categories(client)
    listed = _ok(client.get("/api/finance/transactions"))["transactions"]
    assert listed[0]["id"] == tx["id"]
    assert listed[0]["category_id"] == cats["Uncategorised"]["id"]
    assert client.delete(f"/api/finance/categories/{cats['Uncategorised']['id']}").status_code == 409
    assert client.post("/api/finance/categories", json={"name": "groceries"}).status_code == 409


# ── Transactions, transfers, filters ────────────────────────────────────


def test_transaction_edit_and_delete(client):
    acc = _account(client)
    tx = _tx(client, acc["id"], description="Lunch")
    edited = _ok(client.patch(f"/api/finance/transactions/{tx['id']}",
                              json={"amount": "8.50", "notes": "with Sam"}))
    assert edited["amount_pence"] == 850 and edited["notes"] == "with Sam"
    assert edited["description"] == "Lunch"
    _ok(client.delete(f"/api/finance/transactions/{tx['id']}"))
    assert _ok(client.get("/api/finance/transactions"))["transactions"] == []


def test_transfer_endpoints(client):
    a = _account(client, "A", opening_balance="100")
    b = _account(client, "B")
    pair = _ok(client.post("/api/finance/transfers", json={
        "from_account_id": a["id"], "to_account_id": b["id"], "date": "2026-10-02", "amount": "40"}))
    assert pair["out"]["transfer_account_id"] == b["id"]
    balances = {x["name"]: x["balance_pence"] for x in _ok(client.get("/api/finance/accounts"))["accounts"]}
    assert balances == {"A": 6000, "B": 4000}
    _ok(client.patch(f"/api/finance/transactions/{pair['in']['id']}", json={"amount": "30"}))
    balances = {x["name"]: x["balance_pence"] for x in _ok(client.get("/api/finance/accounts"))["accounts"]}
    assert balances == {"A": 7000, "B": 3000}
    assert _ok(client.delete(f"/api/finance/transactions/{pair['out']['id']}"))["deleted"] == 2
    summary = _ok(client.get("/api/finance/summary?year=2026&month=10"))
    assert summary["total_out_pence"] == 0


def test_month_filters_search_and_summary(client):
    acc = _account(client, "Current")
    cash = _account(client, "Cash", account_type="cash")
    cats = _categories(client)
    _tx(client, acc["id"], date="2026-10-01", amount="2000", direction="in",
        category_id=cats["Salary"]["id"], description="ACME payroll")
    _tx(client, acc["id"], date="2026-10-03", amount="45.20", category_id=cats["Groceries"]["id"],
        description="Tesco Express")
    _tx(client, cash["id"], date="2026-10-04", amount="3.10", category_id=cats["Eating out"]["id"],
        description="Coffee")
    _tx(client, acc["id"], date="2026-09-29", amount="50", description="Last month")
    month = _ok(client.get("/api/finance/transactions?year=2026&month=10"))["transactions"]
    assert [t["description"] for t in month] == ["Coffee", "Tesco Express", "ACME payroll"]
    by_account = _ok(client.get(f"/api/finance/transactions?year=2026&month=10&account_id={cash['id']}"))
    assert [t["description"] for t in by_account["transactions"]] == ["Coffee"]
    incoming = _ok(client.get("/api/finance/transactions?year=2026&month=10&direction=in"))
    assert [t["description"] for t in incoming["transactions"]] == ["ACME payroll"]
    by_cat = _ok(client.get(f"/api/finance/transactions?category_id={cats['Groceries']['id']}"))
    assert len(by_cat["transactions"]) == 1
    found = _ok(client.get("/api/finance/transactions?q=tesco"))["transactions"]
    assert [t["description"] for t in found] == ["Tesco Express"]
    ranged = _ok(client.get("/api/finance/transactions?start=2026-09-29&end=2026-10-01"))
    assert {t["description"] for t in ranged["transactions"]} == {"Last month", "ACME payroll"}
    summary = _ok(client.get("/api/finance/summary?year=2026&month=10"))
    assert summary["total_in_pence"] == 200000
    assert summary["total_out_pence"] == 4830
    assert summary["net_pence"] == 200000 - 4830
    assert [c["name"] for c in summary["spend_by_category"]] == ["Groceries", "Eating out"]


# ── Owner isolation ─────────────────────────────────────────────────────


def test_users_only_see_their_own_finances(client):
    acc = _account(client, "Alice's")
    tx = _tx(client, acc["id"])
    CURRENT_USER["name"] = "bob"
    assert _ok(client.get("/api/finance/accounts"))["accounts"] == []
    assert _ok(client.get("/api/finance/transactions"))["transactions"] == []
    assert client.delete(f"/api/finance/transactions/{tx['id']}").status_code == 404
    assert client.patch(f"/api/finance/accounts/{acc['id']}", json={"name": "x"}).status_code == 404


def test_internal_callers_are_refused(client):
    CURRENT_USER["name"] = "internal-tool"
    assert client.get("/api/finance/accounts").status_code == 403


def test_no_login_mode_uses_the_local_owner(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    CURRENT_USER["name"] = ""
    _account(client, "Local")
    assert [a["name"] for a in _ok(client.get("/api/finance/accounts"))["accounts"]] == ["Local"]


# ── CSV import ──────────────────────────────────────────────────────────

MONZO_CSV = (
    "Date,Name,Amount,Notes\r\n"
    "01/10/2026,ACME Ltd Salary,2000.00,\r\n"
    "02/10/2026,TESCO STORES 1234,-23.45,\r\n"
    "03/10/2026,Pret A Manger,-4.10,\r\n"
    "03/10/2026,Pret A Manger,-4.10,\r\n"
    "31/02/2026,Bad date row,-1.00,\r\n"
    "04/10/2026,Mystery,abc,\r\n"
)
MONZO_MAPPING = {"date_column": "Date", "description_column": "Name", "amount_mode": "signed",
                 "amount_column": "Amount"}


def _import_body(account_id, csv_text=MONZO_CSV, mapping=MONZO_MAPPING, **kw):
    return {"csv_text": csv_text, "mapping": mapping, "account_id": account_id,
            "filename": "monzo.csv", "mapping_name": "Monzo", **kw}


def test_import_preview_flags_errors_and_applies_rules(client):
    acc = _account(client)
    cats = _categories(client)
    _ok(client.post("/api/finance/rules", json={"pattern": "tesco", "category_id": cats["Groceries"]["id"]}))
    preview = _ok(client.post("/api/finance/import/preview", json=_import_body(acc["id"])))
    assert preview["header"] == ["Date", "Name", "Amount", "Notes"]
    assert preview["counts"] == {"total": 6, "errors": 2, "duplicates": 0, "importable": 4}
    rows = {r["row_number"]: r for r in preview["rows"]}
    assert rows[2]["direction"] == "in" and rows[2]["amount_pence"] == 200000
    assert rows[3]["category_id"] == cats["Groceries"]["id"] and rows[3]["rule_id"]
    assert rows[4]["category_id"] == cats["Uncategorised"]["id"]
    assert rows[6]["error"] and rows[7]["error"]
    # Nothing is saved by a preview.
    assert _ok(client.get("/api/finance/transactions"))["transactions"] == []


def test_import_commit_skips_duplicates_and_can_be_undone(client):
    acc = _account(client)
    cats = _categories(client)
    _tx(client, acc["id"], date="2026-10-03", amount="4.10", description="pret a manger")
    result = _ok(client.post("/api/finance/import/commit", json=_import_body(
        acc["id"], category_overrides={"2": cats["Salary"]["id"]})))
    assert result["batch"]["imported_count"] == 3  # salary, tesco, one of the two coffees
    reasons = sorted(s["reason"] for s in result["skipped"])
    assert reasons == ["duplicate", "error", "error"]
    salary = _ok(client.get("/api/finance/transactions?direction=in"))["transactions"][0]
    assert salary["category_id"] == cats["Salary"]["id"] and salary["import_batch_id"]
    # Importing the same file again imports nothing new.
    again = _ok(client.post("/api/finance/import/commit", json=_import_body(acc["id"])))
    assert again["batch"]["imported_count"] == 0
    batches = _ok(client.get("/api/finance/imports"))["imports"]
    assert len(batches) == 2 and batches[1]["imported_count"] == 3
    assert _ok(client.post(f"/api/finance/imports/{result['batch']['id']}/undo"))["removed"] == 3
    remaining = _ok(client.get("/api/finance/transactions"))["transactions"]
    assert [t["description"] for t in remaining] == ["pret a manger"]  # manual entry kept
    assert client.post(f"/api/finance/imports/{result['batch']['id']}/undo").status_code == 409


def test_import_with_split_columns_and_excluded_rows(client):
    acc = _account(client)
    text = ("Transaction Date;Description;Paid In;Paid Out\n"
            "2026-10-01;Refund;12.00;\n"
            "2026-10-02;Rent;;£950.00\n"
            "2026-10-03;Gym;;30\n")
    mapping = {"date_column": "Transaction Date", "description_column": "Description",
               "amount_mode": "split", "paid_in_column": "Paid In", "paid_out_column": "Paid Out",
               "date_format": "yyyy-mm-dd"}
    result = _ok(client.post("/api/finance/import/commit",
                             json=_import_body(acc["id"], text, mapping, exclude_rows=[4])))
    assert result["batch"]["imported_count"] == 2
    assert [s["reason"] for s in result["skipped"]] == ["excluded"]
    rows = {t["description"]: t for t in _ok(client.get("/api/finance/transactions"))["transactions"]}
    assert rows["Refund"]["direction"] == "in" and rows["Refund"]["amount_pence"] == 1200
    assert rows["Rent"]["direction"] == "out" and rows["Rent"]["amount_pence"] == 95000


def test_import_without_header_by_column_number(client):
    acc = _account(client)
    text = "27/10/2026,Corner shop,-2.50\n"
    mapping = {"has_header": False, "date_column": 0, "description_column": 1,
               "amount_column": 2}
    preview = _ok(client.post("/api/finance/import/preview", json=_import_body(acc["id"], text, mapping)))
    assert preview["header"] is None
    assert preview["rows"][0]["row_number"] == 1 and preview["rows"][0]["amount_pence"] == 250


def test_import_rejects_bad_mapping_and_unknown_column(client):
    acc = _account(client)
    bad = client.post("/api/finance/import/preview", json=_import_body(acc["id"], mapping={"date_column": "Date"}))
    assert bad.status_code == 400
    missing = dict(MONZO_MAPPING, amount_column="Value")
    assert client.post("/api/finance/import/preview",
                       json=_import_body(acc["id"], mapping=missing)).status_code == 400


# ── Rules and saved mappings ────────────────────────────────────────────


def test_rules_crud_longest_match_wins(client):
    acc = _account(client)
    cats = _categories(client)
    _ok(client.post("/api/finance/rules", json={"pattern": "pret", "category_id": cats["Eating out"]["id"]}))
    specific = _ok(client.post("/api/finance/rules",
                               json={"pattern": "pret a manger", "category_id": cats["Shopping"]["id"]}))
    # "Remember this" twice for the same text updates instead of duplicating.
    _ok(client.post("/api/finance/rules", json={"pattern": "PRET A MANGER", "category_id": cats["Health"]["id"]}))
    rules = _ok(client.get("/api/finance/rules"))["rules"]
    assert len(rules) == 2 and rules[0]["id"] == specific["id"]
    assert rules[0]["category_id"] == cats["Health"]["id"]
    preview = _ok(client.post("/api/finance/import/preview", json=_import_body(acc["id"])))
    pret = next(r for r in preview["rows"] if r["description"] == "Pret A Manger")
    assert pret["category_id"] == cats["Health"]["id"]
    _ok(client.patch(f"/api/finance/rules/{specific['id']}", json={"pattern": "manger"}))
    _ok(client.delete(f"/api/finance/rules/{specific['id']}"))
    assert len(_ok(client.get("/api/finance/rules"))["rules"]) == 1


def test_saved_mappings(client):
    saved = _ok(client.post("/api/finance/mappings", json={"name": "Monzo", "config": MONZO_MAPPING}))
    assert saved["config"]["date_format"] == "dd/mm/yyyy"
    _ok(client.post("/api/finance/mappings",
                    json={"name": "monzo", "config": dict(MONZO_MAPPING, invert_amounts=True)}))
    mappings = _ok(client.get("/api/finance/mappings"))["mappings"]
    assert len(mappings) == 1 and mappings[0]["config"]["invert_amounts"] is True
    assert client.post("/api/finance/mappings", json={"name": "Bad", "config": {}}).status_code == 400
    _ok(client.delete(f"/api/finance/mappings/{saved['id']}"))
    assert _ok(client.get("/api/finance/mappings"))["mappings"] == []


# ── CSV export ──────────────────────────────────────────────────────────


def test_export_csv_opens_cleanly(client):
    acc = _account(client, "Current")
    other = _account(client, "Savings")
    cats = _categories(client)
    _tx(client, acc["id"], date="2026-10-01", amount="12.5", category_id=cats["Groceries"]["id"],
        description="=HYPERLINK(\"x\")", notes="£ and café")
    _tx(client, acc["id"], date="2026-11-01", amount="1", description="Outside range")
    _ok(client.post("/api/finance/transfers", json={
        "from_account_id": acc["id"], "to_account_id": other["id"], "date": "2026-10-02", "amount": "5"}))
    response = client.get(f"/api/finance/export?start=2026-10-01&end=2026-10-31&account_id={acc['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="finance-2026-10-01-2026-10-31.csv"' in response.headers["content-disposition"]
    raw = response.content
    assert raw.startswith("﻿".encode("utf-8"))
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == csv_io.EXPORT_COLUMNS
    assert rows[1] == ["2026-10-01", "Current", "'=HYPERLINK(\"x\")", "Groceries", "out", "12.50", "£ and café"]
    assert rows[2][3] == "Transfer" and rows[2][5] == "5.00"
    assert len(rows) == 3
