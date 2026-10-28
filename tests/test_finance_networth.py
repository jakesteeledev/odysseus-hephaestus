"""Net worth: assets, liabilities, valuations, contributions and equity."""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import networth as nw
from src.finance import service as fin
from src.finance.models import FinanceContribution, ensure_tables

OWNER = "alice"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    monkeypatch.setattr(fin, "_today_for_owner", lambda: date(2026, 10, 27))
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _groups(result, side):
    return {g["type"]: g for g in result[side]}


def test_net_worth_counts_accounts_cards_assets_and_liabilities(db):
    current = fin.create_account(db, OWNER, name="Current", opening_balance="1000")
    fin.create_account(db, OWNER, name="Amex", account_type="credit_card", opening_balance="-250")
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000")
    nw.create_item(db, OWNER, "asset", name="Work pension", asset_type="pension", value="45000.50")
    nw.create_item(db, OWNER, "liability", name="Home mortgage", liability_type="mortgage", value="180000",
                   linked_asset_id=house.id)
    nw.create_item(db, OWNER, "liability", name="SLC", liability_type="student_loan", value="32000")
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-02", amount="20", direction="out")

    result = nw.net_worth(db, OWNER)
    assets = _groups(result, "assets")
    liabilities = _groups(result, "liabilities")
    assert assets["accounts"]["total_pence"] == 98000
    assert assets["property"]["total_pence"] == 30000000
    assert assets["pension"]["total_pence"] == 4500050
    assert liabilities["credit_card"]["total_pence"] == 25000  # -£250 card balance = £250 owed
    assert liabilities["mortgage"]["total_pence"] == 18000000
    assert liabilities["student_loan"]["total_pence"] == 3200000
    assert result["assets_total_pence"] == 98000 + 30000000 + 4500050
    assert result["liabilities_total_pence"] == 25000 + 18000000 + 3200000
    assert result["net_worth_pence"] == result["assets_total_pence"] - result["liabilities_total_pence"]
    home = assets["property"]["items"][0]
    assert home["equity_pence"] == 30000000 - 18000000 and home["mortgages"][0]["name"] == "Home mortgage"


def test_matches_v1_total_when_there_are_only_accounts(db):
    fin.create_account(db, OWNER, name="Current", opening_balance="500")
    fin.create_account(db, OWNER, name="Card", account_type="credit_card", opening_balance="-120")
    assert nw.net_worth(db, OWNER)["net_worth_pence"] == fin.accounts_overview(db, OWNER)["total_pence"]


def test_archived_items_count_until_they_are_zero(db):
    old = fin.create_account(db, OWNER, name="Old savings", account_type="savings", opening_balance="40")
    fin.update_account(db, OWNER, old.id, archived=True)
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle", value="6000")
    nw.update_item(db, OWNER, "asset", car.id, archived=True)
    loan = nw.create_item(db, OWNER, "liability", name="Car loan", liability_type="loan", value="0")
    nw.update_item(db, OWNER, "liability", loan.id, archived=True)
    result = nw.net_worth(db, OWNER)
    names = [i["name"] for side in ("assets", "liabilities") for g in result[side] for i in g["items"]]
    assert "Old savings" in names and "Car" in names and "Car loan" not in names
    nw.set_valuation(db, OWNER, "asset", car.id, value="0", on="2026-10-27")
    fin.create_transaction(db, OWNER, account_id=old.id, date="2026-10-01", amount="40", direction="out")
    names = [i["name"] for g in nw.net_worth(db, OWNER)["assets"] for i in g["items"]]
    assert names == []


def test_latest_snapshot_counts_and_history_works(db):
    isa = nw.create_item(db, OWNER, "asset", name="Vanguard ISA", asset_type="investment", subtype="ISA")
    nw.set_valuation(db, OWNER, "asset", isa.id, value="10000", on="2026-01-31")
    nw.set_valuation(db, OWNER, "asset", isa.id, value="12500", on="2026-06-30")
    current = fin.create_account(db, OWNER, name="Current", opening_balance="100")
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-07-15", amount="50", direction="in")
    assert nw.net_worth(db, OWNER)["net_worth_pence"] == 1250000 + 15000
    march = nw.net_worth(db, OWNER, as_of=date(2026, 3, 1))
    assert march["net_worth_pence"] == 1000000 + 10000
    assert nw.net_worth(db, OWNER, as_of=date(2025, 12, 31))["net_worth_pence"] == 10000
    # Same day replaces rather than duplicates.
    nw.set_valuation(db, OWNER, "asset", isa.id, value="12600", on="2026-06-30")
    assert [v["value_pence"] for v in nw.list_valuations(db, OWNER, "asset", isa.id)] == [1260000, 1000000]
    assert isa.subtype == "isa"


def test_contributions_and_gain(db):
    current = fin.create_account(db, OWNER, name="Current", opening_balance="5000")
    pension = nw.create_item(db, OWNER, "asset", name="SIPP", asset_type="pension", value="11000")
    nw.add_contribution(db, OWNER, pension.id, amount="2000", on="2026-04-01", note="Employer")
    tx = fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-01", amount="500",
                                direction="out", description="SIPP top-up")
    nw.link_transaction_contribution(db, OWNER, tx.id, pension.id)
    item = nw.list_items(db, OWNER, "asset")[0]
    assert item["contributions_pence"] == 250000 and item["gain_pence"] == 1100000 - 250000
    listed = fin.list_transactions(db, OWNER)
    assert listed[0]["contribution_asset_id"] == pension.id

    fin.update_transaction(db, OWNER, tx.id, amount="600")  # contribution follows the edit
    assert nw.contributions_total(db, OWNER, pension.id) == 260000
    with pytest.raises(fin.FinanceConflict):
        nw.delete_contribution(db, OWNER, nw.list_contributions(db, OWNER, pension.id)[0]["id"])
    fin.delete_transaction(db, OWNER, tx.id)  # and is removed with it
    db.expire_all()
    assert nw.contributions_total(db, OWNER, pension.id) == 200000
    assert db.query(FinanceContribution).count() == 1


def test_contribution_rules(db):
    current = fin.create_account(db, OWNER, name="Current")
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle")
    isa = nw.create_item(db, OWNER, "asset", name="ISA", asset_type="investment")
    income = fin.create_transaction(db, OWNER, account_id=current.id, date="2026-10-01", amount="5", direction="in")
    with pytest.raises(fin.FinanceError):
        nw.add_contribution(db, OWNER, car.id, amount="5")
    with pytest.raises(fin.FinanceError):
        nw.link_transaction_contribution(db, OWNER, income.id, isa.id)


def test_archive_and_delete_rules(db):
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property")
    empty = nw.create_item(db, OWNER, "asset", name="Spare", asset_type="other")
    valued = nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan", value="100")
    nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage", linked_asset_id=house.id)
    with pytest.raises(fin.FinanceConflict):
        nw.delete_item(db, OWNER, "liability", valued.id)
    with pytest.raises(fin.FinanceConflict):
        nw.delete_item(db, OWNER, "asset", house.id)  # a mortgage is linked to it
    nw.delete_item(db, OWNER, "asset", empty.id)
    nw.update_item(db, OWNER, "liability", valued.id, archived=True)
    assert [i["name"] for i in nw.list_items(db, OWNER, "liability")] == ["Mortgage"]


def test_only_mortgages_link_to_property(db):
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property")
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle")
    with pytest.raises(fin.FinanceError):
        nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan", linked_asset_id=house.id)
    with pytest.raises(fin.FinanceError):
        nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage", linked_asset_id=car.id)


def test_validation_and_owner_scoping(db):
    with pytest.raises(fin.FinanceError):
        nw.create_item(db, OWNER, "asset", name="Boat", asset_type="yacht")
    assert nw.create_item(db, OWNER, "asset", name="Bitcoin", asset_type="crypto").asset_type == "investment"
    assert nw.create_item(db, OWNER, "liability", name="Klarna", liability_type="Klarna").liability_type == "bnpl"
    loan = nw.create_item(db, OWNER, "liability", name="Loan", liability_type="loan")
    with pytest.raises(fin.FinanceError):
        nw.set_valuation(db, OWNER, "liability", loan.id, value="-5")
    with pytest.raises(fin.FinanceNotFound):
        nw.set_valuation(db, "bob", "liability", loan.id, value="5")
    assert nw.net_worth(db, "bob")["net_worth_pence"] == 0
