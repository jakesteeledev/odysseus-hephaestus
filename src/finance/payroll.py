"""UK payroll: PAYE income tax, National Insurance, student loans and pensions.

Pure calculations, no database. Rates and thresholds come from
``src.finance.uk_tax_rules`` for the tax year of each pay date.

How a pay period is worked out:

- **Gross pay**: annual salary / pay periods (with dated pay rises), plus
  regular allowances or overtime, plus any bonus paid in the period.
- **Salary sacrifice** (pension or other schemes) comes off before tax and
  NI. From 6 April 2029, pension sacrifice above £2,000 in the tax year is
  added back to NI pay (income tax relief is unchanged).
- **Net pay arrangement** pensions reduce taxable pay but not NI pay.
- **Relief at source**: the pension % is the gross contribution; you pay 80%
  from net pay and the provider adds 20%. Any higher-rate relief is
  claimable through self assessment and is not added automatically.
- **Income tax** uses the cumulative method by default: pay and free pay
  to date (from the tax code), taxable pay rounded down to the pound, rate
  limits for the period rounded up to the pound, tax to date rounded down
  to the penny, minus tax already paid. ``W1``, ``M1`` and ``X`` codes are
  non-cumulative. ``K`` codes add to taxable pay, and tax in a period can't
  exceed 50% of that period's pay (the regulatory limit). ``BR``/``D0``/``D1``
  (and Scottish ``D2``/``D3``) tax all pay at one rate; ``NT`` takes none.
- **NI** is per pay period (never annual), with that period's thresholds.
- **Student loans** are per period from NI pay, rounded down to the pound.
  With more than one undergraduate plan, 9% is taken once above the lowest
  threshold. Postgraduate (6%) is separate.

The annual summary also works out what you actually owe for the year
(including the personal allowance taper above £100,000), which PAYE only
collects if HMRC has adjusted your tax code.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional

from src.finance import uk_tax_rules as rules_mod
from src.finance.service import FinanceError

PAY_FREQUENCIES = ("weekly", "fortnightly", "four_weekly", "monthly")
FREQUENCY_LABELS = {"weekly": "Weekly", "fortnightly": "Every 2 weeks", "four_weekly": "Every 4 weeks",
                    "monthly": "Monthly"}
PENSION_METHODS = ("none", "salary_sacrifice", "net_pay", "relief_at_source")
PENSION_METHOD_LABELS = {"none": "No workplace pension", "salary_sacrifice": "Salary sacrifice",
                         "net_pay": "Net pay arrangement", "relief_at_source": "Relief at source"}
PENSION_BASES = ("qualifying", "full")
REGIONS = ("ruk", "scotland", "wales")
REGION_LABELS = {"ruk": "England and Northern Ireland", "scotland": "Scotland", "wales": "Wales"}
CHANGE_KINDS = ("raise", "salary", "bonus", "allowance", "sacrifice", "tax_code", "pension_percent",
                "pension_method", "employer_percent", "hours", "stop")
WEEKS_PER_PERIOD = {"weekly": 1, "fortnightly": 2, "four_weekly": 4, "monthly": None}
RAS_BASIC_RELIEF_BP = 2000


class TaxCodeError(FinanceError):
    """A tax code that isn't valid, with an explanation in plain words."""


def _floor_pound(pence) -> int:
    return int(math.floor(pence / 100)) * 100 if pence >= 0 else -int(math.ceil(-pence / 100)) * 100


def _ceil_pound(pence) -> int:
    return int(math.ceil(pence / 100)) * 100


def _pct(amount: int, bp: int, rounding=ROUND_HALF_UP) -> int:
    """amount x bp / 10000 to the nearest penny."""
    return int((Decimal(amount) * Decimal(bp) / Decimal(10000)).quantize(Decimal(1), rounding=rounding))


def _pct_floor(amount: int, bp: int) -> int:
    return int(math.floor(Decimal(amount) * Decimal(bp) / Decimal(10000)))


# ── Tax codes ───────────────────────────────────────────────────────────


@dataclass
class TaxCode:
    raw: str
    region: str  # ruk | scotland | wales (from the prefix)
    kind: str  # allowance | k | flat | nt
    number: int = 0
    suffix: str = ""
    flat_band: Optional[str] = None  # BR, D0, D1, D2, D3
    cumulative: bool = True
    explanation: str = ""
    notes: list = field(default_factory=list)

    @property
    def annual_adjustment(self) -> int:
        """Free pay (positive) or K-code additional pay (negative) a year, in pence."""
        if self.kind == "allowance":
            return (self.number * 10 + 9) * 100 if self.number else 0
        if self.kind == "k":
            return -((self.number * 10 + 9) * 100)
        return 0


_CODE_RE = re.compile(r"^(S|C)?(?:(\d{1,5})([LMNT])|K(\d{1,5})|(BR|D[0-3])|(NT)|(0T))(?:(W1|M1|X))?$")


def parse_tax_code(value) -> TaxCode:
    """Read a tax code as printed on a payslip, e.g. ``1257L``, ``S1257L``,
    ``C1257L``, ``K475``, ``BR``, ``D0``, ``NT``, ``0T``, ``1257L M1``, ``1257L W1``,
    ``1257L X``. Raises :class:`TaxCodeError` explaining what's wrong."""
    raw = str(value or "").strip().upper()
    text = re.sub(r"[\s/]+", "", raw)
    if not text:
        raise TaxCodeError("enter your tax code as it appears on your payslip, for example 1257L")
    m = _CODE_RE.match(text)
    if not m:
        hint = ""
        if text.startswith(("W1", "M1")) or text == "X":
            hint = " W1, M1 or X goes after the code, e.g. 1257L M1."
        elif re.match(r"^[A-Z]?\d+$", text):
            hint = " It looks like the letter at the end is missing (most codes end in L, M, N or T)."
        elif re.search(r"[^A-Z0-9]", text):
            hint = " Tax codes only contain letters and numbers."
        raise TaxCodeError(f"'{raw}' isn't a valid UK tax code.{hint} Check your latest payslip or your "
                           "HMRC app.")
    prefix, num, suffix, k_num, flat, nt, zero_t, marker = m.groups()
    region = {"S": "scotland", "C": "wales"}.get(prefix or "", "ruk")
    where = REGION_LABELS[region]
    notes: list = []
    if flat:
        if flat in ("D2", "D3") and region != "scotland":
            raise TaxCodeError(f"'{raw}': {flat} is only used with Scottish (S) codes.")
        code = TaxCode(raw, region, "flat", flat_band=flat)
        rate_names = {"BR": "basic rate", "D0": "the higher rate" if region != "scotland" else "the intermediate rate",
                      "D1": "the additional rate" if region != "scotland" else "the higher rate",
                      "D2": "the advanced rate", "D3": "the top rate"}
        code.explanation = (f"All pay from this job is taxed at {rate_names[flat]}, with no personal allowance. "
                            "This is usual for a second job or pension.")
    elif nt:
        code = TaxCode(raw, region, "nt", explanation="No tax is taken from this pay.")
    elif zero_t:
        code = TaxCode(raw, region, "allowance", number=0,
                       explanation="No personal allowance: all pay is taxed at the normal rates. HMRC uses 0T when "
                                   "it doesn't have enough information or your allowance is used up (for example "
                                   "on income over £125,140).")
    elif k_num:
        number = int(k_num)
        code = TaxCode(raw, region, "k", number=number,
                       explanation=f"A K code: about £{number * 10:,} is added to your taxable pay each year, "
                                   "usually for a company car or other benefits, or tax owed from an earlier year. "
                                   "Tax taken in a pay period can't be more than half that period's pay.")
    else:
        number = int(num)
        code = TaxCode(raw, region, "allowance", number=number, suffix=suffix)
        allowance = number * 10
        text_suffix = {"L": "the standard personal allowance", "M": "the marriage allowance (you received 10% of "
                       "your partner's allowance)", "N": "the marriage allowance (you transferred 10% of your "
                       "allowance to your partner)", "T": "items HMRC needs to review"}[suffix]
        code.explanation = f"You get £{allowance:,} of tax-free pay a year; {suffix} means {text_suffix}."
        if suffix == "L" and number not in (1257,):
            if number < 1257:
                notes.append(f"Your allowance (£{allowance:,}) is lower than the standard £12,570. HMRC may be "
                             "collecting tax for benefits, untaxed income or an underpayment, or your income is "
                             "over £100,000.")
            else:
                notes.append(f"Your allowance (£{allowance:,}) is higher than the standard £12,570, for example "
                             "for job expenses or pension relief.")
        if number > 2000:
            notes.append("This allowance is unusually large; check the code against your payslip.")
    code.region = region
    if marker:
        code.cumulative = False
        code.explanation += (f" {marker} means an emergency, non-cumulative code: each pay period is taxed on its "
                             "own, ignoring pay and tax earlier in the tax year.")
        notes.append("Emergency codes usually change within a few weeks once HMRC has your details.")
    if region != "ruk":
        code.explanation += f" The {prefix} prefix means {where} income tax rates."
    code.notes = notes
    return code


# ── Jobs and changes ────────────────────────────────────────────────────


@dataclass
class JobChange:
    kind: str
    start: date
    end: Optional[date] = None
    amount: int = 0  # pence: annual salary, bonus, or a monthly allowance/sacrifice
    percent_bp: int = 0
    repeat: str = "once"  # once | yearly
    label: str = ""  # a tax code, a pension method, or a scheme name
    id: Optional[str] = None


@dataclass
class Job:
    id: str
    employer: str
    frequency: str
    pay_anchor: date
    salary: int  # gross annual pence
    tax_code: str = ""
    region: Optional[str] = None  # overrides the code's prefix
    ni_category: str = "A"
    student_plans: tuple = ()
    postgraduate: bool = False
    sl_liability_id: Optional[str] = None
    pg_liability_id: Optional[str] = None
    pension_method: str = "none"
    pension_employee_bp: int = 0
    pension_employee_fixed: Optional[int] = None  # pence a month
    pension_employer_bp: int = 0
    pension_employer_fixed: Optional[int] = None
    pension_basis: str = "qualifying"
    ni_passback_bp: int = 0  # share of the employer's NI saving added to the pension
    pension_asset_id: Optional[str] = None
    account_id: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    changes: list = field(default_factory=list)
    member_id: Optional[str] = None


def with_changes(job: Job, extra) -> Job:
    """A copy of *job* with extra (e.g. what-if) changes on top."""
    return replace(job, changes=list(job.changes) + list(extra or []))


def pay_dates(job: Job, start: date, end: date) -> list[date]:
    """Pay dates from start to end inclusive, following the frequency from the
    pay anchor (backwards too), within the job's start and end dates."""
    from src.finance.recurring import occurrence

    step_days = {"weekly": 7, "fortnightly": 14, "four_weekly": 28}.get(job.frequency)
    if step_days:
        n = (start - job.pay_anchor).days // step_days - 1
    else:
        n = (start.year - job.pay_anchor.year) * 12 + (start.month - job.pay_anchor.month) - 1
    out = []
    stop = _stop_date(job)
    while True:
        day = occurrence(job.pay_anchor, job.frequency, n)
        n += 1
        if day < start:
            continue
        if day > end or (stop and day >= stop) or (job.end_date and day > job.end_date):
            return out
        if job.start_date and day < job.start_date:
            continue
        out.append(day)


def _stop_date(job: Job) -> Optional[date]:
    stops = [c.start for c in job.changes if c.kind == "stop"]
    return min(stops) if stops else None


def _events(changes, kind, upto: date):
    """Dated applications of changes of *kind* up to *upto*, expanding yearly repeats."""
    out = []
    for c in changes:
        if c.kind != kind or c.start > upto:
            continue
        day = c.start
        while day <= upto and (c.end is None or day <= c.end):
            out.append((day, c))
            if c.repeat != "yearly":
                break
            day = _anniversary(day, 1)
    return sorted(out, key=lambda e: (e[0], e[1].kind))


def _anniversary(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return date(day.year + years, 3, 1)


def salary_at(job: Job, day: date) -> int:
    salary = job.salary
    timeline = _events(job.changes, "salary", day) + _events(job.changes, "raise", day)
    for _when, c in sorted(timeline, key=lambda e: (e[0], 0 if e[1].kind == "salary" else 1)):
        salary = c.amount if c.kind == "salary" else int(round(salary * (10000 + c.percent_bp) / 10000))
    hours = _latest(job.changes, "hours", day)
    if hours is not None:
        salary = int(round(salary * hours.percent_bp / 10000))
    return salary


def _latest(changes, kind, day: date):
    found = [(c.start, i, c) for i, c in enumerate(changes)
             if c.kind == kind and c.start <= day and (c.end is None or day <= c.end)]
    return max(found, key=lambda e: (e[0], e[1]))[2] if found else None  # on a tie, the later change wins


def tax_code_at(job: Job, day: date) -> str:
    latest = _latest(job.changes, "tax_code", day)
    return latest.label if latest else job.tax_code


def pension_at(job: Job, day: date) -> tuple[str, int, int]:
    """(method, employee bp, employer bp) in effect on a day."""
    method = _latest(job.changes, "pension_method", day)
    employee = _latest(job.changes, "pension_percent", day)
    employer = _latest(job.changes, "employer_percent", day)
    return (method.label if method else job.pension_method,
            employee.percent_bp if employee else job.pension_employee_bp,
            employer.percent_bp if employer else job.pension_employer_bp)


# ── Periods ─────────────────────────────────────────────────────────────


def tax_period_number(day: date, frequency: str) -> int:
    """Tax month (1-12) for monthly pay, otherwise tax week (1-53)."""
    start = rules_mod.tax_year_start(day)
    if frequency == "monthly":
        return (day.year * 12 + day.month) - (start.year * 12 + 4) + (1 if day.day >= 6 else 0)
    return (day - start).days // 7 + 1


def _period_threshold(annual: int, frequency: str) -> int:
    """An annual threshold for one pay period, rounded down to the penny."""
    return annual * {"weekly": 1, "fortnightly": 2, "four_weekly": 4}.get(frequency, 0) // 52 \
        if frequency != "monthly" else annual // 12


def _ni_thresholds(r: dict, frequency: str) -> dict:
    ni = r["ni"]
    if frequency == "monthly":
        return dict(ni["monthly"])
    mult = {"weekly": 1, "fortnightly": 2, "four_weekly": 4}[frequency]
    return {k: v * mult for k, v in ni["weekly"].items()}


def _employer_st(r: dict, frequency: str) -> int:
    st = r["ni"]["employer_st"]
    return st["monthly"] if frequency == "monthly" else st["weekly"] * {"weekly": 1, "fortnightly": 2,
                                                                        "four_weekly": 4}[frequency]


def ni_for_period(pay: int, r: dict, frequency: str, category: str = "A") -> int:
    """Employee Class 1 NI on one period's NI pay (rounded to the penny, half down)."""
    main, upper = r["ni"]["categories"].get(category.upper(), r["ni"]["categories"]["A"])
    t = _ni_thresholds(r, frequency)
    band = max(0, min(pay, t["uel"]) - t["pt"])
    above = max(0, pay - t["uel"])
    total = Decimal(band) * main / 10000 + Decimal(above) * upper / 10000
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_DOWN))


def employer_ni_for_period(pay: int, r: dict, frequency: str) -> int:
    return _pct(max(0, pay - _employer_st(r, frequency)), r["ni"]["employer_rate"], ROUND_HALF_DOWN)


def _bands(r: dict, region: str) -> list:
    return r["income_tax"]["scotland" if region == "scotland" else "ruk"]


def tax_on_taxable(taxable: int, bands: list, *, k: int = 1, divisor: int = 1, extend_basic: int = 0) -> int:
    """Tax on *taxable* pence with band limits scaled to k/divisor of a year
    (rounded up to the pound). *extend_basic* widens the basic band (relief at source)."""
    tax = Decimal(0)
    lower = 0
    basic_index = 1 if len(bands) > 3 else 0  # Scotland: starter then basic
    for i, (limit, rate) in enumerate(bands):
        upper = None if limit is None else _ceil_pound(limit * k / divisor)
        if upper is not None and i >= basic_index and extend_basic:
            upper += extend_basic
        portion = max(0, (taxable if upper is None else min(taxable, upper)) - lower)
        tax += Decimal(portion) * rate / 10000
        if upper is None or taxable <= upper:
            break
        lower = upper
    return int(math.floor(tax))


def _flat_rate(code: TaxCode, region: str, r: dict) -> int:
    bands = _bands(r, region)
    if region == "scotland":
        return {"BR": 2000, "D0": 2100, "D1": 4200, "D2": 4500, "D3": 4800}[code.flat_band]
    return {"BR": bands[0][1], "D0": bands[1][1], "D1": bands[2][1]}[code.flat_band]


def tax_for_period(code: TaxCode, region: str, r: dict, frequency: str, n: int, *, period_taxable: int,
                   taxable_to_date: int, tax_paid_to_date: int, period_pay: int) -> int:
    """PAYE income tax due for one period."""
    if code.kind == "nt":
        return 0
    if code.kind == "flat":
        return _pct_floor(_floor_pound(max(0, period_taxable)), _flat_rate(code, region, r))
    divisor = 12 if frequency == "monthly" else 52
    if code.cumulative:
        k, pay_td, paid = n, taxable_to_date, tax_paid_to_date
    else:
        k, pay_td, paid = (WEEKS_PER_PERIOD[frequency] or 1), period_taxable, 0
    per_period = math.ceil(abs(code.annual_adjustment) / divisor)
    adjust = per_period * k * (1 if code.annual_adjustment >= 0 else -1)
    taxable = _floor_pound(max(0, pay_td - adjust))
    due = tax_on_taxable(taxable, _bands(r, region), k=k, divisor=divisor) - paid
    if code.kind == "k":
        due = min(due, period_pay // 2)  # regulatory limit: at most 50% of this period's pay
    return due


def _student_loan(pay: int, plans: tuple, r: dict, frequency: str) -> dict:
    """Deductions per plan for one period, rounded down to the pound."""
    out = {}
    loans = r["student_loans"]
    ug = sorted((p for p in plans if p in loans and p != "postgraduate"), key=lambda p: loans[p]["annual"])
    if ug:
        thresholds = [_period_threshold(loans[p]["annual"], frequency) for p in ug]
        total = _floor_pound(_pct_floor(max(0, pay - thresholds[0]), loans[ug[0]]["rate"]))
        # Allocate: the higher plan(s) take 9% above their own threshold; the rest goes to the lowest.
        remaining = total
        for plan, threshold in reversed(list(zip(ug, thresholds))[1:]):
            share = min(remaining, _floor_pound(_pct_floor(max(0, pay - threshold), loans[plan]["rate"])))
            out[plan] = share
            remaining -= share
        out[ug[0]] = remaining
    if "postgraduate" in plans:
        pg = loans["postgraduate"]
        out["postgraduate"] = _floor_pound(_pct_floor(max(0, pay - _period_threshold(pg["annual"], frequency)),
                                                      pg["rate"]))
    return out


def _qualifying(gross: int, r: dict, frequency: str) -> int:
    low, high = r["auto_enrolment"][frequency]
    return max(0, min(gross, high) - low)


def _per_period(monthly: int, frequency: str) -> int:
    return int(round(monthly * 12 / rules_mod.PERIODS_PER_YEAR[frequency]))


def _in_period(day: date, previous: Optional[date], pay_day: date) -> bool:
    return (previous is None or day > previous) and day <= pay_day


def calc_tax_year(job: Job, year: int, *, upto: Optional[date] = None) -> list[dict]:
    """Every pay period of the tax year starting 6 April *year*, in order, with
    year-to-date totals. Stops after *upto* if given."""
    start, end = date(year, 4, 6), date(year + 1, 4, 5)
    r = rules_mod.rules_for(year)
    periods_per_year = rules_mod.PERIODS_PER_YEAR[job.frequency]
    dates = pay_dates(job, start - timedelta(days=40), end)
    previous_by_date = {d: (dates[i - 1] if i else None) for i, d in enumerate(dates)}
    dates = [d for d in dates if d >= start]
    ytd = {k: 0 for k in ("gross", "taxable", "ni_pay", "tax", "ni", "employer_ni", "student_loan", "postgraduate",
                          "pension_employee", "pension_employer", "pension_relief", "pension_sacrifice",
                          "other_sacrifice", "net", "pension_deduction")}
    out = []
    code_cache: dict = {}
    for pay_day in dates:
        if upto and pay_day > upto:
            break
        prev = previous_by_date.get(pay_day)
        code_text = tax_code_at(job, pay_day) or "1257L"
        if code_text not in code_cache:
            code_cache[code_text] = parse_tax_code(code_text)
        code = code_cache[code_text]
        region = job.region or code.region
        salary = salary_at(job, pay_day)
        basic = int(round(salary / periods_per_year))
        extras = sum(_per_period(c.amount, job.frequency) for c in job.changes
                     if c.kind == "allowance" and c.start <= pay_day and (c.end is None or pay_day <= c.end))
        bonus = sum(c.amount for when, c in _events(job.changes, "bonus", pay_day) if _in_period(when, prev, pay_day))
        gross = basic + extras + bonus
        other_sacrifice = sum(_per_period(c.amount, job.frequency) for c in job.changes
                              if c.kind == "sacrifice" and c.start <= pay_day and (c.end is None or pay_day <= c.end))
        method, employee_bp, employer_bp = pension_at(job, pay_day)
        pensionable = _qualifying(gross, r, job.frequency) if job.pension_basis == "qualifying" else basic
        if method == "none":
            contribution = employer = 0
        else:
            contribution = (_per_period(job.pension_employee_fixed, job.frequency)
                            if job.pension_employee_fixed is not None else _pct(pensionable, employee_bp))
            employer = (_per_period(job.pension_employer_fixed, job.frequency)
                        if job.pension_employer_fixed is not None else _pct(pensionable, employer_bp))
        contribution = min(contribution, max(0, gross - other_sacrifice))
        sacrifice = contribution if method == "salary_sacrifice" else 0
        cap = r["salary_sacrifice_cap"]
        ni_exempt_sacrifice = sacrifice
        if cap is not None and sacrifice:
            ni_exempt_sacrifice = max(0, min(sacrifice, cap - ytd["pension_sacrifice"]))
        npa = contribution if method == "net_pay" else 0
        taxable = gross - other_sacrifice - sacrifice - npa
        ni_pay = gross - other_sacrifice - ni_exempt_sacrifice
        n = tax_period_number(pay_day, job.frequency)
        tax = tax_for_period(code, region, r, job.frequency, n, period_taxable=taxable,
                             taxable_to_date=ytd["taxable"] + taxable, tax_paid_to_date=ytd["tax"],
                             period_pay=taxable)
        ni = ni_for_period(ni_pay, r, job.frequency, job.ni_category)
        employer_ni = employer_ni_for_period(ni_pay, r, job.frequency)
        if sacrifice and job.ni_passback_bp:
            saved = employer_ni_for_period(ni_pay + ni_exempt_sacrifice, r, job.frequency) - employer_ni
            employer += _pct(saved, job.ni_passback_bp)
        plans = tuple(job.student_plans) + (("postgraduate",) if job.postgraduate else ())
        loans = _student_loan(ni_pay, plans, r, job.frequency)
        ug_loan = sum(v for k, v in loans.items() if k != "postgraduate")
        pg_loan = loans.get("postgraduate", 0)
        ras_paid = _pct(contribution, 10000 - RAS_BASIC_RELIEF_BP) if method == "relief_at_source" else 0
        relief = contribution - ras_paid if method == "relief_at_source" else 0
        deduction = sacrifice or npa or ras_paid
        net = gross - other_sacrifice - sacrifice - npa - tax - ni - ug_loan - pg_loan - ras_paid
        row = {
            "date": pay_day.isoformat(), "tax_period": n, "tax_year": r["label"], "rules_assumed": r["assumed"],
            "tax_code": code.raw, "cumulative": code.cumulative, "region": region,
            "salary_pence": salary, "basic_pence": basic, "extras_pence": extras, "bonus_pence": bonus,
            "gross_pence": gross, "pension_sacrifice_pence": sacrifice, "other_sacrifice_pence": other_sacrifice,
            "ni_sacrifice_added_back_pence": sacrifice - ni_exempt_sacrifice,
            "taxable_pence": taxable, "ni_pay_pence": ni_pay, "tax_pence": tax, "ni_pence": ni,
            "employer_ni_pence": employer_ni, "student_loans": loans, "student_loan_pence": ug_loan,
            "postgraduate_pence": pg_loan, "pension_method": method, "pensionable_pence": pensionable,
            "pension_employee_pence": contribution, "pension_deduction_pence": deduction,
            "pension_employer_pence": employer, "pension_relief_pence": relief, "net_pence": net,
        }
        for key, value in (("gross", gross), ("taxable", taxable), ("ni_pay", ni_pay), ("tax", tax), ("ni", ni),
                           ("employer_ni", employer_ni), ("student_loan", ug_loan), ("postgraduate", pg_loan),
                           ("pension_employee", contribution), ("pension_employer", employer),
                           ("pension_relief", relief), ("pension_sacrifice", sacrifice),
                           ("other_sacrifice", other_sacrifice), ("net", net), ("pension_deduction", deduction)):
            ytd[key] += value
        row["ytd"] = {f"{k}_pence": v for k, v in ytd.items()}
        out.append(row)
    return out


def period_on(job: Job, day: date) -> Optional[dict]:
    """The pay period paid on *day* (None if it isn't a pay date)."""
    year = rules_mod.tax_year_start(day).year
    return next((p for p in calc_tax_year(job, year, upto=day) if p["date"] == day.isoformat()), None)


def next_period(job: Job, today: date) -> Optional[dict]:
    """The next pay period on or after today."""
    for year in (rules_mod.tax_year_start(today).year, rules_mod.tax_year_start(today).year + 1):
        for p in calc_tax_year(job, year):
            if p["date"] >= today.isoformat():
                return p
    return None


# ── Annual summary ─────────────────────────────────────────────────────


def personal_allowance(adjusted_net_income: int, r: dict) -> int:
    """£1 less allowance for every £2 of adjusted net income above £100,000."""
    pa = r["personal_allowance"]
    over = max(0, adjusted_net_income - pa["taper_from"])
    return max(0, pa["amount"] - _floor_pound(over // 2))


def annual_liability(job: Job, year: int, *, salary: Optional[int] = None) -> dict:
    """Income tax actually owed for the year on this job's pay (a single-job
    view, with the allowance taper), NI and student loans on an annual basis.
    Used for the marginal rate; PAYE per period is in :func:`calc_tax_year`."""
    r = rules_mod.rules_for(year)
    probe = with_changes(job, [JobChange("salary", date(year, 4, 6), amount=salary)]) if salary is not None else job
    periods = calc_tax_year(probe, year)
    if not periods:
        return {"tax_pence": 0, "ni_pence": 0, "student_loan_pence": 0, "gross_pence": 0}
    last = periods[-1]["ytd"]
    code = parse_tax_code(tax_code_at(job, date(year + 1, 4, 5)) or "1257L")
    region = job.region or code.region
    ras = last["pension_employee_pence"] if periods[-1]["pension_method"] == "relief_at_source" else 0
    taxable_income = last["taxable_pence"]
    ani = taxable_income - ras
    statutory = personal_allowance(ani, r)
    if code.kind == "k":
        pa = -code.number * 1000
    elif code.kind == "allowance" and code.number != 1257:
        # A non-standard code (benefits, marriage allowance, 0T...): its allowance, less any taper.
        pa = max(0, code.number * 1000 - (r["personal_allowance"]["amount"] - statutory))
    else:
        pa = statutory
    if code.kind in ("flat", "nt"):
        tax = sum(p["tax_pence"] for p in periods)
    else:
        tax = tax_on_taxable(_floor_pound(max(0, taxable_income - pa)), _bands(r, region), extend_basic=ras)
    return {"tax_pence": tax, "ni_pence": last["ni_pence"], "student_loan_pence": last["student_loan_pence"]
            + last["postgraduate_pence"], "gross_pence": last["gross_pence"], "personal_allowance_pence": pa,
            "adjusted_net_income_pence": ani, "taxable_income_pence": taxable_income, "ras_gross_pence": ras}


def annual_summary(job: Job, year: int) -> dict:
    """Take-home, effective and marginal rates, and what the pension really costs."""
    r = rules_mod.rules_for(year)
    periods = calc_tax_year(job, year)
    if not periods:
        return {"tax_year": r["label"], "periods": 0}
    y = periods[-1]["ytd"]
    owed = annual_liability(job, year)
    salary = salary_at(job, date(year + 1, 4, 5))
    step = 15_600  # £156 divides evenly into every pay frequency, so rounding doesn't distort the rate
    higher = annual_liability(job, year, salary=salary + step)
    base = annual_liability(job, year, salary=salary)
    marginal_tax = (higher["tax_pence"] - base["tax_pence"]) / step
    marginal_all = ((higher["tax_pence"] + higher["ni_pence"] + higher["student_loan_pence"])
                    - (base["tax_pence"] + base["ni_pence"] + base["student_loan_pence"])) / step
    deductions = y["tax_pence"] + y["ni_pence"] + y["student_loan_pence"] + y["postgraduate_pence"]
    no_pension = calc_tax_year(with_changes(job, [JobChange("pension_method", date(year, 4, 6), label="none")]), year)
    net_without = no_pension[-1]["ytd"]["net_pence"] if no_pension else y["net_pence"]
    # For relief at source the employee figure is the gross contribution, which includes the 20% top-up.
    into_pension = y["pension_employee_pence"] + y["pension_employer_pence"]
    cost = net_without - y["net_pence"]
    claimable = _ras_claimable(job, year, owed) if periods[-1]["pension_method"] == "relief_at_source" else 0
    paye_gap = owed["tax_pence"] - y["tax_pence"]
    return {
        "tax_year": r["label"], "rules_assumed": r["assumed"], "periods": len(periods),
        "gross_pence": y["gross_pence"], "taxable_pence": y["taxable_pence"], "tax_pence": y["tax_pence"],
        "ni_pence": y["ni_pence"], "student_loan_pence": y["student_loan_pence"],
        "postgraduate_pence": y["postgraduate_pence"], "pension_employee_pence": y["pension_employee_pence"],
        "pension_employer_pence": y["pension_employer_pence"], "pension_relief_pence": y["pension_relief_pence"],
        "employer_ni_pence": y["employer_ni_pence"], "net_pence": y["net_pence"],
        "take_home_monthly_pence": int(round(y["net_pence"] / 12)),
        "effective_rate_percent": round(deductions / y["gross_pence"] * 100, 1) if y["gross_pence"] else 0.0,
        "marginal_tax_percent": round(marginal_tax * 100, 1),
        "marginal_rate_percent": round(marginal_all * 100, 1),
        "personal_allowance_pence": owed["personal_allowance_pence"],
        "tax_owed_pence": owed["tax_pence"], "paye_difference_pence": paye_gap,
        "into_pension_pence": into_pension, "pension_cost_pence": cost,
        "pension_relief_claimable_pence": claimable,
        "taper_band": owed["adjusted_net_income_pence"] > r["personal_allowance"]["taper_from"]
        and owed["personal_allowance_pence"] > 0,
    }


def _ras_claimable(job: Job, year: int, owed: dict) -> int:
    """Higher/additional-rate relief on relief-at-source contributions, claimable via self assessment."""
    r = rules_mod.rules_for(year)
    code = parse_tax_code(tax_code_at(job, date(year + 1, 4, 5)) or "1257L")
    region = job.region or code.region
    taxable = _floor_pound(max(0, owed["taxable_income_pence"] - owed["personal_allowance_pence"]))
    without = tax_on_taxable(taxable, _bands(r, region))
    with_relief = tax_on_taxable(taxable, _bands(r, region), extend_basic=owed["ras_gross_pence"])
    # Extending the basic band by the gross contribution is how relief at source works;
    # the provider's 20% is already in the pension, so only the extra is claimable.
    return max(0, without - with_relief)


# ── Payslip check ───────────────────────────────────────────────────────

PAYSLIP_FIELDS = ("gross", "tax", "ni", "pension", "student_loan", "net")
_FIELD_MAP = {"gross": "gross_pence", "tax": "tax_pence", "ni": "ni_pence", "pension": "pension_deduction_pence",
              "net": "net_pence"}
TOLERANCE = 100  # £1


def compare_payslip(job: Job, payslip: dict) -> dict:
    """Compare a real payslip (pence per field, ``date``) with the calculation."""
    day = payslip["date"] if isinstance(payslip["date"], date) else date.fromisoformat(payslip["date"])
    calc = period_on(job, day)
    if calc is None:
        # Not a scheduled pay date: compare with the nearest one.
        year = rules_mod.tax_year_start(day).year
        nearby = calc_tax_year(job, year) + calc_tax_year(job, year - 1)
        calc = min(nearby, key=lambda p: abs((date.fromisoformat(p["date"]) - day).days)) if nearby else None
    if calc is None:
        raise FinanceError("there are no pay dates for this job around that date")
    rows = []
    for name in PAYSLIP_FIELDS:
        actual = payslip.get(name)
        if actual is None:
            continue
        expected = (calc["student_loan_pence"] + calc["postgraduate_pence"]) if name == "student_loan" \
            else calc[_FIELD_MAP[name]]
        diff = actual - expected
        rows.append({"field": name, "actual_pence": actual, "expected_pence": expected, "difference_pence": diff,
                     "flag": abs(diff) > TOLERANCE})
    return {"date": day.isoformat(), "calculated": calc, "rows": rows,
            "matches": not any(r["flag"] for r in rows), "causes": _causes(job, calc, {r["field"]: r for r in rows})}


def _causes(job: Job, calc: dict, rows: dict) -> list[str]:
    causes = []
    gross = rows.get("gross")
    tax = rows.get("tax")
    if gross and gross["flag"]:
        if gross["difference_pence"] > 0:
            causes.append("Your gross pay is higher than expected: back pay, overtime, a bonus or a pay rise that "
                          "isn't in your salary setup yet.")
        else:
            causes.append("Your gross pay is lower than expected: unpaid leave, fewer hours, or a salary sacrifice "
                          "scheme that isn't in your setup.")
    if tax and tax["flag"] and not (gross and gross["flag"]):
        code = parse_tax_code(calc["tax_code"])
        if code.cumulative and code.kind == "allowance":
            one_off = with_changes(job, [JobChange("tax_code", date.fromisoformat(calc["date"]),
                                                   label=f"{calc['tax_code']} M1" if job.frequency == "monthly"
                                                   else f"{calc['tax_code']} W1")])
            emergency = period_on(one_off, date.fromisoformat(calc["date"]))
            if emergency and abs(emergency["tax_pence"] - tax["actual_pence"]) <= TOLERANCE:
                causes.append("The tax matches an emergency (non-cumulative) code: your employer may be using "
                              f"{calc['tax_code']} W1/M1. Check the code printed on your payslip.")
        causes.append("Your employer may be using a different tax code from the one in your setup (check the "
                      "payslip), or the code changed during the tax year, which spreads a refund or underpayment "
                      "over the remaining months.")
    if rows.get("ni") and rows["ni"]["flag"] and not (gross and gross["flag"]):
        causes.append("NI differs: check your NI category letter (usually A) and whether your pension is salary "
                      "sacrifice.")
    if rows.get("pension") and rows["pension"]["flag"]:
        causes.append("Pension differs: check the percentage, the method (salary sacrifice, net pay or relief at "
                      "source) and whether it's on qualifying earnings or full salary.")
    if rows.get("student_loan") and rows["student_loan"]["flag"]:
        causes.append("Student loan differs: check the plan type(s) and postgraduate loan.")
    if rows.get("net") and rows["net"]["flag"] and len(causes) == 0:
        causes.append("Net pay differs but each deduction matches: there may be another deduction on the payslip "
                      "(for example a union fee, a loan from your employer or a benefit) that isn't in your setup.")
    return causes
