"""Salary setup: jobs, dated changes, payslips, and everything that follows from them.

A job's net pay is calculated by ``src.finance.payroll``. Each job keeps one
recurring income item (``FinanceRecurring.job_id``) on the chosen account,
whose amount follows the calculation per pay date, so Upcoming, matching,
budgets and projections all use the calculated pay. That item can't be
edited by hand; change the job instead.

When a pay date is confirmed (or matched to an imported deposit), the
pension contributions (employee + employer, including any relief-at-source
top-up) are recorded on the linked pension asset and student loan
deductions as payments on the linked liability. Undoing the pay date
removes them again.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.finance import payroll
from src.finance import uk_tax_rules as tax_rules
from src.finance.models import (
    FinanceAccount,
    FinanceAsset,
    FinanceCategory,
    FinanceContribution,
    FinanceJob,
    FinanceJobChange,
    FinanceLiability,
    FinancePayslip,
    FinanceRecurring,
    FinanceRepayment,
    FinanceTransaction,
)
from src.finance.money import MoneyError, parse_pence
from src.finance.service import (
    MAX_NAME_LEN,
    FinanceError,
    FinanceNotFound,
    _clean_text,
    _new_id,
    _require_owner,
    _today_for_owner,
    parse_date,
)

MAX_SALARY = 1_000_000_000  # £10m
EMERGENCY_GRACE_DAYS = 61
NEAR_THRESHOLD = 500_000  # warn within £5,000 below a threshold
PAYROLL_NOTE = "Payroll"
SALARY_ACTIONS = ("raise", "salary", "bonus", "pension_percent", "pension_method", "hours", "stop", "tax_code")


# ── Parsing ─────────────────────────────────────────────────────────────


def _pounds(value, field: str, *, allow_zero=True) -> int:
    try:
        pence = parse_pence(value)
    except MoneyError as exc:
        raise FinanceError(f"{field}: {exc}") from exc
    if pence < 0 or (pence == 0 and not allow_zero):
        raise FinanceError(f"{field} must be {'zero or more' if allow_zero else 'more than zero'}")
    if pence > MAX_SALARY:
        raise FinanceError(f"{field} looks too large")
    return pence


def _percent(value, field: str, low=0, high=10000) -> int:
    from src.finance.projections import parse_rate

    return parse_rate(value, field=field, low=low, high=high)


def _date(value, field: str, required=False) -> Optional[date]:
    if value in (None, ""):
        if required:
            raise FinanceError(f"{field} is required")
        return None
    return value if isinstance(value, date) else parse_date(value, field=field)


def _frequency(value) -> str:
    text = str(value or "monthly").strip().lower().replace(" ", "_").replace("-", "_")
    text = {"week": "weekly", "every_week": "weekly", "every_2_weeks": "fortnightly", "biweekly": "fortnightly",
            "two_weekly": "fortnightly", "4_weekly": "four_weekly", "every_4_weeks": "four_weekly",
            "four_weeks": "four_weekly", "month": "monthly", "every_month": "monthly"}.get(text, text)
    if text not in payroll.PAY_FREQUENCIES:
        raise FinanceError("pay frequency must be weekly, every 2 weeks, every 4 weeks or monthly")
    return text


def _method(value) -> str:
    text = str(value or "none").strip().lower().replace(" ", "_").replace("-", "_")
    text = {"sacrifice": "salary_sacrifice", "ss": "salary_sacrifice", "net_pay_arrangement": "net_pay",
            "npa": "net_pay", "ras": "relief_at_source", "relief": "relief_at_source", "no": "none",
            "": "none"}.get(text, text)
    if text not in payroll.PENSION_METHODS:
        raise FinanceError("pension method must be salary sacrifice, net pay arrangement, relief at source or none")
    return text


def _plans(value) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;/]+|\band\b", str(value))
    out = []
    for raw in items:
        text = re.sub(r"[\s_]+", "_", str(raw).strip().lower())
        if not text or text in ("none", "no"):
            continue
        text = {"1": "plan_1", "2": "plan_2", "4": "plan_4", "5": "plan_5", "plan1": "plan_1", "plan2": "plan_2",
                "plan4": "plan_4", "plan5": "plan_5", "pg": "postgraduate", "postgrad": "postgraduate",
                "pgl": "postgraduate"}.get(text, text)
        if text == "postgraduate":
            continue
        if text not in tax_rules.STUDENT_PLANS:
            raise FinanceError("student loan plans are plan 1, plan 2, plan 4 and plan 5")
        if text not in out:
            out.append(text)
    return out


def _owned(db: Session, owner: str, model, item_id, label: str):
    if item_id in (None, ""):
        return None
    row = db.query(model).filter(model.owner == owner, model.id == str(item_id)).first()
    if row is None:
        raise FinanceNotFound(f"{label} not found")
    return row


# ── Jobs ────────────────────────────────────────────────────────────────


def get_job(db: Session, owner, job_id) -> FinanceJob:
    row = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.id == str(job_id or "")).first()
    if row is None:
        raise FinanceNotFound("job not found")
    return row


def find_job(db: Session, owner, name=None) -> FinanceJob:
    """A job by id or employer name; the only active job when *name* is empty."""
    from src.finance.matching import match_name

    owner = _require_owner(owner)
    rows = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.archived.is_(False)).order_by(
        FinanceJob.employer).all()
    if not rows:
        raise FinanceNotFound("no salary set up yet: add your job in Finance > Salary")
    text = str(name or "").strip()
    if not text:
        if len(rows) == 1:
            return rows[0]
        raise FinanceError("you have more than one job: say which employer (" + ", ".join(r.employer for r in rows) + ")")
    by_id = [r for r in rows if r.id == text]
    if by_id:
        return by_id[0]
    found = match_name(text, rows, name=lambda r: r.employer)
    if found.ok:
        return found.item
    raise FinanceError(f"no job matching '{text}'; jobs: {', '.join(r.employer for r in rows)}")


def _changes(db: Session, owner: str, job_id: str) -> list[FinanceJobChange]:
    return db.query(FinanceJobChange).filter(FinanceJobChange.owner == owner,
                                             FinanceJobChange.job_id == job_id).order_by(
        FinanceJobChange.start_date, FinanceJobChange.id).all()


def snapshot(db: Session, owner: str, row: FinanceJob) -> payroll.Job:
    """The pure payroll view of a job and its changes."""
    return payroll.Job(
        id=row.id, employer=row.employer, frequency=row.frequency, pay_anchor=row.pay_anchor,
        salary=row.salary_pence, tax_code=row.tax_code or "", region=row.region or None,
        ni_category=row.ni_category or "A",
        student_plans=tuple(p for p in (row.student_plans or "").split(",") if p),
        postgraduate=bool(row.postgraduate), sl_liability_id=row.sl_liability_id, pg_liability_id=row.pg_liability_id,
        pension_method=row.pension_method, pension_employee_bp=row.pension_employee_bp,
        pension_employee_fixed=row.pension_employee_fixed_pence, pension_employer_bp=row.pension_employer_bp,
        pension_employer_fixed=row.pension_employer_fixed_pence, pension_basis=row.pension_basis,
        ni_passback_bp=row.ni_passback_bp, pension_asset_id=row.pension_asset_id, account_id=row.account_id,
        start_date=row.start_date, end_date=row.end_date, member_id=row.member_id,
        changes=[payroll.JobChange(c.kind, c.start_date, c.end_date, c.amount_pence, c.percent_bp, c.repeat,
                                   c.label, c.id) for c in _changes(db, owner, row.id)],
    )


def change_to_dict(c: FinanceJobChange) -> dict:
    return {"id": c.id, "job_id": c.job_id, "kind": c.kind, "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat() if c.end_date else None, "amount_pence": c.amount_pence,
            "percent": c.percent_bp / 100, "repeat": c.repeat, "label": c.label or "",
            "description": describe_job_change(c)}


def describe_job_change(c) -> str:
    when = c.start_date.strftime("%d %b %Y")
    yearly = " every year" if c.repeat == "yearly" else ""
    pounds = f"£{c.amount_pence / 100:,.2f}"
    return {
        "raise": f"Pay rise of {c.percent_bp / 100:g}% from {when}{yearly}",
        "salary": f"New salary {pounds} a year from {when}",
        "bonus": f"Bonus of {pounds} paid {when}{yearly}",
        "allowance": f"{c.label or 'Allowance or overtime'}: {pounds} a month from {when}"
                     + (f" to {c.end_date.strftime('%d %b %Y')}" if c.end_date else ""),
        "sacrifice": f"{c.label or 'Salary sacrifice'}: {pounds} a month from {when}"
                     + (f" to {c.end_date.strftime('%d %b %Y')}" if c.end_date else ""),
        "tax_code": f"Tax code {c.label} from {when}",
        "pension_percent": f"Pension contribution {c.percent_bp / 100:g}% from {when}",
        "employer_percent": f"Employer pension {c.percent_bp / 100:g}% from {when}",
        "pension_method": f"Pension by {payroll.PENSION_METHOD_LABELS.get(c.label, c.label).lower()} from {when}",
        "hours": f"Hours reduced to {c.percent_bp / 100:g}% from {when}",
        "stop": f"Job ends {when}",
    }.get(c.kind, c.kind)


def job_to_dict(db: Session, owner: str, row: FinanceJob) -> dict:
    return {
        "id": row.id, "employer": row.employer, "member_id": row.member_id, "frequency": row.frequency,
        "frequency_label": payroll.FREQUENCY_LABELS[row.frequency], "pay_anchor": row.pay_anchor.isoformat(),
        "salary_pence": row.salary_pence, "tax_code": row.tax_code or "", "region": row.region,
        "ni_category": row.ni_category, "student_plans": [p for p in (row.student_plans or "").split(",") if p],
        "postgraduate": bool(row.postgraduate), "sl_liability_id": row.sl_liability_id,
        "pg_liability_id": row.pg_liability_id, "pension_method": row.pension_method,
        "pension_method_label": payroll.PENSION_METHOD_LABELS[row.pension_method],
        "pension_employee_percent": row.pension_employee_bp / 100,
        "pension_employee_fixed_pence": row.pension_employee_fixed_pence,
        "pension_employer_percent": row.pension_employer_bp / 100,
        "pension_employer_fixed_pence": row.pension_employer_fixed_pence, "pension_basis": row.pension_basis,
        "ni_passback_percent": row.ni_passback_bp / 100, "pension_asset_id": row.pension_asset_id,
        "account_id": row.account_id, "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None, "archived": bool(row.archived),
        "notes": row.notes or "", "recurring_id": _recurring_for(db, owner, row.id).id
        if _recurring_for(db, owner, row.id) else None,
        "changes": [change_to_dict(c) for c in _changes(db, owner, row.id)],
    }


_JOB_FIELDS = ("employer", "member_id", "frequency", "pay_anchor", "salary", "tax_code", "region", "ni_category",
               "student_plans", "postgraduate", "sl_liability_id", "pg_liability_id", "pension_method",
               "pension_employee_percent", "pension_employee_fixed", "pension_employer_percent",
               "pension_employer_fixed", "pension_basis", "ni_passback_percent", "pension_asset_id", "account_id",
               "start_date", "end_date", "notes", "archived")


def _apply(db: Session, owner: str, row: FinanceJob, data: dict, *, creating: bool) -> None:
    from src.finance.models import FinanceMember

    unknown = set(data) - set(_JOB_FIELDS)
    if unknown:
        raise FinanceError(f"unknown salary field: {', '.join(sorted(unknown))}")
    has = lambda key: key in data and (creating or data[key] is not None)  # noqa: E731
    if creating or has("employer"):
        row.employer = _clean_text(data.get("employer"), field="employer", max_len=MAX_NAME_LEN, required=True)
    if creating or has("frequency"):
        row.frequency = _frequency(data.get("frequency"))
    if creating or has("pay_anchor"):
        row.pay_anchor = _date(data.get("pay_anchor"), "pay date", required=True)
    if creating or has("salary"):
        row.salary_pence = _pounds(data.get("salary"), "salary", allow_zero=False)
    if has("tax_code"):
        text = str(data.get("tax_code") or "").strip().upper()
        if text:
            payroll.parse_tax_code(text)
        row.tax_code = re.sub(r"\s+", " ", text)
    if has("region"):
        region = str(data.get("region") or "").strip().lower() or None
        region = {"england": "ruk", "northern_ireland": "ruk", "ni": "ruk", "rest_of_uk": "ruk"}.get(region, region)
        if region not in (None, *payroll.REGIONS):
            raise FinanceError("region must be England and Northern Ireland, Scotland or Wales")
        row.region = region
    if has("ni_category"):
        letter = str(data.get("ni_category") or "A").strip().upper()
        if letter not in tax_rules.RULES[max(tax_rules.RULES)]["ni"]["categories"]:
            raise FinanceError("NI category must be a letter such as A, B, C, H, J, M, X or Z")
        row.ni_category = letter
    if has("student_plans"):
        plans = _plans(data.get("student_plans"))
        row.student_plans = ",".join(plans)
        if isinstance(data.get("student_plans"), (list, tuple)) and any(
                str(p).lower() in ("postgraduate", "pg", "postgrad") for p in data["student_plans"]):
            row.postgraduate = True
    if has("postgraduate"):
        row.postgraduate = bool(data.get("postgraduate"))
    for key, label in (("sl_liability_id", "student loan"), ("pg_liability_id", "postgraduate loan")):
        if key in data:
            found = _owned(db, owner, FinanceLiability, data[key], label)
            setattr(row, key, found.id if found else None)
    if creating or has("pension_method"):
        row.pension_method = _method(data.get("pension_method"))
    for key, attr, fixed_attr, label in (
            ("pension_employee_percent", "pension_employee_bp", "pension_employee_fixed_pence", "your pension"),
            ("pension_employer_percent", "pension_employer_bp", "pension_employer_fixed_pence", "employer pension")):
        if has(key):
            value = data.get(key)
            setattr(row, attr, 0 if value in (None, "") else _percent(value, f"{label} %"))
        elif creating:
            setattr(row, attr, 0)
        fixed_key = key.replace("_percent", "_fixed")
        if fixed_key in data:
            value = data.get(fixed_key)
            setattr(row, fixed_attr, None if value in (None, "") else _pounds(value, f"{label} (a month)"))
    if creating or has("pension_basis"):
        basis = str(data.get("pension_basis") or "qualifying").strip().lower().replace(" ", "_")
        basis = {"qualifying_earnings": "qualifying", "full_salary": "full", "basic": "full"}.get(basis, basis)
        if basis not in payroll.PENSION_BASES:
            raise FinanceError("pension basis must be qualifying earnings or full salary")
        row.pension_basis = basis
    if has("ni_passback_percent"):
        value = data.get("ni_passback_percent")
        row.ni_passback_bp = 0 if value in (None, "") else _percent(value, "NI saving passed on")
    elif creating:
        row.ni_passback_bp = 0
    if "pension_asset_id" in data:
        asset = _owned(db, owner, FinanceAsset, data["pension_asset_id"], "pension")
        if asset is not None and asset.asset_type != "pension":
            raise FinanceError("link the workplace pension to a pension asset")
        row.pension_asset_id = asset.id if asset else None
    if "account_id" in data:
        account = _owned(db, owner, FinanceAccount, data["account_id"], "account")
        if account is not None and account.account_type == "credit_card":
            raise FinanceError("choose a bank account for your pay, not a credit card")
        row.account_id = account.id if account else None
    if "member_id" in data:
        member = _owned(db, owner, FinanceMember, data["member_id"], "household member")
        row.member_id = member.id if member else None
    for key in ("start_date", "end_date"):
        if key in data:
            setattr(row, key, _date(data[key], key.replace("_", " ")))
    if row.start_date and row.end_date and row.end_date < row.start_date:
        raise FinanceError("the end date must be after the start date")
    if has("notes"):
        row.notes = _clean_text(data.get("notes") or "", field="notes", max_len=1000)
    if has("archived") and not creating:
        row.archived = bool(data["archived"])


def create_job(db: Session, owner, **data) -> FinanceJob:
    owner = _require_owner(owner)
    row = FinanceJob(id=_new_id(), owner=owner)
    if "member_id" not in data:
        from src.finance.members import ensure_household

        data["member_id"] = ensure_household(db, owner).id
    _apply(db, owner, row, data, creating=True)
    db.add(row)
    db.flush()
    sync_recurring(db, owner, row)
    return row


def update_job(db: Session, owner, job_id, **data) -> FinanceJob:
    owner = _require_owner(owner)
    row = get_job(db, owner, job_id)
    _apply(db, owner, row, data, creating=False)
    db.flush()
    sync_recurring(db, owner, row)
    return row


def delete_job(db: Session, owner, job_id) -> None:
    """Delete a job, its changes and payslips. Its net pay item is archived (its
    confirmed history stays)."""
    owner = _require_owner(owner)
    row = get_job(db, owner, job_id)
    item = _recurring_for(db, owner, row.id)
    if item is not None:
        item.archived = True
        item.job_id = None
    db.query(FinanceJobChange).filter(FinanceJobChange.job_id == row.id).delete(synchronize_session=False)
    db.query(FinancePayslip).filter(FinancePayslip.job_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.flush()


def list_jobs(db: Session, owner, *, include_archived=False) -> list[dict]:
    owner = _require_owner(owner)
    query = db.query(FinanceJob).filter(FinanceJob.owner == owner)
    if not include_archived:
        query = query.filter(FinanceJob.archived.is_(False))
    return [job_to_dict(db, owner, r) for r in query.order_by(FinanceJob.employer)]


# ── Changes ─────────────────────────────────────────────────────────────


def _change_fields(db: Session, owner: str, c: FinanceJobChange, data: dict) -> None:
    kind = str(data.get("kind") or c.kind or "").strip().lower().replace(" ", "_")
    kind = {"pay_rise": "raise", "rise": "raise", "new_salary": "salary", "overtime": "allowance",
            "pension": "pension_percent", "code": "tax_code"}.get(kind, kind)
    if kind not in payroll.CHANGE_KINDS:
        raise FinanceError("change must be a pay rise, new salary, bonus, allowance, salary sacrifice, tax code, "
                           "pension change, hours change or job end")
    c.kind = kind
    c.start_date = _date(data.get("start_date", c.start_date), "start date", required=True)
    c.end_date = _date(data.get("end_date", c.end_date), "end date")
    if c.end_date and c.end_date < c.start_date:
        raise FinanceError("the end date must be after the start date")
    c.repeat = "yearly" if str(data.get("repeat", c.repeat or "once")).lower() in ("yearly", "true", "1", "annual",
                                                                                     "every year") else "once"
    c.amount_pence = c.amount_pence or 0
    c.percent_bp = c.percent_bp or 0
    c.label = c.label or ""
    if kind in ("salary", "bonus", "allowance", "sacrifice"):
        c.amount_pence = _pounds(data.get("amount", c.amount_pence / 100 if c.amount_pence else None),
                                 "amount", allow_zero=False)
    if kind == "raise":
        c.percent_bp = _percent(data.get("percent", c.percent_bp / 100 if c.percent_bp else None), "pay rise",
                                low=-5000, high=10000)
    if kind in ("pension_percent", "employer_percent"):
        c.percent_bp = _percent(data.get("percent", c.percent_bp / 100), "pension %")
    if kind == "hours":
        c.percent_bp = _percent(data.get("percent", c.percent_bp / 100 if c.percent_bp else None), "hours %",
                                low=100, high=10000)
    if kind == "tax_code":
        text = str(data.get("label", c.label) or "").strip().upper()
        payroll.parse_tax_code(text)
        c.label = re.sub(r"\s+", " ", text)
    elif kind == "pension_method":
        c.label = _method(data.get("label", c.label))
    elif data.get("label") is not None:
        c.label = _clean_text(data.get("label"), field="label", max_len=80)
    if kind not in ("raise", "bonus"):
        c.repeat = "once"


def add_change(db: Session, owner, job_id, **data) -> FinanceJobChange:
    owner = _require_owner(owner)
    job = get_job(db, owner, job_id)
    c = FinanceJobChange(id=_new_id(), owner=owner, job_id=job.id, kind="", amount_pence=0, percent_bp=0,
                         repeat="once", label="")
    _change_fields(db, owner, c, data)
    db.add(c)
    db.flush()
    sync_recurring(db, owner, job)
    return c


def update_change(db: Session, owner, change_id, **data) -> FinanceJobChange:
    owner = _require_owner(owner)
    c = db.query(FinanceJobChange).filter(FinanceJobChange.owner == owner,
                                          FinanceJobChange.id == str(change_id or "")).first()
    if c is None:
        raise FinanceNotFound("change not found")
    _change_fields(db, owner, c, data)
    db.flush()
    sync_recurring(db, owner, get_job(db, owner, c.job_id))
    return c


def delete_change(db: Session, owner, change_id) -> None:
    owner = _require_owner(owner)
    c = db.query(FinanceJobChange).filter(FinanceJobChange.owner == owner,
                                          FinanceJobChange.id == str(change_id or "")).first()
    if c is None:
        raise FinanceNotFound("change not found")
    job = get_job(db, owner, c.job_id)
    db.delete(c)
    db.flush()
    sync_recurring(db, owner, job)


# ── The net pay recurring item ─────────────────────────────────────────


def _recurring_for(db: Session, owner: str, job_id: str) -> Optional[FinanceRecurring]:
    return db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                             FinanceRecurring.job_id == job_id).first()


def _salary_category(db: Session, owner: str) -> Optional[str]:
    row = db.query(FinanceCategory).filter(FinanceCategory.owner == owner, FinanceCategory.kind == "income",
                                           FinanceCategory.name.ilike("salary")).first()
    return row.id if row else None


def sync_recurring(db: Session, owner: str, row: FinanceJob, *, today: Optional[date] = None) -> Optional[FinanceRecurring]:
    """Create or update the job's calculated net pay item (archived when the job
    has no pay account, is archived or has ended)."""
    today = today or _today_for_owner()
    item = _recurring_for(db, owner, row.id)
    job = snapshot(db, owner, row)
    upcoming = payroll.next_period(job, today)
    active = bool(row.account_id) and not row.archived and upcoming is not None
    if item is None:
        if not active:
            return None
        item = FinanceRecurring(id=_new_id(), owner=owner, job_id=row.id, direction="in")
        db.add(item)
    item.name = f"{row.employer} pay"[:MAX_NAME_LEN]
    item.account_id = row.account_id or item.account_id
    item.direction = "in"
    item.frequency = row.frequency
    item.anchor_date = row.pay_anchor
    item.end_date = min((d for d in (row.end_date, payroll._stop_date(job)) if d), default=None)
    if item.end_date and item.end_date < item.anchor_date:
        item.end_date = item.anchor_date
    item.amount_pence = max(1, upcoming["net_pence"]) if upcoming else max(1, item.amount_pence or 1)
    item.match_text = row.employer
    item.category_id = _salary_category(db, owner)
    item.liability_id = item.asset_id = item.to_account_id = None
    item.archived = not active
    if item.account_id is None:
        db.expunge(item)
        return None
    db.flush()
    return item


def recalculate_all(db: Session, owner) -> None:
    """Refresh every job's net pay item (e.g. after a new tax year)."""
    owner = _require_owner(owner)
    for row in db.query(FinanceJob).filter(FinanceJob.owner == owner).all():
        sync_recurring(db, owner, row)


def net_pay_on(db: Session, owner: str, job_id: str, day: date, *, cache: Optional[dict] = None) -> Optional[int]:
    """Calculated net pay for a pay date (a payslip's net wins if one was entered)."""
    key = (job_id, day)
    if cache is not None and key in cache:
        return cache[key]
    row = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.id == job_id).first()
    value = None
    if row is not None:
        slip = db.query(FinancePayslip).filter(FinancePayslip.job_id == row.id, FinancePayslip.pay_date == day,
                                               FinancePayslip.net_pence.isnot(None)).first()
        if slip is not None:
            value = slip.net_pence
        else:
            period = payroll.period_on(snapshot(db, owner, row), day)
            value = period["net_pence"] if period else None
    if cache is not None:
        cache[key] = value
    return value


def _payroll_note(row: FinanceJob, due: date) -> str:
    return f"{PAYROLL_NOTE} {due.isoformat()} ({row.employer})"[:200]


def on_pay_confirmed(db: Session, owner: str, item: FinanceRecurring, due: date) -> None:
    """Record the pay date's pension contributions and student loan payments."""
    row = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.id == item.job_id).first()
    if row is None:
        return
    period = payroll.period_on(snapshot(db, owner, row), due)
    if period is None:
        return
    slip = db.query(FinancePayslip).filter(FinancePayslip.job_id == row.id, FinancePayslip.pay_date == due).first()
    note = _payroll_note(row, due)
    pension = period["pension_employee_pence"] + period["pension_employer_pence"]
    if row.pension_asset_id and pension > 0 and not db.query(FinanceContribution.id).filter(
            FinanceContribution.owner == owner, FinanceContribution.asset_id == row.pension_asset_id,
            FinanceContribution.note == note).first():
        db.add(FinanceContribution(id=_new_id(), owner=owner, asset_id=row.pension_asset_id, date=due,
                                   amount_pence=pension, note=note))
    ug = period["student_loan_pence"]
    pg = period["postgraduate_pence"]
    if slip is not None and slip.student_loan_pence is not None:
        # The payslip shows the real total; keep the postgraduate share as calculated.
        ug = max(0, slip.student_loan_pence - pg)
    for liability_id, amount in ((row.sl_liability_id, ug), (row.pg_liability_id, pg)):
        if liability_id and amount > 0 and not db.query(FinanceRepayment.id).filter(
                FinanceRepayment.owner == owner, FinanceRepayment.liability_id == liability_id,
                FinanceRepayment.note == note).first():
            db.add(FinanceRepayment(id=_new_id(), owner=owner, liability_id=liability_id, date=due,
                                    amount_pence=amount, note=note))
    db.flush()


def on_pay_undone(db: Session, owner: str, item: FinanceRecurring, due: date) -> None:
    row = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.id == item.job_id).first()
    if row is None:
        return
    note = _payroll_note(row, due)
    db.query(FinanceContribution).filter(FinanceContribution.owner == owner,
                                         FinanceContribution.note == note).delete(synchronize_session=False)
    db.query(FinanceRepayment).filter(FinanceRepayment.owner == owner,
                                      FinanceRepayment.note == note).delete(synchronize_session=False)
    db.flush()


# ── Payslips ────────────────────────────────────────────────────────────


def payslip_to_dict(p: FinancePayslip) -> dict:
    return {"id": p.id, "job_id": p.job_id, "pay_date": p.pay_date.isoformat(), "gross_pence": p.gross_pence,
            "tax_pence": p.tax_pence, "ni_pence": p.ni_pence, "pension_pence": p.pension_pence,
            "student_loan_pence": p.student_loan_pence, "net_pence": p.net_pence, "tax_code": p.tax_code or "",
            "notes": p.notes or ""}


def add_payslip(db: Session, owner, job_id, *, pay_date, gross=None, tax=None, ni=None, pension=None,
                student_loan=None, net=None, tax_code="", notes="") -> FinancePayslip:
    owner = _require_owner(owner)
    job = get_job(db, owner, job_id)
    day = _date(pay_date, "pay date", required=True)
    values = {}
    for key, value in (("gross", gross), ("tax", tax), ("ni", ni), ("pension", pension),
                       ("student_loan", student_loan), ("net", net)):
        if value in (None, ""):
            values[key] = None
            continue
        if key == "tax":  # a tax refund shows as a negative amount
            try:
                values[key] = parse_pence(value)
            except MoneyError as exc:
                raise FinanceError(f"tax: {exc}") from exc
        else:
            values[key] = _pounds(value, key.replace("_", " "))
    if all(v is None for v in values.values()):
        raise FinanceError("enter at least one figure from the payslip")
    code = str(tax_code or "").strip().upper()
    if code:
        payroll.parse_tax_code(code)
    existing = db.query(FinancePayslip).filter(FinancePayslip.owner == owner, FinancePayslip.job_id == job.id,
                                               FinancePayslip.pay_date == day).first()
    row = existing or FinancePayslip(id=_new_id(), owner=owner, job_id=job.id, pay_date=day)
    row.gross_pence, row.tax_pence, row.ni_pence = values["gross"], values["tax"], values["ni"]
    row.pension_pence, row.student_loan_pence, row.net_pence = values["pension"], values["student_loan"], values["net"]
    row.tax_code = re.sub(r"\s+", " ", code)
    row.notes = _clean_text(notes or "", field="notes", max_len=500)
    if existing is None:
        db.add(row)
    db.flush()
    return row


def delete_payslip(db: Session, owner, payslip_id) -> None:
    owner = _require_owner(owner)
    row = db.query(FinancePayslip).filter(FinancePayslip.owner == owner,
                                          FinancePayslip.id == str(payslip_id or "")).first()
    if row is None:
        raise FinanceNotFound("payslip not found")
    db.delete(row)
    db.flush()


def check_payslip(db: Session, owner, payslip_id=None, *, job_id=None) -> Optional[dict]:
    """Compare a payslip (the latest for the job when no id) with the calculation."""
    owner = _require_owner(owner)
    query = db.query(FinancePayslip).filter(FinancePayslip.owner == owner)
    if payslip_id:
        query = query.filter(FinancePayslip.id == str(payslip_id))
    elif job_id:
        query = query.filter(FinancePayslip.job_id == job_id)
    slip = query.order_by(FinancePayslip.pay_date.desc()).first()
    if slip is None:
        if payslip_id:
            raise FinanceNotFound("payslip not found")
        return None
    row = get_job(db, owner, slip.job_id)
    job = snapshot(db, owner, row)
    result = payroll.compare_payslip(job, {
        "date": slip.pay_date, "gross": slip.gross_pence, "tax": slip.tax_pence, "ni": slip.ni_pence,
        "pension": slip.pension_pence, "student_loan": slip.student_loan_pence, "net": slip.net_pence})
    if slip.tax_code and payroll.parse_tax_code(slip.tax_code).raw.replace(" ", "") != \
            (payroll.tax_code_at(job, slip.pay_date) or "").replace(" ", ""):
        result["causes"].insert(0, f"The payslip shows tax code {slip.tax_code} but your setup uses "
                                   f"{payroll.tax_code_at(job, slip.pay_date) or 'none'}: update the tax code.")
    result["payslip"] = payslip_to_dict(slip)
    result["employer"] = row.employer
    return result


# ── Overview and warnings ──────────────────────────────────────────────


def salary_warnings(db: Session, owner, *, today: Optional[date] = None) -> list[dict]:
    from src.finance.projections import _warning

    owner = _require_owner(owner)
    today = today or _today_for_owner()
    out = []
    jobs = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.archived.is_(False)).all()
    regulars = db.query(FinanceRecurring).filter(FinanceRecurring.owner == owner,
                                                 FinanceRecurring.archived.is_(False)).all()
    salary_category = _salary_category(db, owner)
    for row in jobs:
        job = snapshot(db, owner, row)
        name = row.employer
        code_text = payroll.tax_code_at(job, today)
        if not code_text:
            out.append(_warning("no_tax_code", f"{name}: no tax code entered, so 1257L is assumed. Copy the code "
                                "from your payslip.", "job", row.id))
        else:
            code = payroll.parse_tax_code(code_text)
            if not code.cumulative:
                started = row.start_date or (row.created_at.date() if row.created_at else today)
                since = max([c.start for c in job.changes if c.kind == "tax_code" and c.start <= today] or [started])
                if (today - since).days > EMERGENCY_GRACE_DAYS:
                    out.append(_warning("emergency_code", f"{name}: the emergency code {code_text} has been in use for "
                                        "over two months. Check with HMRC that your code has been updated.",
                                        "job", row.id))
        if not row.account_id:
            out.append(_warning("no_pay_account", f"{name}: choose the account your pay goes into, so it shows in "
                                "Upcoming and projections.", "job", row.id))
        if row.pension_method != "none" and not row.pension_asset_id:
            out.append(_warning("no_pension_asset", f"{name}: link your workplace pension to a pension asset so "
                                "contributions are added to it.", "job", row.id))
        if (row.student_plans and not row.sl_liability_id) or (row.postgraduate and not row.pg_liability_id):
            out.append(_warning("sl_unlinked", f"{name}: a student loan plan is set but no student loan is linked, "
                                "so deductions don't reduce a balance.", "job", row.id))
        for r in regulars:
            if r.job_id or r.to_account_id or r.direction != "in":
                continue
            words = set(re.findall(r"[a-z]{3,}", f"{r.name} {r.match_text}".lower()))
            employer_words = set(re.findall(r"[a-z]{3,}", name.lower())) - {"ltd", "limited", "plc", "the"}
            if (r.category_id and r.category_id == salary_category) or words & {"salary", "wage", "wages", "payroll"} \
                    or words & employer_words:
                out.append({**_warning("duplicate_salary", f"'{r.name}' looks like a hand-entered salary that "
                                       f"duplicates the calculated {name} pay. Replace it to avoid counting pay "
                                       "twice.", "recurring", r.id), "action": "replace", "job_id": row.id})
        if row.pension_asset_id:
            for r in regulars:
                if r.asset_id == row.pension_asset_id and not r.job_id:
                    out.append(_warning("duplicate_pension", f"'{r.name}' adds to the same pension as {name}'s "
                                        "payroll contributions; projections leave it out to avoid counting twice.",
                                        "recurring", r.id))
        salary = payroll.salary_at(job, today)
        r = tax_rules.rules_for(today)
        taper = r["personal_allowance"]["taper_from"]
        if taper - NEAR_THRESHOLD <= salary < taper:
            out.append(_warning("near_taper", f"{name}: your salary is within £5,000 of £100,000, where you start "
                                "to lose your personal allowance (a 60% tax band). Pension contributions can keep "
                                "you below it.", "job", row.id))
        for plan in job.student_plans:
            limit = r["student_loans"].get(plan, {}).get("annual")
            if limit and limit - NEAR_THRESHOLD <= salary < limit:
                out.append(_warning("near_sl_threshold", f"{name}: your salary is just under the "
                                    f"{plan.replace('_', ' ').title()} repayment threshold; a pay rise will start "
                                    "student loan deductions.", "job", row.id))
    out += _not_modelled(db, owner, today, regulars, bool(jobs))
    return out


def _not_modelled(db: Session, owner: str, today: date, regulars, has_jobs: bool) -> list[dict]:
    from src.finance.projections import _warning

    since = today - timedelta(days=365)
    text = " ".join(f"{r.name} {r.match_text}" for r in regulars).lower() + " " + " ".join(
        (row[0] or "").lower() for row in db.query(FinanceTransaction.description).filter(
            FinanceTransaction.owner == owner, FinanceTransaction.direction == "in",
            FinanceTransaction.date >= since).limit(2000))
    found = []
    checks = (
        ("dividends", r"\bdividends?\b", "Dividend income"),
        ("self_employment", r"\b(?:self[- ]?employ\w*|sole trader|self assessment)\b", "Self-employment income"),
        ("universal_credit", r"\buniversal credit\b|\bdwp uc\b", "Universal Credit's earnings taper"),
        ("child_benefit", r"\bchild benefit\b|\bchb\b", "The High Income Child Benefit Charge"),
    )
    for kind, pattern, label in checks:
        if re.search(pattern, text) and (kind != "child_benefit" or has_jobs):
            found.append(_warning(f"not_modelled_{kind}", f"{label} looks relevant to your data but isn't modelled "
                                  "yet, so take-home and projections may be off.", None, None))
    return found


def salary_overview(db: Session, owner, *, today: Optional[date] = None) -> dict:
    """Everything the Salary section shows."""
    owner = _require_owner(owner)
    today = today or _today_for_owner()
    year = tax_rules.tax_year_start(today).year
    rules = tax_rules.rules_for(year)
    jobs = []
    for row in db.query(FinanceJob).filter(FinanceJob.owner == owner).order_by(
            FinanceJob.archived, FinanceJob.employer):
        job = snapshot(db, owner, row)
        try:
            code = payroll.parse_tax_code(payroll.tax_code_at(job, today) or "1257L")
            code_info = {"code": code.raw, "explanation": code.explanation, "notes": code.notes,
                         "region": job.region or code.region, "cumulative": code.cumulative}
        except FinanceError as exc:
            code_info = {"code": payroll.tax_code_at(job, today), "error": str(exc)}
        periods = payroll.calc_tax_year(job, year)
        jobs.append({
            **job_to_dict(db, owner, row),
            "next_period": payroll.next_period(job, today),
            "summary": payroll.annual_summary(job, year),
            "year_to_date": next((p["ytd"] for p in reversed(periods) if p["date"] <= today.isoformat()), None),
            "tax_code_info": code_info,
            "payslips": [payslip_to_dict(p) for p in db.query(FinancePayslip).filter(
                FinancePayslip.job_id == row.id).order_by(FinancePayslip.pay_date.desc()).limit(24)],
            "latest_check": check_payslip(db, owner, job_id=row.id),
        })
    return {"today": today.isoformat(), "tax_year": rules["label"], "rules_assumed": rules["assumed"],
            "rules_source": rules["income_tax"]["source"], "rules_checked": rules["income_tax"]["checked"],
            "jobs": jobs, "warnings": salary_warnings(db, owner, today=today),
            "frequencies": payroll.FREQUENCY_LABELS, "pension_methods": payroll.PENSION_METHOD_LABELS,
            "regions": payroll.REGION_LABELS, "student_plans": list(tax_rules.STUDENT_PLANS),
            "ni_categories": sorted(rules["ni"]["categories"])}


def replace_manual_salary(db: Session, owner, recurring_id) -> FinanceRecurring:
    """Archive a hand-entered salary item that duplicates calculated pay."""
    from src.finance.recurring import get_recurring

    owner = _require_owner(owner)
    item = get_recurring(db, owner, recurring_id)
    if item.job_id:
        raise FinanceError("that's the calculated pay item; it can't be replaced")
    item.archived = True
    db.flush()
    return item


# ── What-ifs (for the salary tool and projection scenarios) ────────────


def normalise_salary_change(db: Session, owner: str, raw: dict) -> dict:
    """A salary what-if: {action, job_id?, from?, percent?, amount?, label?, repeat?}."""
    owner = _require_owner(owner)
    action = str(raw.get("action") or "").strip().lower().replace(" ", "_")
    action = {"pay_rise": "raise", "rise": "raise", "new_salary": "salary", "new_job": "salary",
              "pension": "pension_percent", "method": "pension_method", "reduced_hours": "hours",
              "stop_work": "stop", "code": "tax_code"}.get(action, action)
    if action not in SALARY_ACTIONS:
        raise FinanceError(f"salary change must be one of: {', '.join(SALARY_ACTIONS)}")
    job = find_job(db, owner, raw.get("job_id") or raw.get("job") or None)
    out = {"action": action, "job_id": job.id,
           "from": _date(raw.get("from"), "from") or _today_for_owner()}
    if action in ("salary", "bonus"):
        out["amount"] = _pounds(raw.get("amount"), "amount", allow_zero=False)
    if action == "raise":
        if raw.get("percent") not in (None, ""):
            out["percent_bp"] = _percent(raw.get("percent"), "pay rise", low=-5000, high=10000)
        else:
            # A rise in pounds a year becomes a new salary.
            rise = _pounds(raw.get("amount"), "pay rise", allow_zero=False)
            out["action"] = "salary"
            out["amount"] = payroll.salary_at(snapshot(db, owner, job), out["from"]) + rise
            out["rise_pence"] = rise
    if action in ("pension_percent", "hours"):
        out["percent_bp"] = _percent(raw.get("percent"), "percentage", low=0 if action != "hours" else 100)
    if action == "pension_method":
        out["label"] = _method(raw.get("label") or raw.get("method"))
    if action == "tax_code":
        text = str(raw.get("label") or raw.get("tax_code") or "").strip().upper()
        payroll.parse_tax_code(text)
        out["label"] = text
    out["repeat"] = "yearly" if str(raw.get("repeat") or "").lower() == "yearly" else "once"
    return out


def salary_change_to_job_change(c: dict) -> payroll.JobChange:
    start = c["from"] if isinstance(c["from"], date) else date.fromisoformat(c["from"])
    return payroll.JobChange(c["action"], start, None, c.get("amount", 0), c.get("percent_bp", 0),
                             c.get("repeat", "once"), c.get("label", ""))


def describe_salary_change(c: dict, names: Optional[dict] = None) -> str:
    names = names or {}
    start = c["from"] if isinstance(c["from"], date) else date.fromisoformat(c["from"])
    when = start.strftime("%b %Y")
    who = names.get(c.get("job_id"), "your job")
    action = c["action"]
    if action == "salary" and c.get("rise_pence"):
        return f"£{c['rise_pence'] / 100:,.0f} a year pay rise at {who} from {when}"
    return {
        "raise": f"{c.get('percent_bp', 0) / 100:g}% pay rise at {who} from {when}",
        "salary": f"Salary of £{c.get('amount', 0) / 100:,.0f} at {who} from {when}",
        "bonus": f"£{c.get('amount', 0) / 100:,.0f} bonus from {who} in {when}",
        "pension_percent": f"Pension at {c.get('percent_bp', 0) / 100:g}% at {who} from {when}",
        "pension_method": f"Pension by {payroll.PENSION_METHOD_LABELS.get(c.get('label'), c.get('label'))} from {when}",
        "hours": f"Hours at {c.get('percent_bp', 0) / 100:g}% at {who} from {when}",
        "stop": f"Stop working at {who} from {when}",
        "tax_code": f"Tax code {c.get('label')} from {when}",
    }[action]


# ── Projections ─────────────────────────────────────────────────────────


class _PayrollHook:
    """Called by the projection engine each month: pays net salary into the
    pay account, adds pension contributions to the linked pension and takes
    student loan deductions off the linked loan."""

    def __init__(self, jobs: list):
        self.jobs = jobs

    def __call__(self, engine, start: date, end: date, f: dict) -> None:
        state = engine.__dict__.setdefault("_payroll", {"cache": {}, "rows": [], "jobs": None})
        if state["jobs"] is None:
            extra: dict = {}
            for c in engine.changes:
                if c["type"] == "salary":
                    extra.setdefault(c["job_id"], []).append(salary_change_to_job_change(c))
            state["jobs"] = [payroll.with_changes(j, extra.get(j.id)) for j in self.jobs]
        for job in state["jobs"]:
            cache = state["cache"].setdefault(job.id, {})
            for day in payroll.pay_dates(job, start + timedelta(days=1), end):
                year = tax_rules.tax_year_start(day).year
                if year not in cache:
                    cache[year] = {p["date"]: p for p in payroll.calc_tax_year(job, year)}
                p = cache[year].get(day.isoformat())
                if p is None:
                    continue
                net = p["net_pence"]
                repaid = 0
                for plan, amount in p["student_loans"].items():
                    lid = job.pg_liability_id if plan == "postgraduate" else job.sl_liability_id
                    if not lid or lid not in engine.balance or amount <= 0:
                        continue
                    owed = engine.balance[lid]
                    taken = min(amount, owed)
                    net += amount - taken  # deductions stop once the loan is repaid
                    engine.balance[lid] = owed - taken
                    repaid += taken
                    if owed > 0 and owed - taken == 0:
                        engine.end_loan(lid, day, "repaid")
                engine.pool_add(job.account_id, net)
                f["income"] += net
                f["payroll_loan_repaid"] += repaid
                for key, field in (("salary_gross", "gross_pence"), ("tax", "tax_pence"), ("ni", "ni_pence"),
                                   ("pension_employee", "pension_employee_pence"),
                                   ("pension_employer", "pension_employer_pence"),
                                   ("pension_relief", "pension_relief_pence")):
                    f[key] += p[field]
                f["student_loan"] += repaid
                pension = p["pension_employee_pence"] + p["pension_employer_pence"]
                aid = job.pension_asset_id
                if pension and aid and aid in engine.asset_value and aid not in engine.sold:
                    engine.asset_value[aid] += pension
                    f["external_contributions"] += pension
                    f["asset_contributions"][aid] = f["asset_contributions"].get(aid, 0) + pension
                state["rows"].append({**{k: p[k] for k in (
                    "date", "tax_year", "rules_assumed", "gross_pence", "tax_pence", "ni_pence",
                    "pension_employee_pence", "pension_employer_pence", "pension_relief_pence",
                    "employer_ni_pence")},
                    "student_loan_pence": p["student_loan_pence"] + p["postgraduate_pence"],
                    "net_pence": net, "job_id": job.id})


def attach_payroll(db: Session, owner: str, inputs) -> None:
    rows = db.query(FinanceJob).filter(FinanceJob.owner == owner, FinanceJob.archived.is_(False)).order_by(
        FinanceJob.employer, FinanceJob.id).all()
    if not rows:
        return
    inputs.jobs = [snapshot(db, owner, r) for r in rows]
    inputs.payroll = _PayrollHook(inputs.jobs)
    inputs.warnings.extend(salary_warnings(db, owner, today=inputs.today))
    # A job with calculated pay means "no recurring income" no longer applies.
    inputs.warnings[:] = [w for w in inputs.warnings if w["kind"] != "no_income"]


def income_by_tax_year(engine) -> list[dict]:
    """Per tax year: gross, tax, NI, student loan, pension and net pay from the projection."""
    rows = engine.__dict__.get("_payroll", {}).get("rows", [])
    out: dict = {}
    for r in rows:
        y = out.setdefault(r["tax_year"], {"tax_year": r["tax_year"], "assumed": r["rules_assumed"], "periods": 0,
                                           **{k: 0 for k in ("gross_pence", "tax_pence", "ni_pence",
                                                             "student_loan_pence", "pension_employee_pence",
                                                             "pension_employer_pence", "pension_relief_pence",
                                                             "employer_ni_pence", "net_pence")}})
        y["periods"] += 1
        for k in ("gross_pence", "tax_pence", "ni_pence", "student_loan_pence", "pension_employee_pence",
                  "pension_employer_pence", "pension_relief_pence", "employer_ni_pence", "net_pence"):
            y[k] += r[k]
    return [out[k] for k in sorted(out)]
