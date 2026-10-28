"""Projection and salary routes (``/api/finance/insights/projection``,
``/api/finance/projection/*``, ``/api/finance/salary*``).

Registered by ``setup_finance_routes`` before its generic ``/{collection}``
routes. Same owner resolution, session handling and error mapping as the
rest of the finance API.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.finance import payroll_jobs as jobs
from src.finance import projections as proj


class AssumptionsUpdate(BaseModel):
    growth: Optional[Dict[str, Any]] = None
    asset_growth: Optional[Dict[str, Any]] = None
    cash_interest: Optional[Any] = None
    inflation: Optional[Any] = None
    shortfall_rate: Optional[Any] = None
    spending_months: Optional[Any] = None
    spending_override: Optional[Any] = None
    horizon_years: Optional[Any] = None


class ScenarioSave(BaseModel):
    name: str
    changes: List[Dict[str, Any]]
    notes: str = ""


class JobSave(BaseModel):
    employer: Optional[str] = None
    member_id: Optional[str] = None
    frequency: Optional[str] = None
    pay_anchor: Optional[str] = None
    salary: Optional[str] = None
    tax_code: Optional[str] = None
    region: Optional[str] = None
    ni_category: Optional[str] = None
    student_plans: Optional[List[str]] = None
    postgraduate: Optional[bool] = None
    sl_liability_id: Optional[str] = None
    pg_liability_id: Optional[str] = None
    pension_method: Optional[str] = None
    pension_employee_percent: Optional[str] = None
    pension_employee_fixed: Optional[str] = None
    pension_employer_percent: Optional[str] = None
    pension_employer_fixed: Optional[str] = None
    pension_basis: Optional[str] = None
    ni_passback_percent: Optional[str] = None
    pension_asset_id: Optional[str] = None
    account_id: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    notes: Optional[str] = None
    archived: Optional[bool] = None


class JobChangeSave(BaseModel):
    kind: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    amount: Optional[str] = None
    percent: Optional[str] = None
    repeat: Optional[str] = None
    label: Optional[str] = None


class PayslipSave(BaseModel):
    pay_date: str
    gross: Optional[str] = None
    tax: Optional[str] = None
    ni: Optional[str] = None
    pension: Optional[str] = None
    student_loan: Optional[str] = None
    net: Optional[str] = None
    tax_code: str = ""
    notes: str = ""


def _set(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True)


def register_projection_routes(router: APIRouter, owner_for: Callable[[Request], str], session) -> None:
    # ── Projections ─────────────────────────────────────────────────────

    @router.get("/insights/projection")
    def projection(request: Request, years: Optional[int] = None, scenario: Optional[str] = None):
        owner = owner_for(request)
        ids = [s for s in (scenario or "").split(",") if s]
        with session() as db:
            out = proj.projection(db, owner, years=years, scenario_ids=ids)
            out["scenario_list"] = proj.list_scenarios(db, owner)
            return out

    @router.get("/projection/assumptions")
    def get_assumptions(request: Request):
        owner = owner_for(request)
        with session() as db:
            return proj.assumptions_to_dict(proj.get_assumptions(db, owner))

    @router.patch("/projection/assumptions")
    def update_assumptions(body: AssumptionsUpdate, request: Request):
        owner = owner_for(request)
        with session() as db:
            return proj.assumptions_to_dict(proj.set_assumptions(db, owner, _set(body)))

    @router.get("/projection/scenarios")
    def list_scenarios(request: Request):
        owner = owner_for(request)
        with session() as db:
            return {"scenarios": proj.list_scenarios(db, owner)}

    @router.post("/projection/scenarios")
    def create_scenario(body: ScenarioSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            row = proj.save_scenario(db, owner, name=body.name, changes=body.changes, notes=body.notes)
            return proj.scenario_to_dict(row, proj.item_names(db, owner))

    @router.put("/projection/scenarios/{scenario_id}")
    def update_scenario(scenario_id: str, body: ScenarioSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            row = proj.save_scenario(db, owner, name=body.name, changes=body.changes, notes=body.notes,
                                     scenario_id=scenario_id)
            return proj.scenario_to_dict(row, proj.item_names(db, owner))

    @router.delete("/projection/scenarios/{scenario_id}")
    def delete_scenario(scenario_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            proj.delete_scenario(db, owner, scenario_id)
            return {"ok": True}

    # ── Salary ──────────────────────────────────────────────────────────

    @router.get("/salary")
    def salary(request: Request):
        owner = owner_for(request)
        with session() as db:
            jobs.recalculate_all(db, owner)  # keeps the pay item's amount current
            return jobs.salary_overview(db, owner)

    @router.post("/salary/jobs")
    def create_job(body: JobSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            row = jobs.create_job(db, owner, **_set(body))
            return jobs.job_to_dict(db, owner, row)

    @router.patch("/salary/jobs/{job_id}")
    def update_job(job_id: str, body: JobSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            row = jobs.update_job(db, owner, job_id, **_set(body))
            return jobs.job_to_dict(db, owner, row)

    @router.delete("/salary/jobs/{job_id}")
    def delete_job(job_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            jobs.delete_job(db, owner, job_id)
            return {"ok": True}

    @router.post("/salary/jobs/{job_id}/changes")
    def add_change(job_id: str, body: JobChangeSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            return jobs.change_to_dict(jobs.add_change(db, owner, job_id, **_set(body)))

    @router.patch("/salary/changes/{change_id}")
    def update_change(change_id: str, body: JobChangeSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            return jobs.change_to_dict(jobs.update_change(db, owner, change_id, **_set(body)))

    @router.delete("/salary/changes/{change_id}")
    def delete_change(change_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            jobs.delete_change(db, owner, change_id)
            return {"ok": True}

    @router.post("/salary/jobs/{job_id}/payslips")
    def add_payslip(job_id: str, body: PayslipSave, request: Request):
        owner = owner_for(request)
        with session() as db:
            row = jobs.add_payslip(db, owner, job_id, **body.model_dump())
            return {"payslip": jobs.payslip_to_dict(row), "check": jobs.check_payslip(db, owner, row.id)}

    @router.get("/salary/payslips/{payslip_id}/check")
    def check_payslip(payslip_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            return jobs.check_payslip(db, owner, payslip_id)

    @router.delete("/salary/payslips/{payslip_id}")
    def delete_payslip(payslip_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            jobs.delete_payslip(db, owner, payslip_id)
            return {"ok": True}

    @router.post("/salary/replace-manual/{recurring_id}")
    def replace_manual(recurring_id: str, request: Request):
        owner = owner_for(request)
        with session() as db:
            jobs.replace_manual_salary(db, owner, recurring_id)
            return {"ok": True}
