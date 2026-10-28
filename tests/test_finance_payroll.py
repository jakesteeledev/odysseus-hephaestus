"""UK payroll: tax codes, PAYE, NI, student loans, pensions, payslip checks,
and everything that updates when the salary setup changes.

All figures are made up. Each calculation test shows the working.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.finance import loans
from src.finance import networth as nw
from src.finance import payroll as pr
from src.finance import payroll_jobs as pj
from src.finance import projections as proj
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance import uk_tax_rules as rules
from src.finance.models import FinanceContribution, FinanceRepayment, ensure_tables

OWNER = "alice"
TODAY = date(2026, 4, 10)


def job(**kw):
    base = dict(id="j", employer="Acme", frequency="monthly", pay_anchor=date(2026, 4, 25), salary=3_000_000,
                tax_code="1257L")
    base.update(kw)
    return pr.Job(**base)


def year(j, y=2026):
    return pr.calc_tax_year(j, y)


# ── Rules file ──────────────────────────────────────────────────────────


def test_every_rule_section_has_a_source_and_check_date():
    seen = 0
    for _year, name, section in rules.sections():
        if name in ("label",):
            continue
        assert section["source"].startswith("https://"), name
        assert date.fromisoformat(section["checked"]), name
        seen += 1
    assert seen >= 11
    assert set(rules.RULES) == {2025, 2026}


def test_future_years_carry_forward_and_the_2029_cap_applies():
    later = rules.rules_for(2028)
    assert later["assumed"] and later["based_on"] == "2026/27" and later["label"] == "2028/29"
    assert later["salary_sacrifice_cap"] is None
    assert rules.rules_for(2029)["salary_sacrifice_cap"] == 200_000
    assert not rules.rules_for(date(2026, 6, 1))["assumed"]
    assert rules.rules_for(date(2026, 4, 5))["label"] == "2025/26"


# ── Tax codes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,region,kind,cumulative", [
    ("1257L", "ruk", "allowance", True), ("1257M", "ruk", "allowance", True), ("1257N", "ruk", "allowance", True),
    ("S1257L", "scotland", "allowance", True), ("C1257L", "wales", "allowance", True), ("K475", "ruk", "k", True),
    ("SK100", "scotland", "k", True), ("BR", "ruk", "flat", True), ("D0", "ruk", "flat", True),
    ("D1", "ruk", "flat", True), ("SD3", "scotland", "flat", True), ("NT", "ruk", "nt", True),
    ("0T", "ruk", "allowance", True), ("1257L W1", "ruk", "allowance", False),
    ("1257L M1", "ruk", "allowance", False), ("1257L X", "ruk", "allowance", False),
    ("s1257l/m1", "scotland", "allowance", False), ("1100T", "ruk", "allowance", True),
])
def test_valid_tax_codes(text, region, kind, cumulative):
    code = pr.parse_tax_code(text)
    assert (code.region, code.kind, code.cumulative) == (region, kind, cumulative)
    assert code.explanation


@pytest.mark.parametrize("text,hint", [
    ("", "enter your tax code"), ("1257", "letter at the end"), ("12-57L", "only contain"),
    ("D2", "only used with Scottish"), ("M1", "goes after the code"), ("1257Q", "isn't a valid"),
])
def test_invalid_tax_codes_explain_why(text, hint):
    with pytest.raises(pr.TaxCodeError) as exc:
        pr.parse_tax_code(text)
    assert hint in str(exc.value)


def test_unusual_allowance_is_explained():
    assert any("lower than the standard" in n for n in pr.parse_tax_code("1100L").notes)
    assert any("Emergency" in n for n in pr.parse_tax_code("1257L W1").notes)


# ── £30,000, 1257L, monthly, England ────────────────────────────────────


def test_30k_england_april_and_full_year():
    # April: gross £30,000 / 12 = £2,500.00.
    # Free pay month 1 = (1257 x 10 + 9) / 12 = £1,048.25.
    # Taxable = £2,500 - £1,048.25 = £1,451.75 -> £1,451 (rounded down) x 20% = £290.20.
    # NI = (£2,500 - £1,048 PT) x 8% = £1,452 x 8% = £116.16.
    # Net = £2,500 - £290.20 - £116.16 = £2,093.64.
    periods = year(job())
    april = periods[0]
    assert april["date"] == "2026-04-25" and april["tax_period"] == 1
    assert (april["gross_pence"], april["tax_pence"], april["ni_pence"], april["net_pence"]) == \
        (250_000, 29_020, 11_616, 209_364)
    # Year: taxable £30,000 - £12,579 = £17,421 x 20% = £3,484.20 (cumulative, so pennies even out).
    # NI £116.16 x 12 = £1,393.92. Net = £30,000 - £3,484.20 - £1,393.92 = £25,121.88.
    ytd = periods[-1]["ytd"]
    assert len(periods) == 12
    assert (ytd["tax_pence"], ytd["ni_pence"], ytd["net_pence"]) == (348_420, 139_392, 2_512_188)


def test_30k_scotland():
    # Month 1 taxable £1,451. Limits for one month, rounded up to the pound:
    # starter £3,967/12 -> £331, basic £16,956/12 -> £1,413.
    # £331 x 19% = £62.89; (£1,413 - £331) = £1,082 x 20% = £216.40; (£1,451 - £1,413) = £38 x 21% = £7.98.
    # Total £287.27.
    # Year: £3,967 x 19% = £753.73; £12,989 x 20% = £2,597.80; (£17,421 - £16,956) = £465 x 21% = £97.65
    # = £3,449.18.
    periods = year(job(tax_code="S1257L"))
    assert periods[0]["region"] == "scotland"
    assert periods[0]["tax_pence"] == 28_727
    assert periods[-1]["ytd"]["tax_pence"] == 344_918


def test_region_override_beats_the_prefix():
    assert year(job(tax_code="1257L", region="scotland"))[0]["tax_pence"] == 28_727


def test_110k_taper_and_60_percent_marginal_rate():
    # Adjusted net income £110,000: allowance falls by (£110,000 - £100,000) / 2 = £5,000 to £7,570.
    # Taxable £102,430: £37,700 x 20% = £7,540 + £64,730 x 40% = £25,892 = £33,432 owed.
    # The next £100 loses £50 of allowance, so £150 more is taxed at 40% = £60: a 60% rate.
    # (The summary measures it on £156, which divides evenly into every pay frequency.)
    # With 2% NI above the upper limit, 62% in total.
    s = pr.annual_summary(job(salary=11_000_000), 2026)
    assert s["personal_allowance_pence"] == 757_000
    assert s["tax_owed_pence"] == 3_343_200
    assert s["marginal_tax_percent"] == 60.0 and s["marginal_rate_percent"] == 62.0
    assert s["taper_band"]
    # PAYE on 1257L doesn't know about the taper and under-collects.
    assert s["paye_difference_pence"] == s["tax_owed_pence"] - s["tax_pence"] > 0


def test_basic_rate_marginal_and_effective_rates():
    s = pr.annual_summary(job(), 2026)
    assert s["marginal_tax_percent"] == 20.0 and s["marginal_rate_percent"] == 28.0
    assert s["effective_rate_percent"] == round((348_420 + 139_392) / 3_000_000 * 100, 1)
    assert s["take_home_monthly_pence"] == round(2_512_188 / 12)


# ── Pensions ────────────────────────────────────────────────────────────


def test_pension_methods_5_percent_on_qualifying_earnings():
    # Pensionable = £2,500 - £520 (lower limit) = £1,980; 5% = £99.00; employer 3% = £59.40.
    common = dict(pension_employee_bp=500, pension_employer_bp=300)
    ss = year(job(pension_method="salary_sacrifice", **common))[0]
    # Sacrifice: pay £2,401 for tax and NI. Taxable £2,401 - £1,048.25 = £1,352.75 -> £1,352 x 20% = £270.40.
    # NI (£2,401 - £1,048) x 8% = £108.24. Net £2,401 - £270.40 - £108.24 = £2,022.36.
    assert (ss["taxable_pence"], ss["ni_pay_pence"], ss["tax_pence"], ss["ni_pence"], ss["net_pence"]) == \
        (240_100, 240_100, 27_040, 10_824, 202_236)
    npa = year(job(pension_method="net_pay", **common))[0]
    # Net pay: taxable £2,401 (tax £270.40) but NI on the full £2,500 (£116.16).
    # Net £2,500 - £99 - £270.40 - £116.16 = £2,014.44.
    assert (npa["taxable_pence"], npa["ni_pay_pence"], npa["tax_pence"], npa["ni_pence"], npa["net_pence"]) == \
        (240_100, 250_000, 27_040, 11_616, 201_444)
    ras = year(job(pension_method="relief_at_source", **common))[0]
    # Relief at source: tax and NI on £2,500 (£290.20, £116.16); you pay 80% of £99 = £79.20 from net pay,
    # the provider adds £19.80. Net £2,500 - £290.20 - £116.16 - £79.20 = £2,014.44.
    assert (ras["taxable_pence"], ras["tax_pence"], ras["ni_pence"], ras["net_pence"]) == \
        (250_000, 29_020, 11_616, 201_444)
    assert (ras["pension_deduction_pence"], ras["pension_relief_pence"]) == (7_920, 1_980)
    for p in (ss, npa, ras):
        assert (p["pension_employee_pence"], p["pension_employer_pence"]) == (9_900, 5_940)


def test_relief_at_source_higher_rate_relief_is_claimable_not_added():
    # £70,000, 5% of full salary = £3,500 gross a year (£291.67 a month, so £3,500.04 over the year).
    # Relief at source extends the basic band by that, so it moves from 40% to 20%: about £700 claimable
    # through self assessment.
    s = pr.annual_summary(job(salary=7_000_000, pension_method="relief_at_source", pension_employee_bp=500,
                              pension_basis="full"), 2026)
    assert abs(s["pension_relief_claimable_pence"] - 70_000) <= 1
    # The basic 20% the provider adds: £291.67 - 80% (£233.34) = £58.33 a month.
    assert s["pension_relief_pence"] == 12 * 5_833


def test_pension_cost_after_relief():
    # Salary sacrifice of £99 a month costs less than £99 of take-home: tax and NI relief.
    s = pr.annual_summary(job(pension_method="salary_sacrifice", pension_employee_bp=500,
                              pension_employer_bp=300), 2026)
    assert s["into_pension_pence"] == 12 * (9_900 + 5_940)
    # Each month: £99 less pay saves £19.80 tax + £7.92 NI, so it costs £71.28 (the tax may differ by pennies).
    assert abs(s["pension_cost_pence"] - 12 * 7_128) <= 12


def test_2029_salary_sacrifice_cap():
    # £40,000 sacrificing 7.5% of full salary = £250 a month = £3,000 in 2029/30.
    # The first £2,000 (eight months) is NI-free; the last £1,000 is added back to NI pay.
    j = job(salary=4_000_000, pension_method="salary_sacrifice", pension_employee_bp=750, pension_basis="full",
            pay_anchor=date(2029, 4, 25))
    periods = year(j, 2029)
    assert periods[-1]["ytd"]["pension_sacrifice_pence"] == 300_000
    assert sum(p["ni_sacrifice_added_back_pence"] for p in periods) == 100_000
    assert [p["ni_sacrifice_added_back_pence"] for p in periods] == [0] * 8 + [25_000] * 4
    # So NI is £1,000 x 8% = £80 more than with an uncapped sacrifice, and income tax is unchanged.
    uncapped = year(j, 2028)
    assert periods[-1]["ytd"]["ni_pence"] - uncapped[-1]["ytd"]["ni_pence"] == 8_000
    assert periods[-1]["ytd"]["tax_pence"] == uncapped[-1]["ytd"]["tax_pence"]


def test_employer_ni_passback():
    # 50% of the employer's 15% NI saving on a £99 sacrifice (£14.85) = £7.43 extra into the pension.
    p = year(job(pension_method="salary_sacrifice", pension_employee_bp=500, pension_employer_bp=300,
                 ni_passback_bp=5000))[0]
    assert p["pension_employer_pence"] == 5_940 + 743


# ── Other tax codes ─────────────────────────────────────────────────────


def test_k_code_and_regulatory_limit():
    # K475 on £2,500 a month: add (475 x 10 + 9) / 12 = £396.59 -> taxable £2,896 x 20% = £579.20.
    assert year(job(tax_code="K475"))[0]["tax_pence"] == 57_920
    # K1000 on £500 a month: taxable £500 + £834.09 = £1,334 x 20% = £266.80, but tax can't exceed
    # 50% of the £500 pay: £250.
    low = year(job(tax_code="K1000", salary=600_000))[0]
    assert low["tax_pence"] == 25_000


def test_br_0t_and_m1_against_cumulative():
    # BR: £2,500 x 20% = £500 a month. 0T: no allowance; month 1 basic band £37,700/12 -> £3,142, so also £500.
    assert year(job(tax_code="BR"))[0]["tax_pence"] == 50_000
    assert year(job(tax_code="0T"))[-1]["ytd"]["tax_pence"] == 600_000
    # D0: everything at 40%.
    assert year(job(tax_code="D0"))[0]["tax_pence"] == 100_000
    # NT: no tax.
    assert year(job(tax_code="NT"))[-1]["ytd"]["tax_pence"] == 0
    # 1257L M1: every month is month 1 (£290.20), so £3,482.40 a year against £3,484.20 cumulative.
    m1 = year(job(tax_code="1257L M1"))
    assert {p["tax_pence"] for p in m1} == {29_020}
    assert m1[-1]["ytd"]["tax_pence"] == 348_240


def test_cumulative_tax_refunds_after_a_mid_year_code_change():
    # BR for April-June then 1257L from July: cumulative tax catches up with the allowance and refunds.
    j = job(changes=[pr.JobChange("tax_code", date(2026, 4, 6), label="BR"),
                     pr.JobChange("tax_code", date(2026, 7, 1), label="1257L")])
    periods = year(j)
    assert [p["tax_pence"] for p in periods[:3]] == [50_000] * 3
    assert periods[3]["tax_pence"] < 0  # a refund in July
    assert periods[-1]["ytd"]["tax_pence"] == 348_420


def test_week_one_basis_for_weekly_pay():
    # Weekly £600 on 1257L W1: free pay £12,579 / 52 = £241.91 -> taxable £358 x 20% = £71.60.
    # NI (£600 - £242) x 8% = £28.64.
    w = year(job(frequency="weekly", salary=3_120_000, tax_code="1257L W1", pay_anchor=date(2026, 4, 10)))
    assert w[0]["tax_pence"] == 7_160 and w[0]["ni_pence"] == 2_864
    assert len(w) == 52


def test_fortnightly_and_four_weekly_ni_thresholds():
    # Fortnightly £1,200: NI (£1,200 - 2 x £242) x 8% = £57.28.
    f = year(job(frequency="fortnightly", salary=3_120_000, pay_anchor=date(2026, 4, 10)))
    assert f[0]["gross_pence"] == 120_000 and f[0]["ni_pence"] == 5_728
    assert len(f) == 26
    # Four-weekly £2,400: (£2,400 - £968) x 8% = £114.56.
    four = year(job(frequency="four_weekly", salary=3_120_000, pay_anchor=date(2026, 4, 10)))
    assert four[0]["ni_pence"] == 11_456 and len(four) == 13


def test_ni_categories():
    assert year(job(ni_category="C"))[0]["ni_pence"] == 0
    # Category J: 2% from PT to UEL: £1,452 x 2% = £29.04.
    assert year(job(ni_category="J"))[0]["ni_pence"] == 2_904


# ── Student loans ───────────────────────────────────────────────────────


def test_plan_2_and_postgraduate_together():
    # £40,000 a month = £3,333.33.
    # Plan 2: threshold £29,385/12 = £2,448.75; (£3,333.33 - £2,448.75) x 9% = £79.61 -> £79.
    # Postgraduate: (£3,333.33 - £1,750) x 6% = £94.99 -> £94.
    p = year(job(salary=4_000_000, student_plans=("plan_2",), postgraduate=True))[0]
    assert p["student_loans"] == {"plan_2": 7_900, "postgraduate": 9_400}
    assert p["net_pence"] == 333_333 - p["tax_pence"] - p["ni_pence"] - 7_900 - 9_400


def test_two_undergraduate_plans_take_nine_percent_once():
    # Plan 1 (£26,900 -> £2,241.66 a month) and Plan 2 (£2,448.75) on £3,333.33:
    # total (£3,333.33 - £2,241.66) x 9% = £98.25 -> £98; Plan 2's share (£3,333.33 - £2,448.75) x 9% -> £79;
    # Plan 1 gets the remaining £19.
    p = year(job(salary=4_000_000, student_plans=("plan_1", "plan_2")))[0]
    assert p["student_loans"] == {"plan_1": 1_900, "plan_2": 7_900}


def test_student_loans_use_ni_pay_after_salary_sacrifice():
    plain = year(job(salary=4_000_000, student_plans=("plan_2",)))[0]["student_loan_pence"]
    sacrificed = year(job(salary=4_000_000, student_plans=("plan_2",), pension_method="salary_sacrifice",
                          pension_employee_bp=500, pension_basis="full"))[0]["student_loan_pence"]
    assert sacrificed < plain


# ── Pay rises and bonuses ───────────────────────────────────────────────


def test_pay_rise_mid_tax_year_and_bonus_month():
    # 10% rise from 1 October: £2,500 -> £2,750 from the October pay. A £1,000 bonus on 15 December
    # lands in the 25 December pay.
    j = job(changes=[pr.JobChange("raise", date(2026, 10, 1), percent_bp=1000),
                     pr.JobChange("bonus", date(2026, 12, 15), amount=100_000)])
    periods = year(j)
    gross = [p["gross_pence"] for p in periods]
    assert gross[:6] == [250_000] * 6
    assert gross[6:8] == [275_000] * 2
    assert gross[8] == 375_000 and periods[8]["bonus_pence"] == 100_000
    assert gross[9:] == [275_000] * 3
    # The bonus month pays more tax and NI than the months around it.
    assert periods[8]["tax_pence"] > periods[7]["tax_pence"] and periods[8]["ni_pence"] > periods[7]["ni_pence"]
    assert periods[-1]["ytd"]["gross_pence"] == sum(gross)


def test_yearly_pay_rise_compounds():
    j = job(changes=[pr.JobChange("raise", date(2026, 4, 1), percent_bp=300, repeat="yearly")])
    assert pr.salary_at(j, date(2026, 5, 1)) == 3_090_000
    assert pr.salary_at(j, date(2027, 5, 1)) == 3_182_700


def test_other_sacrifice_scheme_and_allowance():
    # A £100 a month cycle-to-work scheme comes off pay before tax and NI; a £200 allowance is added.
    j = job(changes=[pr.JobChange("sacrifice", date(2026, 4, 1), amount=10_000, label="Cycle to work"),
                     pr.JobChange("allowance", date(2026, 4, 1), amount=20_000, label="Shift allowance")])
    p = year(j)[0]
    assert p["gross_pence"] == 270_000 and p["taxable_pence"] == 260_000 and p["ni_pay_pence"] == 260_000


def test_reduced_hours_and_stopping_work():
    j = job(changes=[pr.JobChange("hours", date(2026, 9, 1), percent_bp=8000),
                     pr.JobChange("stop", date(2027, 1, 1))])
    periods = year(j)
    assert periods[5]["gross_pence"] == 200_000  # September at 80%
    assert periods[-1]["date"] == "2026-12-25"


# ── Payslip check ───────────────────────────────────────────────────────


def test_payslip_matching_and_differences():
    j = job()
    ok = pr.compare_payslip(j, {"date": "2026-04-25", "gross": 250_000, "tax": 29_020, "ni": 11_616,
                                "net": 209_364})
    assert ok["matches"] and not ok["causes"]
    emergency = pr.compare_payslip(j, {"date": "2026-05-25", "gross": 250_000, "tax": 29_020, "ni": 11_616})
    # Month 2 cumulative tax is £290.40; £290.20 is within £1, so still a match.
    assert emergency["matches"]
    wrong = pr.compare_payslip(j, {"date": "2026-04-25", "gross": 250_000, "tax": 50_000, "ni": 11_616,
                                   "net": 188_384})
    assert not wrong["matches"]
    assert {r["field"] for r in wrong["rows"] if r["flag"]} == {"tax", "net"}
    assert any("different tax code" in c for c in wrong["causes"])
    bonus = pr.compare_payslip(j, {"date": "2026-04-25", "gross": 300_000})
    assert any("back pay" in c for c in bonus["causes"])


def test_payslip_spots_an_emergency_code():
    # Month 3 on a W1/M1 code taxes £290.20 instead of the cumulative £290.40... too close to tell apart,
    # so use a pay rise month where they differ: after a rise cumulative tax catches up.
    j = job(changes=[pr.JobChange("raise", date(2026, 6, 1), percent_bp=2000)])
    calc = pr.period_on(j, date(2026, 6, 25))
    m1 = pr.period_on(pr.with_changes(j, [pr.JobChange("tax_code", date(2026, 4, 6), label="1257L M1")]),
                      date(2026, 6, 25))
    assert abs(calc["tax_pence"] - m1["tax_pence"]) <= 100  # equal pay months match closely
    j2 = job(changes=[pr.JobChange("tax_code", date(2026, 4, 6), label="BR"),
                      pr.JobChange("tax_code", date(2026, 6, 1), label="1257L")])
    real_m1 = pr.period_on(pr.with_changes(j2, [pr.JobChange("tax_code", date(2026, 6, 1), label="1257L M1")]),
                           date(2026, 6, 25))
    result = pr.compare_payslip(j2, {"date": "2026-06-25", "gross": 250_000, "tax": real_m1["tax_pence"]})
    assert not result["matches"]
    assert any("emergency" in c for c in result["causes"])


# ── Database: jobs, recurring pay, pension, student loan, projection ────


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    for module in (fin, loans, pj, proj, rec):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def setup(db):
    current = fin.create_account(db, OWNER, name="Current", account_type="current", opening_balance="1000")
    pension = nw.create_item(db, OWNER, "asset", name="Workplace pension", asset_type="pension", value="20000",
                             value_date="2026-04-01")
    student = nw.create_item(db, OWNER, "liability", name="Student loan", liability_type="student_loan",
                             value="30000", value_date="2026-04-01")
    row = pj.create_job(db, OWNER, employer="Acme Ltd", frequency="monthly", pay_anchor="2026-04-25",
                        salary="30000", tax_code="1257L", account_id=current.id, pension_method="salary_sacrifice",
                        pension_employee_percent="5", pension_employer_percent="3", pension_asset_id=pension.id,
                        student_plans=["plan_2"], sl_liability_id=student.id)
    return {"current": current, "pension": pension, "student": student, "job": row}


def _pay_item(db):
    return db.query(rec.FinanceRecurring).filter(rec.FinanceRecurring.job_id.isnot(None)).one()


def test_job_creates_a_calculated_pay_item(db, setup):
    item = _pay_item(db)
    april = pr.period_on(pj.snapshot(db, OWNER, setup["job"]), date(2026, 4, 25))
    assert item.name == "Acme Ltd pay" and item.direction == "in" and item.account_id == setup["current"].id
    assert item.amount_pence == april["net_pence"]
    with pytest.raises(fin.FinanceError, match="calculated"):
        rec.update_recurring(db, OWNER, item.id, amount="5")
    with pytest.raises(fin.FinanceError, match="calculated"):
        rec.delete_recurring(db, OWNER, item.id)
    up = rec.upcoming(db, OWNER, days=40)
    row = next(r for r in up["items"] if r["recurring_id"] == item.id)
    assert row["amount_pence"] == april["net_pence"] and row["calculated"]


def test_changing_the_salary_updates_pay_pension_and_projection(db, setup):
    before_item = _pay_item(db).amount_pence
    base = proj.projection(db, OWNER, years=1, today=TODAY)["base"]
    pj.update_job(db, OWNER, setup["job"].id, salary="36000")
    after_item = _pay_item(db).amount_pence
    assert after_item > before_item
    raised = proj.projection(db, OWNER, years=1, today=TODAY)["base"]
    pension_before = next(a for a in base["years"][0]["assets"] if a["id"] == setup["pension"].id)
    pension_after = next(a for a in raised["years"][0]["assets"] if a["id"] == setup["pension"].id)
    assert pension_after["contributions_pence"] > pension_before["contributions_pence"]
    assert raised["end"]["cash_pence"] > base["end"]["cash_pence"]
    income = raised["income_by_tax_year"]
    assert income[0]["tax_year"] == "2026/27" and income[0]["gross_pence"] == 12 * 300_000


def test_projection_pays_net_salary_pension_and_student_loan(db, setup):
    result = proj.projection(db, OWNER, years=2, today=TODAY)["base"]
    job = pj.snapshot(db, OWNER, setup["job"])
    first_year = [p for p in pr.calc_tax_year(job, 2026)]
    flows = result["points"][0]["flows"]
    assert flows["income"] == first_year[0]["net_pence"]
    assert flows["external_contributions"] == first_year[0]["pension_employee_pence"] + first_year[0]["pension_employer_pence"]
    assert flows["payroll_loan_repaid"] == first_year[0]["student_loan_pence"]
    years = result["income_by_tax_year"]
    assert [y["tax_year"] for y in years] == ["2026/27", "2027/28"]  # pay dates to March 2028
    assert years[1]["assumed"] and not years[0]["assumed"]
    assert any(a["key"] == "tax_rules" for a in result["assumptions"])
    assert not any(w["kind"] == "no_income" for w in result["warnings"])


def test_student_loan_deductions_stop_when_repaid(db, setup):
    nw.set_valuation(db, OWNER, "liability", setup["student"].id, value="100", on="2026-04-05")
    pj.update_job(db, OWNER, setup["job"].id, salary="45000")
    result = proj.projection(db, OWNER, years=1, today=TODAY)["base"]
    repaid = [p["flows"]["payroll_loan_repaid"] for p in result["points"]]
    assert sum(repaid) == 10_000 and repaid[-1] == 0
    assert any(e["kind"] == "paid_off" and e["item_id"] == setup["student"].id for e in result["events"])


def test_salary_scenarios_in_projections(db, setup):
    base = proj.projection(db, OWNER, years=1, today=TODAY)["base"]
    out = proj.projection(db, OWNER, years=1, today=TODAY, changes=[
        {"type": "salary", "action": "raise", "amount": "3000", "from": "2026-04-06"}])
    what_if = out["scenarios"][0]
    assert what_if["descriptions"] == ["£3,000 a year pay rise at Acme Ltd from Apr 2026"]
    assert what_if["end"]["cash_pence"] > base["end"]["cash_pence"]
    gross = what_if["income_by_tax_year"][0]["gross_pence"]
    assert gross == 3_300_000
    stopped = proj.projection(db, OWNER, years=1, today=TODAY, changes=[
        {"type": "salary", "action": "stop", "from": "2026-10-01"}])["scenarios"][0]
    assert stopped["income_by_tax_year"][0]["periods"] == 6  # April to September


def test_confirming_pay_records_pension_and_student_loan(db, setup):
    item = _pay_item(db)
    rec.confirm(db, OWNER, item.id, "2026-04-25")
    contributions = db.query(FinanceContribution).filter(FinanceContribution.asset_id == setup["pension"].id).all()
    repayments = db.query(FinanceRepayment).filter(FinanceRepayment.liability_id == setup["student"].id).all()
    april = pr.period_on(pj.snapshot(db, OWNER, setup["job"]), date(2026, 4, 25))
    assert [c.amount_pence for c in contributions] == [april["pension_employee_pence"] + april["pension_employer_pence"]]
    assert [r.amount_pence for r in repayments] == ([april["student_loan_pence"]] if april["student_loan_pence"] else [])
    rec.undo(db, OWNER, item.id, "2026-04-25")
    assert db.query(FinanceContribution).count() == 0
    assert db.query(FinanceRepayment).count() == 0


def test_imported_pay_matches_within_tolerance(db, setup):
    item = _pay_item(db)
    expected = item.amount_pence
    tx = fin.create_transaction(db, OWNER, account_id=setup["current"].id, date="2026-04-24",
                                amount=f"{(expected + 150) / 100:.2f}", direction="in",
                                description="ACME LTD SALARY")
    matches = rec.match_candidates(db, OWNER, item, date(2026, 4, 25))
    assert [m["id"] for m in matches] == [tx.id]


def test_payslip_check_and_warnings(db, setup):
    j = pj.snapshot(db, OWNER, setup["job"])
    april = pr.period_on(j, date(2026, 4, 25))
    slip = pj.add_payslip(db, OWNER, setup["job"].id, pay_date="2026-04-25", gross="2500",
                          tax=f"{april['tax_pence'] / 100 + 30:.2f}", ni=f"{april['ni_pence'] / 100:.2f}",
                          net=f"{april['net_pence'] / 100 - 30:.2f}", tax_code="1257L")
    result = pj.check_payslip(db, OWNER, slip.id)
    assert not result["matches"]
    assert [r["field"] for r in result["rows"] if r["flag"]] == ["tax", "net"]
    # A payslip's net pay replaces the calculated amount for that date.
    assert pj.net_pay_on(db, OWNER, setup["job"].id, date(2026, 4, 25)) == april["net_pence"] - 3_000
    # Duplicate manual salary and missing tax code warnings.
    manual = rec.create_recurring(db, OWNER, name="Salary", account_id=setup["current"].id, amount="2000",
                                  direction="in", next_due="2026-04-28")
    pj.update_job(db, OWNER, setup["job"].id, tax_code="")
    kinds = {w["kind"] for w in pj.salary_warnings(db, OWNER)}
    assert {"duplicate_salary", "no_tax_code"} <= kinds
    pj.replace_manual_salary(db, OWNER, manual.id)
    assert "duplicate_salary" not in {w["kind"] for w in pj.salary_warnings(db, OWNER)}


def test_more_warnings(db):
    current = fin.create_account(db, OWNER, name="Current", account_type="current")
    pj.create_job(db, OWNER, employer="Big Co", pay_anchor="2026-04-25", salary="98000",
                  tax_code="1257L M1", account_id=current.id, student_plans="plan 2",
                  pension_method="net_pay", pension_employee_percent="5", start_date="2026-01-01")
    fin.create_transaction(db, OWNER, account_id=current.id, date="2026-03-01", amount="100", direction="in",
                           description="HMRC CHILD BENEFIT")
    kinds = {w["kind"] for w in pj.salary_warnings(db, OWNER)}
    assert {"emergency_code", "sl_unlinked", "near_taper", "no_pension_asset",
            "not_modelled_child_benefit"} <= kinds


def test_job_validation(db):
    for bad in ({"tax_code": "12345"}, {"frequency": "daily"}, {"salary": "-1"}, {"pension_method": "magic"},
                {"student_plans": "plan 3"}, {"pension_employee_percent": "120"}, {"region": "mars"}):
        with pytest.raises(fin.FinanceError):
            pj.create_job(db, OWNER, **{"employer": "X", "pay_anchor": "2026-04-25", "salary": "30000", **bad})
    row = pj.create_job(db, OWNER, employer="X", pay_anchor="2026-04-25", salary="30000")
    assert db.query(rec.FinanceRecurring).count() == 0  # no pay account yet, so no pay item
    change = pj.add_change(db, OWNER, row.id, kind="pay rise", start_date="2027-04-01", percent="3", repeat="yearly")
    assert pj.change_to_dict(change)["description"] == "Pay rise of 3% from 01 Apr 2027 every year"
    with pytest.raises(fin.FinanceError):
        pj.add_change(db, OWNER, row.id, kind="tax_code", start_date="2027-04-01", label="ZZZ")
    pj.delete_change(db, OWNER, change.id)
    pj.delete_job(db, OWNER, row.id)
    assert pj.list_jobs(db, OWNER) == []


def test_salary_overview(db, setup):
    data = pj.salary_overview(db, OWNER)
    assert data["tax_year"] == "2026/27" and not data["rules_assumed"]
    j = data["jobs"][0]
    assert j["next_period"]["date"] == "2026-04-25"
    assert j["summary"]["periods"] == 12
    assert j["tax_code_info"]["code"] == "1257L"
    assert j["recurring_id"] == _pay_item(db).id
