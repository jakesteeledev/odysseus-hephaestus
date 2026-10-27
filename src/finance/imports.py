"""CSV import pipeline, categorisation rules, saved bank mappings and export.

Import flow: the browser uploads a CSV and a column mapping, gets a preview
of every parsed row (errors, duplicates, suggested categories), then commits.
The commit re-parses the same file on the server instead of trusting rows
sent back by the browser; the browser only sends per-row category choices
and rows to leave out. Every import is a batch that can be undone.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import utcnow_naive
from src.finance import csv_io
from src.finance.models import (
    FinanceAccount,
    FinanceBankMapping,
    FinanceCategory,
    FinanceImportBatch,
    FinanceRule,
    FinanceTransaction,
)
from src.finance.service import (
    FinanceConflict,
    FinanceError,
    FinanceNotFound,
    _get_account,
    _get_category,
    _new_id,
    _require_owner,
    create_transaction,
    uncategorised,
)

MAX_RULE_PATTERN_LEN = 80
MAX_MAPPING_NAME_LEN = 60


# ── Rules ───────────────────────────────────────────────────────────────


def rule_to_dict(rule: FinanceRule) -> dict:
    return {"id": rule.id, "pattern": rule.pattern, "category_id": rule.category_id}


def list_rules(db: Session, owner) -> list[dict]:
    owner = _require_owner(owner)
    rules = db.query(FinanceRule).filter(FinanceRule.owner == owner).all()
    return [rule_to_dict(r) for r in _ordered_rules(rules)]


def _ordered_rules(rules):
    # Longest (most specific) pattern wins; ties go to the oldest rule.
    return sorted(rules, key=lambda r: (-len(r.pattern), r.created_at or datetime.min, r.id))


def _clean_pattern(pattern) -> str:
    text = " ".join(str(pattern or "").split())
    if not text:
        raise FinanceError("rule text is required")
    if len(text) > MAX_RULE_PATTERN_LEN:
        raise FinanceError(f"rule text must be at most {MAX_RULE_PATTERN_LEN} characters")
    return text


def create_rule(db: Session, owner, *, pattern, category_id) -> FinanceRule:
    owner = _require_owner(owner)
    pattern = _clean_pattern(pattern)
    category = _get_category(db, owner, category_id)
    existing = (
        db.query(FinanceRule)
        .filter(FinanceRule.owner == owner, func.lower(FinanceRule.pattern) == pattern.lower())
        .first()
    )
    if existing is not None:
        # "Remember this" twice for the same text just updates the category.
        existing.category_id = category.id
        db.flush()
        return existing
    rule = FinanceRule(id=_new_id(), owner=owner, pattern=pattern, category_id=category.id)
    db.add(rule)
    db.flush()
    return rule


def update_rule(db: Session, owner, rule_id, *, pattern=None, category_id=None) -> FinanceRule:
    owner = _require_owner(owner)
    rule = _get_rule(db, owner, rule_id)
    if pattern is not None:
        rule.pattern = _clean_pattern(pattern)
    if category_id is not None:
        rule.category_id = _get_category(db, owner, category_id).id
    db.flush()
    return rule


def delete_rule(db: Session, owner, rule_id) -> None:
    owner = _require_owner(owner)
    db.delete(_get_rule(db, owner, rule_id))
    db.flush()


def _get_rule(db: Session, owner: str, rule_id) -> FinanceRule:
    rule = db.query(FinanceRule).filter(FinanceRule.owner == owner, FinanceRule.id == str(rule_id or "")).first()
    if rule is None:
        raise FinanceNotFound("rule not found")
    return rule


class RuleMatcher:
    """Case-insensitive 'description contains X' matching."""

    def __init__(self, rules):
        self._rules = [(r.pattern.lower(), r) for r in _ordered_rules(rules)]

    def match(self, description: str) -> Optional[FinanceRule]:
        text = " ".join(str(description or "").split()).lower()
        for pattern, rule in self._rules:
            if pattern in text:
                return rule
        return None


# ── Saved bank mappings ─────────────────────────────────────────────────


def mapping_to_dict(mapping: FinanceBankMapping) -> dict:
    try:
        config = json.loads(mapping.config_json or "{}")
    except (TypeError, ValueError):
        config = {}
    return {"id": mapping.id, "name": mapping.name, "config": config}


def list_mappings(db: Session, owner) -> list[dict]:
    owner = _require_owner(owner)
    rows = db.query(FinanceBankMapping).filter(FinanceBankMapping.owner == owner).order_by(
        func.lower(FinanceBankMapping.name)).all()
    return [mapping_to_dict(m) for m in rows]


def save_mapping(db: Session, owner, *, name, config: dict) -> FinanceBankMapping:
    """Create or replace (by name, case-insensitive) a saved bank mapping."""
    owner = _require_owner(owner)
    name = " ".join(str(name or "").split())
    if not name:
        raise FinanceError("mapping name is required")
    if len(name) > MAX_MAPPING_NAME_LEN:
        raise FinanceError(f"mapping name must be at most {MAX_MAPPING_NAME_LEN} characters")
    try:
        clean = csv_io.Mapping.from_dict(config).to_dict()
    except csv_io.CsvError as exc:
        raise FinanceError(str(exc)) from exc
    existing = (
        db.query(FinanceBankMapping)
        .filter(FinanceBankMapping.owner == owner, func.lower(FinanceBankMapping.name) == name.lower())
        .first()
    )
    if existing is not None:
        existing.name = name
        existing.config_json = json.dumps(clean)
        db.flush()
        return existing
    mapping = FinanceBankMapping(id=_new_id(), owner=owner, name=name, config_json=json.dumps(clean))
    db.add(mapping)
    db.flush()
    return mapping


def delete_mapping(db: Session, owner, mapping_id) -> None:
    owner = _require_owner(owner)
    mapping = (
        db.query(FinanceBankMapping)
        .filter(FinanceBankMapping.owner == owner, FinanceBankMapping.id == str(mapping_id or ""))
        .first()
    )
    if mapping is None:
        raise FinanceNotFound("mapping not found")
    db.delete(mapping)
    db.flush()


# ── Import ──────────────────────────────────────────────────────────────


def _dup_key(day: date, amount_pence: int, direction: str, description: str) -> tuple:
    return (day, amount_pence, direction, " ".join(str(description or "").split()).lower())


def _parse(data, mapping_config: dict):
    try:
        text = csv_io.decode_csv(data) if isinstance(data, (bytes, bytearray)) else str(data or "")
        rows = csv_io.read_rows(text)
        mapping = csv_io.Mapping.from_dict(mapping_config)
        return csv_io.parse_statement(rows, mapping)
    except csv_io.CsvError as exc:
        raise FinanceError(str(exc)) from exc


def preview_import(db: Session, owner, *, data, mapping: dict, account_id) -> dict:
    """Parse a statement and report what an import would do, without saving."""
    owner = _require_owner(owner)
    account = _get_account(db, owner, account_id)
    header, parsed = _parse(data, mapping)
    plan = _plan(db, owner, account, parsed)
    rows = []
    for item in parsed:
        entry = {
            "row_number": item.row_number,
            "raw": item.raw,
            "date": item.date.isoformat() if item.date else None,
            "description": item.description,
            "amount_pence": item.amount_pence,
            "direction": item.direction,
            "error": item.error,
            "duplicate": item.row_number in plan["duplicates"],
            "category_id": plan["categories"].get(item.row_number),
            "rule_id": plan["rules"].get(item.row_number),
        }
        rows.append(entry)
    errors = sum(1 for r in rows if r["error"])
    duplicates = len(plan["duplicates"])
    return {
        "header": header,
        "rows": rows,
        "counts": {
            "total": len(rows),
            "errors": errors,
            "duplicates": duplicates,
            "importable": len(rows) - errors - duplicates,
        },
    }


def _plan(db: Session, owner: str, account: FinanceAccount, parsed) -> dict:
    """Work out duplicates and suggested categories for parsed rows.

    A row is a duplicate when the account already has a transaction with the
    same date, amount, direction and description. Matching is counted, so a
    file with two identical coffees against one existing coffee imports one.
    """
    good = [p for p in parsed if p.ok]
    existing = Counter()
    if good:
        days = [p.date for p in good]
        for tx in db.query(FinanceTransaction).filter(
            FinanceTransaction.owner == owner,
            FinanceTransaction.account_id == account.id,
            FinanceTransaction.date >= min(days),
            FinanceTransaction.date <= max(days),
        ):
            existing[_dup_key(tx.date, tx.amount_pence, tx.direction, tx.description)] += 1
    matcher = RuleMatcher(db.query(FinanceRule).filter(FinanceRule.owner == owner).all())
    fallback = uncategorised(db, owner).id
    duplicates, categories, rules = set(), {}, {}
    for item in good:
        key = _dup_key(item.date, item.amount_pence, item.direction, item.description)
        if existing[key] > 0:
            existing[key] -= 1
            duplicates.add(item.row_number)
            continue
        rule = matcher.match(item.description)
        categories[item.row_number] = rule.category_id if rule else fallback
        rules[item.row_number] = rule.id if rule else None
    return {"duplicates": duplicates, "categories": categories, "rules": rules}


def commit_import(
    db: Session,
    owner,
    *,
    data,
    mapping: dict,
    account_id,
    filename: str = "",
    mapping_name: str = "",
    category_overrides: Optional[dict] = None,
    exclude_rows: Optional[list] = None,
) -> dict:
    """Import a statement as one batch. Returns the batch and skipped rows.

    *category_overrides* maps row numbers to category ids chosen in the
    preview; *exclude_rows* lists row numbers the user unticked.
    """
    owner = _require_owner(owner)
    account = _get_account(db, owner, account_id)
    _, parsed = _parse(data, mapping)
    plan = _plan(db, owner, account, parsed)
    overrides = {int(k): v for k, v in (category_overrides or {}).items()}
    excluded = {int(r) for r in (exclude_rows or [])}
    valid_categories = {c.id for c in db.query(FinanceCategory.id).filter(FinanceCategory.owner == owner)}

    batch = FinanceImportBatch(
        id=_new_id(), owner=owner, account_id=account.id,
        filename=str(filename or "")[:200], mapping_name=str(mapping_name or "")[:MAX_MAPPING_NAME_LEN],
    )
    db.add(batch)
    db.flush()
    imported, skipped = 0, []
    for item in parsed:
        if not item.ok:
            skipped.append({"row_number": item.row_number, "reason": "error", "detail": item.error})
            continue
        if item.row_number in plan["duplicates"]:
            skipped.append({"row_number": item.row_number, "reason": "duplicate",
                            "detail": f"{item.date.isoformat()} {item.description}"})
            continue
        if item.row_number in excluded:
            skipped.append({"row_number": item.row_number, "reason": "excluded", "detail": ""})
            continue
        category_id = overrides.get(item.row_number) or plan["categories"].get(item.row_number)
        if category_id not in valid_categories:
            raise FinanceError(f"row {item.row_number}: unknown category")
        create_transaction(
            db, owner, account_id=account.id, date=item.date, amount=Decimal(item.amount_pence) / 100,
            direction=item.direction, category_id=category_id, description=item.description,
            import_batch_id=batch.id,
        )
        imported += 1
    batch.imported_count = imported
    batch.skipped_count = len(skipped)
    db.flush()
    return {"batch": batch_to_dict(batch, account.name), "skipped": skipped}


def batch_to_dict(batch: FinanceImportBatch, account_name: str = "") -> dict:
    return {
        "id": batch.id,
        "account_id": batch.account_id,
        "account_name": account_name,
        "filename": batch.filename,
        "mapping_name": batch.mapping_name,
        "imported_count": batch.imported_count,
        "skipped_count": batch.skipped_count,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "undone_at": batch.undone_at.isoformat() if batch.undone_at else None,
    }


def list_batches(db: Session, owner, *, limit: int = 20) -> list[dict]:
    owner = _require_owner(owner)
    names = dict(db.query(FinanceAccount.id, FinanceAccount.name).filter(FinanceAccount.owner == owner))
    batches = (
        db.query(FinanceImportBatch)
        .filter(FinanceImportBatch.owner == owner)
        .order_by(FinanceImportBatch.created_at.desc())
        .limit(int(limit))
        .all()
    )
    return [batch_to_dict(b, names.get(b.account_id, "")) for b in batches]


def undo_import(db: Session, owner, batch_id) -> int:
    """Delete every transaction from an import batch. Returns how many."""
    owner = _require_owner(owner)
    batch = (
        db.query(FinanceImportBatch)
        .filter(FinanceImportBatch.owner == owner, FinanceImportBatch.id == str(batch_id or ""))
        .first()
    )
    if batch is None:
        raise FinanceNotFound("import not found")
    if batch.undone_at is not None:
        raise FinanceConflict("this import was already undone")
    removed = (
        db.query(FinanceTransaction)
        .filter(FinanceTransaction.owner == owner, FinanceTransaction.import_batch_id == batch.id)
        .delete(synchronize_session=False)
    )
    batch.undone_at = utcnow_naive()
    db.flush()
    return int(removed or 0)


# ── Export ──────────────────────────────────────────────────────────────


def export_transactions(db: Session, owner, *, start: Optional[date] = None, end: Optional[date] = None,
                        account_id: Optional[str] = None) -> str:
    """CSV text for transactions with ``start <= date <= end`` (both inclusive)."""
    owner = _require_owner(owner)
    query = db.query(FinanceTransaction).filter(FinanceTransaction.owner == owner)
    if account_id:
        query = query.filter(FinanceTransaction.account_id == _get_account(db, owner, account_id).id)
    if start is not None:
        query = query.filter(FinanceTransaction.date >= start)
    if end is not None:
        query = query.filter(FinanceTransaction.date <= end)
    accounts = dict(db.query(FinanceAccount.id, FinanceAccount.name).filter(FinanceAccount.owner == owner))
    categories = dict(db.query(FinanceCategory.id, FinanceCategory.name).filter(FinanceCategory.owner == owner))
    rows = []
    for tx in query.order_by(FinanceTransaction.date, FinanceTransaction.created_at, FinanceTransaction.id):
        rows.append({
            "date": tx.date.isoformat(),
            "account": accounts.get(tx.account_id, ""),
            "description": tx.description,
            "category": "Transfer" if tx.transfer_id else categories.get(tx.category_id, ""),
            "direction": tx.direction,
            "amount_pence": tx.amount_pence,
            "notes": tx.notes,
        })
    return csv_io.export_csv(rows)
