"""Projection and salary agent tools.

- ``project_finances`` (read): runs the projection engine, optionally with a
  saved scenario or one-off what-ifs, and returns a compact yearly table,
  events, assumptions and warnings.
- ``manage_projection_scenario`` (list is a read; save/delete are writes).
- ``query_salary`` (read): payslip breakdown, annual summary, marginal rate,
  year to date and the latest payslip check, with optional what-ifs.
- ``update_salary`` (write): pay rises, new salary, tax code and pension
  changes; the result says what changed downstream.

Every figure comes from ``src.finance.projections`` or ``src.finance.payroll``.
The text tells the model to quote figures as given and never recalculate.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from src.finance import service as fin
from src.finance.matching import match_name
from src.finance.money import format_pence
from src.tools._common import _parse_tool_args
from src.tools.finance import _err, _first, _session, _today

QUOTE_RULE = ("These figures are calculated by the finance engine and already reconcile. Quote them as given. "
              "Do not recalculate, adjust or add up figures yourself. If the user asks for something not covered, "
              "call this tool again with different options. Use the exact amounts shown (to the penny) and don't "
              "describe assets, income or changes that aren't listed above.")
SALARY_RULE = ("These figures are calculated by the payroll engine using HMRC rates for each tax year. Quote them as "
               "given. Do not recalculate, adjust or add up figures yourself. For a different what-if, call this "
               "tool again with different options.")


def _money(pence: int) -> str:
    return format_pence(pence)


def _signed(pence: int) -> str:
    return ("+" if pence >= 0 else "-") + format_pence(abs(pence))


def _parse_when(value, *, field: str) -> Optional[date]:
    """A date, 'YYYY-MM' or a bare year (1 January, so 'in 2030' finds the year row ending in 2030)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) == 4:
        return date(int(text), 1, 1)
    if len(text) == 7 and text[4] == "-":
        return date(int(text[:4]), int(text[5:7]), 1)
    from src.tools.finance import _parse_day

    return _parse_day(text, field=field)


def _items(db, owner):
    from src.finance import networth as nw

    return (nw.list_items(db, owner, "asset", include_archived=False)
            + nw.list_items(db, owner, "liability", include_archived=False))


def _find_item(db, owner, name, *, kind=None):
    """Match an asset or liability by name; (item, None) or (None, error)."""
    from src.finance import networth as nw

    pool = nw.list_items(db, owner, kind) if kind else _items(db, owner)
    match = match_name(name, pool)
    if match.ok:
        return match.item, None
    label = {"asset": "asset", "liability": "debt"}.get(kind, "asset or debt")
    if match.status == "ambiguous":
        return None, _err(f"'{name}' matches more than one {label}: {', '.join(match.options[:12])}. Ask the user which.")
    return None, _err(f"No {label} called '{name}'. Existing: {', '.join(match.options[:15]) or 'none'}. Ask the user.")


# ── project_finances ───────────────────────────────────────────────────


def _what_ifs(db, owner, args: dict) -> tuple[list, Optional[Dict]]:
    """One-off what-if arguments -> scenario changes (not saved)."""
    changes: list = []
    extra = _first(args, "extra_monthly_income")
    if extra is not None:
        changes.append({"type": "extra_income", "amount": str(extra),
                        "from": _first(args, "from", "extra_income_from")})
    less = _first(args, "reduce_monthly_spending")
    if less is not None:
        changes.append({"type": "reduce_spending", "amount": str(less), "from": _first(args, "from")})
    overpay = _first(args, "overpay")
    if overpay is not None:
        target = _first(args, "overpay_item", "item", default="mortgage")
        item, error = _find_item(db, owner, target, kind="liability")
        if error:
            return [], error
        monthly = str(_first(args, "overpay_frequency", default="monthly")).lower() != "once"
        change = {"type": "overpay", "liability_id": item["id"], "amount": str(overpay), "monthly": monthly}
        when = _first(args, "overpay_on", "from")
        if monthly:
            change["from"] = when
        else:
            change["on"] = when or _today().isoformat()
        changes.append(change)
    sell = _first(args, "sell")
    if sell is not None:
        item, error = _find_item(db, owner, sell, kind="asset")
        if error:
            return [], error
        change = {"type": "sell", "asset_id": item["id"], "on": _first(args, "sell_on", default=_today().isoformat()),
                  "price": _first(args, "sell_price")}
        clears = _first(args, "sell_clears")
        if clears is not None:
            debt, error = _find_item(db, owner, clears, kind="liability")
            if error:
                return [], error
            change["clear_liability_id"] = debt["id"]
        changes.append(change)
    balloon = _first(args, "balloon_choice")
    if balloon is not None:
        from src.finance import loans

        target = _first(args, "balloon_item")
        if target is None:
            options = []
            for liability in _items(db, owner):
                schedule = loans.get_schedule(db, owner, liability["id"]) if liability["kind"] == "liability" else None
                if schedule is not None and schedule.balloon_pence:
                    options.append(liability)
            if len(options) != 1:
                return [], _err("Which car finance? " + (", ".join(o["name"] for o in options) or "No loan has a "
                                "balloon payment recorded.") + " Ask the user.")
            item = options[0]
        else:
            item, error = _find_item(db, owner, target, kind="liability")
            if error:
                return [], error
        changes.append({"type": "balloon", "liability_id": item["id"], "choice": balloon,
                        "months": _first(args, "refinance_months"), "rate": _first(args, "refinance_rate")})
    salary = _first(args, "salary")
    if salary is not None:
        changes.append({"type": "salary", "action": "salary", "amount": str(salary),
                        "from": _first(args, "salary_from", "from")})
    raise_by = _first(args, "pay_rise")
    if raise_by is not None:
        text = str(raise_by).strip()
        change = {"type": "salary", "action": "raise", "from": _first(args, "salary_from", "from")}
        if text.endswith("%"):
            change["percent"] = text.rstrip("%")
        else:
            change["amount"] = text
        changes.append(change)
    if _first(args, "pension_percent") is not None:
        changes.append({"type": "salary", "action": "pension_percent", "percent": str(args["pension_percent"]),
                        "from": _first(args, "salary_from", "from")})
    if _first(args, "pension_method") is not None:
        changes.append({"type": "salary", "action": "pension_method", "label": str(args["pension_method"]),
                        "from": _first(args, "salary_from", "from")})
    if _first(args, "stop_work") not in (None, False, "false", "no"):
        changes.append({"type": "salary", "action": "stop", "from": _first(args, "stop_work_from", "from")})
    return changes, None


def _year_table(result: dict) -> list[str]:
    lines = ["Year (ends) | Net worth | Cash | Shortfall | Assets | Liabilities"]
    for y in result["years"]:
        lines.append(f"{y['label']} ({y['end_label']}) | {_money(y['net_worth_pence'])} | {_money(y['cash_pence'])} | "
                     f"{_money(y['shortfall_pence'])} | {_money(y['assets_pence'])} | {_money(y['liabilities_pence'])}")
    return lines


def _events(result: dict, limit: int = 12) -> list[str]:
    out = []
    for e in result["events"][:limit]:
        when = date.fromisoformat(e["date"]).strftime("%b %Y")
        out.append(f"- {when}: {e['text']}" + (f". {e['followup']}" if e.get("followup") else ""))
    if len(result["events"]) > limit:
        out.append(f"- …and {len(result['events']) - limit} more")
    return out


def _projection_text(out: dict, *, focus=None, at: Optional[date] = None) -> str:
    base = out["base"]
    lines = [f"Projection for the whole household, {out['years']} year{'s' if out['years'] != 1 else ''} from "
             f"{date.fromisoformat(base['today']).strftime('%d %b %Y')}, worked out month by month.",
             f"Net worth today: {_money(base['start']['net_worth_pence'])}."]
    scenario = out["scenarios"][0] if out["scenarios"] else None
    if scenario:
        lines.append(f"Scenario '{scenario['name']}': " + "; ".join(scenario["descriptions"]) + ".")
        lines.append("Year (ends) | Net worth (base) | Net worth (scenario) | Difference | Cash (scenario) | Shortfall (scenario)")
        for b, s in zip(base["years"], scenario["years"]):
            lines.append(f"{b['label']} ({b['end_label']}) | {_money(b['net_worth_pence'])} | {_money(s['net_worth_pence'])} | "
                         f"{_signed(s['net_worth_pence'] - b['net_worth_pence'])} | {_money(s['cash_pence'])} | "
                         f"{_money(s['shortfall_pence'])}")
    else:
        lines += _year_table(base)
    shown = scenario or base
    if focus is not None:
        lines.append(f"{focus['name']} by year" + (" (scenario)" if scenario else "") + ":")
        for y in shown["years"]:
            pool = y["assets"] + [{"id": l["id"], "value_pence": l["balance_pence"]} for l in y["liabilities"]]
            row = next((p for p in pool if p["id"] == focus["id"]), None)
            value = row["value_pence"] if row else 0
            lines.append(f"- {y['end_label']}: {_money(value)}" + ("" if row else " (sold or gone)"))
            equity = next((e for e in y["property_equity"] if e["asset_id"] == focus["id"]), None)
            if equity:
                lines[-1] += f" (equity {_money(equity['equity_pence'])})"
    if at is not None:
        from src.finance.projections import value_at

        if focus is not None:
            found = value_at(shown, focus["id"], at)
            if found:
                label = "balance" if found["kind"] == "liability" else "value"
                lines.append(f"At {date.fromisoformat(found['date']).strftime('%b %Y')} (the projection's year end "
                             f"nearest on or after {at.strftime('%d %b %Y')}): {focus['name']} {label} "
                             f"{_money(found['value_pence'])}.")
            else:
                lines.append(f"{focus['name']} is not in the projection by {at.isoformat()} (sold, paid off or gone).")
        else:
            row = next((y for y in shown["years"] if y["end"] >= at.isoformat()), None)
            if row:
                lines.append(f"At {row['end_label']} (the projection's year end nearest on or after "
                             f"{at.strftime('%d %b %Y')}): net worth "
                             f"{_money(row['net_worth_pence'])}, cash {_money(row['cash_pence'])}, shortfall "
                             f"{_money(row['shortfall_pence'])}.")
    last = shown["years"][-1] if shown["years"] else None
    if last is not None:
        owed = [(l["name"], l["balance_pence"]) for l in last["liabilities"] if l["balance_pence"] > 0]
        if last["cards_pence"] < 0:
            owed.append(("Credit cards", -last["cards_pence"]))
        if last["shortfall_pence"] > 0:
            owed.append((shown["shortfall"]["name"], last["shortfall_pence"]))
        cleared = [e for e in shown["events"] if e["kind"] in ("paid_off", "hand_back", "card_cleared")]
        if owed:
            lines.append(f"Debts still owed at {last['end_label']} (the end of this projection): "
                         + ", ".join(f"{name} {_money(v)}" for name, v in owed)
                         + ". Not debt free within this period; ask again with more years to see further.")
        elif cleared:
            when = date.fromisoformat(cleared[-1]["date"]).strftime("%b %Y")
            lines.append(f"Debt free: every debt is cleared by {when}.")
        else:
            lines.append("No debts in this projection.")
    sf = shown["shortfall"]
    if sf["runs_out"]:
        lines.append(f"Cash runs out in {date.fromisoformat(sf['runs_out']).strftime('%b %Y')}; the gap is tracked as "
                     f"'{sf['name']}', peaking at {_money(sf['peak_pence'])}.")
    events = _events(shown)
    lines.append("Events:" if events else "Events: none in this period.")
    lines += events
    if shown.get("income_by_tax_year"):
        lines.append("Income per tax year (gross | tax | NI | student loan | pension you | pension employer | net):")
        for r in shown["income_by_tax_year"]:
            lines.append(f"- {r['tax_year']}{' (rates assumed)' if r['assumed'] else ''}: {_money(r['gross_pence'])} | "
                         f"{_money(r['tax_pence'])} | {_money(r['ni_pence'])} | {_money(r['student_loan_pence'])} | "
                         f"{_money(r['pension_employee_pence'])} | {_money(r['pension_employer_pence'])} | "
                         f"{_money(r['net_pence'])}")
    lines.append("Assumptions:")
    lines += [f"- {a['label']}: {a['value']}" for a in base["assumptions"]]
    if base["warnings"]:
        lines.append("Data warnings:")
        lines += [f"- {w['text']}" for w in base["warnings"]]
    lines.append(QUOTE_RULE)
    return "\n".join(lines)


async def do_project_finances(content, owner: Optional[str] = None) -> Dict:
    """Read-only multi-year projection, optionally with a scenario or what-ifs."""
    from src.finance import projections as proj

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            at = _parse_when(_first(args, "at"), field="at")
            years = _first(args, "years")
            years = proj.parse_years(years) if years is not None else None
            if at is not None:
                today = _today()
                needed = max(1, (at.year - today.year) + (1 if (at.month, at.day) > (today.month, today.day) else 0))
                if needed > proj.MAX_YEARS:
                    return _err(f"{at.isoformat()} is more than {proj.MAX_YEARS} years away; projections go up to "
                                f"{proj.MAX_YEARS} years.")
                years = max(years or 0, needed) or None
            focus = None
            if _first(args, "item") is not None and _first(args, "overpay") is None:
                focus, error = _find_item(db, owner, args["item"])
                if error:
                    return error
            scenario_ids = []
            if _first(args, "scenario") is not None:
                try:
                    scenario_ids = [proj.find_scenario(db, owner, args["scenario"]).id]
                except fin.FinanceError as exc:
                    return _err(f"{exc}. Ask the user which saved scenario they mean.")
            changes, error = _what_ifs(db, owner, args)
            if error:
                return error
            out = proj.projection(db, owner, years=years, scenario_ids=scenario_ids, changes=changes or None)
            if scenario_ids and changes:
                # Show the ad-hoc what-if on top of the saved scenario as one comparison.
                out["scenarios"] = out["scenarios"][-1:]
            text = _projection_text(out, focus=focus, at=at)
            # Keep the structured data small: small local models get the whole tool result,
            # and the text above already carries every figure.
            base = out["base"]
            compact = lambda r: [{"end": y["end"], "net_worth_pence": y["net_worth_pence"],  # noqa: E731
                                  "cash_pence": y["cash_pence"], "shortfall_pence": y["shortfall_pence"]}
                                 for y in r["years"]]
            data = {"years": out["years"], "base": compact(base), "runs_out": base["shortfall"]["runs_out"]}
            if out["scenarios"]:
                data["scenario"] = {"name": out["scenarios"][0]["name"], "years": compact(out["scenarios"][0])}
            return {"response": text, "data": data, "exit_code": 0}
    except fin.FinanceError as exc:
        return _err(str(exc))
    except Exception:  # pragma: no cover - logged for diagnosis
        import logging

        logging.getLogger(__name__).exception("project_finances failed")
        return _err("The projection failed. Nothing was changed.")


# ── manage_projection_scenario ─────────────────────────────────────────


def _resolve_change_names(db, owner, change: dict) -> tuple[dict, Optional[Dict]]:
    """Accept 'liability'/'asset'/'clears' names in place of ids."""
    c = dict(change)
    for key, id_key, kind in (("liability", "liability_id", "liability"), ("loan", "liability_id", "liability"),
                              ("asset", "asset_id", "asset"), ("clears", "clear_liability_id", "liability")):
        if c.get(key) not in (None, "") and not c.get(id_key):
            item, error = _find_item(db, owner, c.pop(key), kind=kind)
            if error:
                return {}, error
            c[id_key] = item["id"]
    return c, None


async def do_manage_projection_scenario(content, owner: Optional[str] = None) -> Dict:
    """List, save or delete named projection scenarios."""
    from src.finance import projections as proj

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    action = str(_first(args, "action", default="list")).strip().lower()
    action = {"show": "list", "view": "list", "create": "save", "add": "save", "update": "save",
              "remove": "delete"}.get(action, action)
    if action not in ("list", "save", "delete"):
        return _err("action must be list, save or delete")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            if action == "list":
                items = proj.list_scenarios(db, owner)
                if not items:
                    return {"response": "No saved projection scenarios yet.", "data": {"scenarios": []}, "exit_code": 0}
                return {"response": "Saved scenarios:\n" + "\n".join(
                    f"- {s['name']}: {'; '.join(s['descriptions'])}" for s in items),
                    "data": {"scenarios": items}, "exit_code": 0}
            name = _first(args, "name", "scenario")
            if not name:
                return _err("Nothing saved. Say which scenario (its name). Ask the user.")
            if action == "delete":
                row = proj.find_scenario(db, owner, name)
                label = row.name
                proj.delete_scenario(db, owner, row.id)
                db.commit()
                return {"response": f"Deleted the scenario '{label}'.", "data": {"name": label}, "exit_code": 0}
            raw = args.get("changes")
            if not isinstance(raw, list) or not raw:
                return _err("Nothing saved. A scenario needs a list of changes, e.g. "
                            "[{\"type\": \"overpay\", \"liability\": \"mortgage\", \"amount\": \"200\", \"monthly\": true}].")
            changes = []
            for change in raw:
                resolved, error = _resolve_change_names(db, owner, change if isinstance(change, dict) else {})
                if error:
                    return _err(f"Nothing saved. {error['error']}")
                changes.append(resolved)
            existing = None
            try:
                existing = proj.find_scenario(db, owner, name)
                if existing.name.lower() != str(name).strip().lower():
                    existing = None
            except fin.FinanceError:
                existing = None
            row = proj.save_scenario(db, owner, name=name, changes=changes, notes=_first(args, "notes", default=""),
                                     scenario_id=existing.id if existing else None)
            saved = proj.scenario_to_dict(row, proj.item_names(db, owner))
            db.commit()
            return {"response": f"{'Updated' if existing else 'Saved'} the scenario '{saved['name']}': "
                                f"{'; '.join(saved['descriptions'])}. Use project_finances with scenario="
                                f"'{saved['name']}' to see it against the base case.",
                    "data": saved, "exit_code": 0}
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")


# ── query_salary ───────────────────────────────────────────────────────


def _period_lines(p: dict) -> list[str]:
    parts = [f"gross {_money(p['gross_pence'])}"]
    if p["pension_sacrifice_pence"]:
        parts.append(f"pension (salary sacrifice) -{_money(p['pension_sacrifice_pence'])}")
    if p["other_sacrifice_pence"]:
        parts.append(f"other salary sacrifice -{_money(p['other_sacrifice_pence'])}")
    parts.append(f"income tax -{_money(p['tax_pence'])}" if p["tax_pence"] >= 0 else
                 f"income tax refund +{_money(-p['tax_pence'])}")
    parts.append(f"NI -{_money(p['ni_pence'])}")
    loans = p["student_loan_pence"] + p["postgraduate_pence"]
    if loans:
        parts.append(f"student loan -{_money(loans)}")
    if p["pension_method"] in ("net_pay", "relief_at_source") and p["pension_deduction_pence"]:
        parts.append(f"pension -{_money(p['pension_deduction_pence'])}")
    parts.append(f"net pay {_money(p['net_pence'])}")
    extra = []
    if p["pension_employer_pence"]:
        extra.append(f"employer pension {_money(p['pension_employer_pence'])}")
    if p["pension_relief_pence"]:
        extra.append(f"20% tax relief added to the pension {_money(p['pension_relief_pence'])}")
    return [", ".join(parts) + "." + (f" Also: {', '.join(extra)}." if extra else "")]


def _summary_lines(s: dict) -> list[str]:
    if not s.get("periods"):
        return []
    lines = [f"{s['tax_year']} summary{' (rates assumed)' if s['rules_assumed'] else ''}: take-home "
             f"{_money(s['net_pence'])} ({_money(s['take_home_monthly_pence'])} a month on average) from gross "
             f"{_money(s['gross_pence'])}; income tax {_money(s['tax_pence'])}, NI {_money(s['ni_pence'])}, "
             f"student loan {_money(s['student_loan_pence'] + s['postgraduate_pence'])}; effective rate "
             f"{s['effective_rate_percent']}%.",
             f"Marginal rate on the next £1: {s['marginal_tax_percent']}% income tax, {s['marginal_rate_percent']}% "
             "with NI and student loan." + (" This is the personal allowance taper (60% band)." if s["taper_band"] else "")]
    if abs(s["paye_difference_pence"]) > 100:
        lines.append(f"PAYE on this tax code collects {_money(abs(s['paye_difference_pence']))} "
                     f"{'less' if s['paye_difference_pence'] > 0 else 'more'} than the tax actually owed for the year.")
    if s["into_pension_pence"]:
        lines.append(f"Into the pension this tax year: {_money(s['into_pension_pence'])}; it costs "
                     f"{_money(s['pension_cost_pence'])} of take-home after tax and NI relief.")
    if s["pension_relief_claimable_pence"]:
        lines.append(f"Higher-rate pension relief claimable via self assessment (not added automatically): "
                     f"{_money(s['pension_relief_claimable_pence'])}.")
    return lines


def _salary_what_ifs(job, args: dict, year: int, next_date: Optional[date]) -> tuple[list, list[str]]:
    from src.finance import payroll as pr
    from src.finance.projections import parse_rate

    start = date(year, 4, 6)
    changes, labels = [], []
    salary = _first(args, "salary")
    if salary is not None:
        from src.finance.money import parse_pence

        amount = parse_pence(salary)
        changes.append(pr.JobChange("salary", start, amount=amount))
        labels.append(f"salary of {_money(amount)}")
    raise_by = _first(args, "pay_rise")
    if raise_by is not None:
        text = str(raise_by).strip()
        if text.endswith("%"):
            bp = parse_rate(text, field="pay rise", low=-5000, high=10000)
            changes.append(pr.JobChange("raise", start, percent_bp=bp))
            labels.append(f"a {bp / 100:g}% pay rise")
        else:
            from src.finance.money import parse_pence

            rise = parse_pence(text)
            changes.append(pr.JobChange("salary", start, amount=pr.salary_at(job, start) + rise))
            labels.append(f"a {_money(rise)} a year pay rise")
    if _first(args, "pension_percent") is not None:
        bp = parse_rate(args["pension_percent"], field="pension %", low=0, high=10000)
        changes.append(pr.JobChange("pension_percent", start, percent_bp=bp))
        labels.append(f"pension at {bp / 100:g}%")
    if _first(args, "pension_method") is not None:
        from src.finance.payroll_jobs import _method

        method = _method(args["pension_method"])
        changes.append(pr.JobChange("pension_method", start, label=method))
        if method != "none" and job.pension_method == "none" and _first(args, "pension_percent") is None:
            labels.append("(no pension % set; add pension_percent)")
        labels.append(f"pension by {pr.PENSION_METHOD_LABELS[method].lower()}")
    if _first(args, "bonus") is not None:
        from src.finance.money import parse_pence

        amount = parse_pence(args["bonus"])
        changes.append(pr.JobChange("bonus", next_date or start, amount=amount))
        labels.append(f"a {_money(amount)} bonus in the next pay")
    if _first(args, "tax_code") is not None:
        code = pr.parse_tax_code(args["tax_code"])
        changes.append(pr.JobChange("tax_code", start, label=code.raw))
        labels.append(f"tax code {code.raw}")
    return changes, labels


async def do_query_salary(content, owner: Optional[str] = None) -> Dict:
    """Read-only payslip breakdown, annual summary, year to date and payslip check, with what-ifs."""
    from src.finance import payroll as pr
    from src.finance import payroll_jobs as pj
    from src.finance import uk_tax_rules as rules

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            try:
                row = pj.find_job(db, owner, _first(args, "job", "employer"))
            except fin.FinanceNotFound:
                return _err("No salary is set up yet. Tell the user to add their job in Finance > Salary (employer, "
                            "gross salary, pay date, tax code, pension and student loan), then ask again.")
            except fin.FinanceError as exc:
                return _err(f"{exc}. Ask the user which job.")
            job = pj.snapshot(db, owner, row)
            today = _today()
            on = _parse_when(_first(args, "date", "pay_date"), field="date") if _first(args, "date", "pay_date") else None
            period = pr.period_on(job, on) if on else pr.next_period(job, today)
            if on and period is None:
                year_rows = pr.calc_tax_year(job, rules.tax_year_start(on).year)
                period = min(year_rows, key=lambda p: abs((date.fromisoformat(p["date"]) - on).days)) if year_rows else None
            year = rules.tax_year_start(date.fromisoformat(period["date"]) if period else today).year
            summary = pr.annual_summary(job, year)
            code_text = pr.tax_code_at(job, today) or "1257L"
            code = pr.parse_tax_code(code_text)
            lines = [f"Salary at {row.employer}: {_money(pr.salary_at(job, today))} a year, paid "
                     f"{pr.FREQUENCY_LABELS[row.frequency].lower()}, tax code {code.raw}"
                     f"{'' if row.tax_code else ' (assumed: none entered)'}, "
                     f"{pr.REGION_LABELS[job.region or code.region]} rates, NI category {row.ni_category}."]
            if period:
                lines.append(f"Pay on {date.fromisoformat(period['date']).strftime('%d %b %Y')} "
                             f"(tax {'month' if row.frequency == 'monthly' else 'week'} {period['tax_period']}):")
                lines += _period_lines(period)
                ytd = period["ytd"]
                lines.append(f"Tax year to date after that pay: gross {_money(ytd['gross_pence'])}, tax "
                             f"{_money(ytd['tax_pence'])}, NI {_money(ytd['ni_pence'])}, net {_money(ytd['net_pence'])}.")
            lines += _summary_lines(summary)
            lines.append(f"Tax code {code.raw}: {code.explanation}" + (" " + " ".join(code.notes) if code.notes else ""))
            check = pj.check_payslip(db, owner, job_id=row.id)
            if check:
                flagged = [r for r in check["rows"] if r["flag"]]
                if not flagged:
                    lines.append(f"Latest payslip ({check['date']}) matches the setup within £1.")
                else:
                    lines.append(f"Latest payslip ({check['date']}) differs: " + "; ".join(
                        f"{r['field'].replace('_', ' ')} payslip {_money(r['actual_pence'])} vs calculated "
                        f"{_money(r['expected_pence'])}" for r in flagged) + ".")
                    lines += [f"- {c}" for c in check["causes"]]
            else:
                lines.append("No payslip entered yet to check against (Finance > Salary > Enter a payslip).")
            data = {"employer": row.employer, "pay_date": period["date"] if period else None,
                    "net_pence": period["net_pence"] if period else None,
                    "take_home_year_pence": summary.get("net_pence"), "tax_code": code.raw,
                    "payslip_matches": check["matches"] if check else None}
            changes, labels = _salary_what_ifs(job, args, year, date.fromisoformat(period["date"]) if period else None)
            if changes:
                alt = pr.with_changes(job, changes)
                alt_summary = pr.annual_summary(alt, year)
                alt_period = pr.period_on(alt, date.fromisoformat(period["date"])) if period else None
                diff_year = alt_summary["net_pence"] - summary["net_pence"]
                lines.append(f"What if: {', '.join(labels)} for the whole {summary['tax_year']} tax year:")
                lines.append(f"- Take-home for the year {_money(alt_summary['net_pence'])} instead of "
                             f"{_money(summary['net_pence'])} ({_signed(diff_year)}, about "
                             f"{_signed(int(round(diff_year / 12)))} a month).")
                if alt_period:
                    lines.append(f"- Pay on {date.fromisoformat(alt_period['date']).strftime('%d %b %Y')}: net "
                                 f"{_money(alt_period['net_pence'])} instead of {_money(period['net_pence'])}.")
                lines.append(f"- Income tax {_money(alt_summary['tax_pence'])} ({_signed(alt_summary['tax_pence'] - summary['tax_pence'])}), "
                             f"NI {_money(alt_summary['ni_pence'])} ({_signed(alt_summary['ni_pence'] - summary['ni_pence'])}), "
                             f"student loan {_money(alt_summary['student_loan_pence'] + alt_summary['postgraduate_pence'])}.")
                if alt_summary["into_pension_pence"] or summary["into_pension_pence"]:
                    lines.append(f"- Into the pension: {_money(alt_summary['into_pension_pence'])} instead of "
                                 f"{_money(summary['into_pension_pence'])} "
                                 f"({_signed(alt_summary['into_pension_pence'] - summary['into_pension_pence'])}); "
                                 f"it costs {_money(alt_summary['pension_cost_pence'])} of take-home.")
                lines.append(f"- Marginal rate: {alt_summary['marginal_rate_percent']}% "
                             f"(was {summary['marginal_rate_percent']}%).")
                data["what_if"] = {"changes": labels, "take_home_year_pence": alt_summary["net_pence"],
                                   "take_home_change_pence": diff_year}
            lines.append(SALARY_RULE)
            return {"response": "\n".join(lines), "data": data, "exit_code": 0}
    except fin.FinanceError as exc:
        return _err(str(exc))


# ── update_salary ──────────────────────────────────────────────────────

UPDATE_ACTIONS = ("pay_rise", "set_salary", "tax_code", "pension", "bonus")


async def do_update_salary(content, owner: Optional[str] = None) -> Dict:
    """Record a pay rise, new salary, tax code or pension change, and report what it changed."""
    from src.finance import payroll as pr
    from src.finance import payroll_jobs as pj
    from src.finance import projections as proj
    from src.finance import uk_tax_rules as rules

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _err("Invalid JSON arguments")
    action = str(_first(args, "action", default="")).strip().lower().replace(" ", "_")
    action = {"raise": "pay_rise", "rise": "pay_rise", "salary": "set_salary", "new_salary": "set_salary",
              "code": "tax_code", "pension_percent": "pension", "pension_method": "pension"}.get(action, action)
    if action not in UPDATE_ACTIONS:
        return _err(f"action must be one of: {', '.join(UPDATE_ACTIONS)}")
    try:
        owner = fin.resolve_owner(owner)
        with _session() as db:
            try:
                row = pj.find_job(db, owner, _first(args, "job", "employer"))
            except fin.FinanceNotFound:
                return _err("Nothing saved. No salary is set up yet; the user needs to add their job in Finance > Salary.")
            except fin.FinanceError as exc:
                return _err(f"Nothing saved. {exc}. Ask the user which job.")
            today = _today()
            year = rules.tax_year_start(today).year
            before_job = pj.snapshot(db, owner, row)
            before_summary = pr.annual_summary(before_job, year)
            before_next = pr.next_period(before_job, today)
            before_proj = proj.projection(db, owner, years=5)["base"]
            when = _first(args, "from", "date", "on")
            done = ""
            if action == "pay_rise":
                rise = _first(args, "percent", "amount")
                if rise is None:
                    return _err("Nothing saved. How big is the pay rise (a % or £ a year)? Ask the user.")
                start = when or today.isoformat()
                text = str(rise).strip()
                if text.endswith("%") or _first(args, "percent") is not None:
                    c = pj.add_change(db, owner, row.id, kind="raise", start_date=start, percent=text.rstrip("%"),
                                      repeat=_first(args, "repeat", default="once"))
                else:
                    from src.finance.money import parse_pence

                    new = pr.salary_at(before_job, fin.parse_date(start)) + parse_pence(text)
                    c = pj.add_change(db, owner, row.id, kind="salary", start_date=start, amount=f"{new / 100:.2f}")
                done = pj.describe_job_change(c)
            elif action == "set_salary":
                amount = _first(args, "amount", "salary")
                if amount is None:
                    return _err("Nothing saved. What's the new gross salary? Ask the user.")
                if when:
                    c = pj.add_change(db, owner, row.id, kind="salary", start_date=when, amount=str(amount))
                    done = pj.describe_job_change(c)
                else:
                    pj.update_job(db, owner, row.id, salary=str(amount))
                    done = f"Salary set to {_money(row.salary_pence)} a year"
            elif action == "tax_code":
                code = _first(args, "tax_code", "code")
                if code is None:
                    return _err("Nothing saved. What's the new tax code? Ask the user.")
                if when:
                    c = pj.add_change(db, owner, row.id, kind="tax_code", start_date=when, label=str(code))
                    done = pj.describe_job_change(c)
                else:
                    pj.update_job(db, owner, row.id, tax_code=str(code))
                    done = f"Tax code set to {row.tax_code}"
            elif action == "pension":
                updates = {}
                if _first(args, "percent", "pension_percent") is not None:
                    updates["pension_employee_percent"] = str(_first(args, "percent", "pension_percent")).rstrip("%")
                if _first(args, "employer_percent") is not None:
                    updates["pension_employer_percent"] = str(args["employer_percent"]).rstrip("%")
                if _first(args, "method", "pension_method") is not None:
                    updates["pension_method"] = _first(args, "method", "pension_method")
                if not updates:
                    return _err("Nothing saved. Say the new pension % (and/or employer % or method). Ask the user.")
                if when and set(updates) == {"pension_employee_percent"}:
                    c = pj.add_change(db, owner, row.id, kind="pension_percent", start_date=when,
                                      percent=updates["pension_employee_percent"])
                    done = pj.describe_job_change(c)
                else:
                    pj.update_job(db, owner, row.id, **updates)
                    done = (f"Pension updated: {pr.PENSION_METHOD_LABELS[row.pension_method].lower()}, you "
                            f"{row.pension_employee_bp / 100:g}%, employer {row.pension_employer_bp / 100:g}%")
            elif action == "bonus":
                amount = _first(args, "amount", "bonus")
                if amount is None:
                    return _err("Nothing saved. How much is the bonus? Ask the user.")
                c = pj.add_change(db, owner, row.id, kind="bonus", start_date=when or today.isoformat(),
                                  amount=str(amount), repeat=_first(args, "repeat", default="once"))
                done = pj.describe_job_change(c)
            after_job = pj.snapshot(db, owner, row)
            after_summary = pr.annual_summary(after_job, year)
            after_next = pr.next_period(after_job, today)
            after_proj = proj.projection(db, owner, years=5)["base"]
            db.commit()
            lines = [f"Saved for {row.employer}: {done}."]
            if before_next and after_next:
                lines.append(f"Recurring income: the calculated pay item now expects {_money(after_next['net_pence'])} "
                             f"on {date.fromisoformat(after_next['date']).strftime('%d %b %Y')} (was "
                             f"{_money(before_next['net_pence'])}).")
            lines.append(f"{before_summary.get('tax_year', '')} take-home: {_money(after_summary.get('net_pence', 0))} "
                         f"(was {_money(before_summary.get('net_pence', 0))}).")
            lines.append(f"Pension contributions this tax year: {_money(after_summary.get('into_pension_pence', 0))} "
                         f"(was {_money(before_summary.get('into_pension_pence', 0))})"
                         + (", added to the linked pension as each pay is confirmed." if row.pension_asset_id else
                            "; no pension asset is linked, so they aren't added to one."))
            lines.append(f"5-year projection: net worth {_money(after_proj['end']['net_worth_pence'])} "
                         f"(was {_money(before_proj['end']['net_worth_pence'])}).")
            lines.append(SALARY_RULE)
            return {"response": "\n".join(lines), "data": {"change": done,
                                                           "take_home_year_pence": after_summary.get("net_pence")},
                    "exit_code": 0}
    except fin.FinanceError as exc:
        return _err(f"Nothing saved. {exc}")
