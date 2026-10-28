"""Projection and salary agent tools and their routing (made-up figures).

The text the model sees must carry the engine's and payroll's numbers exactly.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.tools.finance as tools
import src.tools.finance_projection as ptools
from src.action_intents import classify_tool_intent
from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.finance import loans
from src.finance import networth as nw
from src.finance import payroll as pr
from src.finance import payroll_jobs as pj
from src.finance import projections as proj
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import ensure_tables
from src.finance.money import format_pence
from src.finance.routing import FINANCE_TOOLS, finance_tool_focus
from src.tool_capabilities import ToolEffect, ToolRunSecurityContext, capabilities_for_action
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.turn_contract import requested_capabilities

OWNER = "alice"
TODAY = date(2026, 10, 28)


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    make = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        db = make()
        try:
            yield db
        finally:
            db.close()

    for module in (tools, ptools):
        monkeypatch.setattr(module, "_session", _session)
        monkeypatch.setattr(module, "_today", lambda: TODAY)
    for module in (fin, loans, pj, proj, rec):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    yield make
    engine.dispose()


def run(fn, args, owner=OWNER):
    return asyncio.run(fn(json.dumps(args), owner=owner))


@pytest.fixture
def household(factory):
    db = factory()
    cur = fin.create_account(db, OWNER, name="Current", account_type="current", opening_balance="3318")
    house = nw.create_item(db, OWNER, "asset", name="House", asset_type="property", value="147500",
                           value_date="2026-10-01")
    car = nw.create_item(db, OWNER, "asset", name="Car", asset_type="vehicle", value="12000", value_date="2026-10-01")
    pension = nw.create_item(db, OWNER, "asset", name="Pension", asset_type="pension", value="10000",
                             value_date="2026-10-01")
    mortgage = nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage",
                              linked_asset_id=house.id)
    loans.set_schedule(db, OWNER, mortgage.id, start_date="2026-10-15", start_balance="122000", apr="5.4",
                       term_months=300, payment_day=15)
    pcp = nw.create_item(db, OWNER, "liability", name="Car PCP", liability_type="loan", subtype="pcp")
    loans.set_schedule(db, OWNER, pcp.id, start_date="2026-10-01", start_balance="9000", apr="0", term_months=24,
                       payment_day=1, balloon="5000")
    job = pj.create_job(db, OWNER, employer="Acme", pay_anchor="2026-10-25", salary="40000", tax_code="1257L",
                        account_id=cur.id, pension_method="salary_sacrifice", pension_employee_percent="5",
                        pension_employer_percent="3", pension_asset_id=pension.id)
    db.commit()
    ids = {"current": cur.id, "house": house.id, "car": car.id, "pension": pension.id, "mortgage": mortgage.id,
           "pcp": pcp.id, "job": job.id}
    db.close()
    return ids


# ── project_finances ───────────────────────────────────────────────────


def test_projection_text_matches_the_engine_exactly(factory, household):
    result = run(ptools.do_project_finances, {"years": 5})
    assert result["exit_code"] == 0
    text = result["response"]
    db = factory()
    engine = proj.projection(db, OWNER, years=5)["base"]
    db.close()
    for y in engine["years"]:
        row = (f"{y['label']} ({y['end_label']}) | {format_pence(y['net_worth_pence'])} | {format_pence(y['cash_pence'])} | "
               f"{format_pence(y['shortfall_pence'])} | {format_pence(y['assets_pence'])} | "
               f"{format_pence(y['liabilities_pence'])}")
        assert row in text
    for e in engine["events"]:
        assert e["text"] in text
    for a in engine["assumptions"]:
        assert f"- {a['label']}: {a['value']}" in text
    assert ptools.QUOTE_RULE in text
    assert [y["net_worth_pence"] for y in result["data"]["base"]] == [y["net_worth_pence"] for y in engine["years"]]
    assert len(json.dumps(result)) < 8000  # small enough for a 16k-context local model
    assert "Car PCP: final (balloon) payment" in text


def test_debt_free_line(household):
    short = run(ptools.do_project_finances, {"years": 2})["response"]
    assert "Debts still owed at" in short and "Mortgage £" in short and "Not debt free within this period" in short
    long = run(ptools.do_project_finances, {"years": 30})["response"]
    assert "Debt free: every debt is cleared by" in long


def test_mortgage_in_2030(factory, household):
    result = run(ptools.do_project_finances, {"item": "mortgage", "at": "2030"})
    db = factory()
    out = proj.projection(db, OWNER, years=5)["base"]
    db.close()
    found = proj.value_at(out, household["mortgage"], date(2030, 1, 1))
    assert f"Mortgage balance {format_pence(found['value_pence'])}" in result["response"]
    assert "At Oct 2030" in result["response"]  # the year row ending in 2030


def test_what_if_extra_income_and_overpay(factory, household):
    result = run(ptools.do_project_finances, {"years": 3, "extra_monthly_income": "1000"})
    text = result["response"]
    assert "Scenario 'What if': £1,000.00 a month more income from now." in text
    db = factory()
    out = proj.projection(db, OWNER, years=3, changes=[{"type": "extra_income", "amount": "1000"}])
    db.close()
    b, s = out["base"]["years"][-1], out["scenarios"][0]["years"][-1]
    assert (f"{b['label']} ({b['end_label']}) | {format_pence(b['net_worth_pence'])} | {format_pence(s['net_worth_pence'])} | "
            f"+{format_pence(s['net_worth_pence'] - b['net_worth_pence'])}") in text
    over = run(ptools.do_project_finances, {"years": 30, "overpay": "300", "item": "mortgage"})
    assert over["exit_code"] == 0 and "Overpay Mortgage by £300.00 a month" in over["response"]


def test_balloon_choices_and_salary_what_if(household):
    back = run(ptools.do_project_finances, {"years": 3, "balloon_choice": "hand_back"})
    assert "Hand the car back at Car PCP's balloon" in back["response"]
    assert "car handed back" in back["response"]
    raise_ = run(ptools.do_project_finances, {"years": 2, "pay_rise": "3000"})
    assert "£3,000 a year pay rise at Acme" in raise_["response"]
    assert "Income per tax year" in raise_["response"]


def test_saved_scenarios_through_the_tools(household):
    saved = run(ptools.do_manage_projection_scenario, {"action": "save", "name": "Sell the car", "changes": [
        {"type": "sell", "asset": "car", "on": "2027-06-01", "clears": "car pcp"}]})
    assert saved["exit_code"] == 0 and "Sell Car in Jun 2027, clearing Car PCP" in saved["response"]
    listed = run(ptools.do_manage_projection_scenario, {})
    assert "Sell the car" in listed["response"]
    shown = run(ptools.do_project_finances, {"scenario": "sell car", "years": 2})
    assert "Scenario 'Sell the car'" in shown["response"] and "Car sold for" in shown["response"]
    unknown = run(ptools.do_project_finances, {"scenario": "move to Spain"})
    assert unknown["exit_code"] == 1 and "Sell the car" in unknown["error"]
    bad = run(ptools.do_manage_projection_scenario, {"action": "save", "name": "X", "changes": [{"type": "sell", "asset": "boat"}]})
    assert bad["exit_code"] == 1 and "Nothing saved" in bad["error"]
    gone = run(ptools.do_manage_projection_scenario, {"action": "delete", "name": "sell the car"})
    assert gone["exit_code"] == 0


# ── query_salary / update_salary ───────────────────────────────────────


def test_salary_text_matches_payroll_exactly(factory, household):
    result = run(ptools.do_query_salary, {})
    text = result["response"]
    db = factory()
    job = pj.snapshot(db, OWNER, pj.get_job(db, OWNER, household["job"]))
    db.close()
    period = pr.next_period(job, TODAY)
    summary = pr.annual_summary(job, 2026)
    assert f"net pay {format_pence(period['net_pence'])}" in text
    assert f"income tax -{format_pence(period['tax_pence'])}" in text
    assert f"NI -{format_pence(period['ni_pence'])}" in text
    assert f"take-home {format_pence(summary['net_pence'])}" in text
    assert f"{summary['marginal_rate_percent']}% with NI" in text
    assert "You get £12,570 of tax-free pay a year" in text
    assert ptools.SALARY_RULE in text
    assert "No payslip entered yet" in text


def test_salary_what_if_3k_raise_and_pension(factory, household):
    result = run(ptools.do_query_salary, {"pay_rise": "3000"})
    db = factory()
    job = pj.snapshot(db, OWNER, pj.get_job(db, OWNER, household["job"]))
    db.close()
    before = pr.annual_summary(job, 2026)
    after = pr.annual_summary(pr.with_changes(job, [pr.JobChange("salary", date(2026, 4, 6), amount=4_300_000)]), 2026)
    diff = after["net_pence"] - before["net_pence"]
    assert f"Take-home for the year {format_pence(after['net_pence'])} instead of {format_pence(before['net_pence'])} " \
           f"(+{format_pence(diff)}" in result["response"]
    pension = run(ptools.do_query_salary, {"pension_percent": "8"})
    assert "pension at 8%" in pension["response"] and "Into the pension:" in pension["response"]
    code = run(ptools.do_query_salary, {"tax_code": "K100"})
    assert "tax code K100" in code["response"]


def test_salary_without_a_job(factory):
    result = run(ptools.do_query_salary, {})
    assert result["exit_code"] == 1 and "Finance > Salary" in result["error"]


def test_update_salary_reports_what_changed(factory, household):
    result = run(ptools.do_update_salary, {"action": "pay_rise", "percent": "5", "from": "2027-04-01", "repeat": "yearly"})
    assert result["exit_code"] == 0
    text = result["response"]
    assert "Pay rise of 5% from 01 Apr 2027 every year" in text
    assert "Recurring income: the calculated pay item now expects" in text
    assert "5-year projection: net worth" in text
    db = factory()
    assert [c.kind for c in pj._changes(db, OWNER, household["job"])] == ["raise"]
    db.close()
    code = run(ptools.do_update_salary, {"action": "tax_code", "tax_code": "1100L"})
    assert "Tax code set to 1100L" in code["response"]
    bad = run(ptools.do_update_salary, {"action": "tax_code", "tax_code": "ZZZ"})
    assert bad["exit_code"] == 1 and "Nothing saved" in bad["error"]
    pension = run(ptools.do_update_salary, {"action": "pension", "percent": "8"})
    assert "you 8%" in pension["response"]
    db = factory()
    item = db.query(rec.FinanceRecurring).filter(rec.FinanceRecurring.job_id == household["job"]).one()
    expected = pr.next_period(pj.snapshot(db, OWNER, pj.get_job(db, OWNER, household["job"])), TODAY)["net_pence"]
    assert item.amount_pence == expected
    db.close()


# ── Registration, approval and routing ─────────────────────────────────


def test_tools_are_registered_with_the_right_effects():
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert {"project_finances", "manage_projection_scenario", "query_salary", "update_salary"} <= names <= names
    assert {"project_finances", "manage_projection_scenario", "query_salary", "update_salary"} <= FINANCE_TOOLS
    for tool in ("project_finances", "query_salary"):
        assert capabilities_for_action(tool, "{}").effects == {ToolEffect.READ_PRIVATE}
    assert capabilities_for_action("manage_projection_scenario", "{}").effects == {ToolEffect.READ_PRIVATE}
    assert ToolEffect.WRITE_PRIVATE in capabilities_for_action("manage_projection_scenario", '{"action": "save"}').effects
    assert ToolEffect.WRITE_PRIVATE in capabilities_for_action("update_salary", '{"action": "pay_rise"}').effects
    tainted = ToolRunSecurityContext()
    tainted.external_untrusted_context_seen = True
    assert not tainted.decision_for("update_salary", '{"action": "pay_rise"}').allowed
    block = function_call_to_tool_block("project_finances", '{"years": 5}')
    assert block.tool_type == "project_finances"


def _permits(request, tool):
    return create_request_authority(request).permits(ExactOperation.normalize(tool, "{}"))


@pytest.mark.parametrize("text", [
    "project my net worth over 5 years",
    "what will my mortgage balance be in 2030",
    "what if I earned £1,000 more a month",
    "when will I be debt free",
    "project my net worth over the next 5 years",
    "can I afford a new car next year?",
    "what if I hand the car back at the balloon",
    "save a scenario called Sell the car where I sell the car in June 2027 and the money clears the car PCP",
    "compare my scenarios",
])
def test_projection_questions_select_the_projection_tool(text):
    assert finance_tool_focus(text) == "project_finances"
    assert classify_tool_intent(text).category == "finance"
    assert "finance" in requested_capabilities(text)
    assert _permits(text, "project_finances")


@pytest.mark.parametrize("text", [
    "what's my take-home pay",
    "is my tax code right",
    "what if I sacrifice 8% into my pension",
    "how much would a £3,000 pay rise change my take-home?",
    "should I up my pension to 8%?",
    "check my payslip",
    "how much national insurance do I pay",
])
def test_salary_questions_select_the_salary_tool(text):
    assert finance_tool_focus(text) == "query_salary"
    assert classify_tool_intent(text).category == "finance"
    assert "finance" in requested_capabilities(text)
    assert _permits(text, "query_salary")


@pytest.mark.parametrize("text", [
    "show me the email with my payslip",
    "find the email from HR about my salary",
    "summarise my unread emails",
])
def test_email_about_pay_stays_with_email(text):
    assert classify_tool_intent(text).category == "email"
    assert "finance" not in requested_capabilities(text)


def test_keyword_routers_offer_the_specific_tools():
    from src.agent_loop import _qwen38_router_tool_names

    assert {"project_finances"} <= _qwen38_router_tool_names("project my net worth over 5 years")
    assert {"query_salary"} <= _qwen38_router_tool_names("what's my take-home pay")
    assert "query_salary" in _qwen38_router_tool_names("is my tax code right")
