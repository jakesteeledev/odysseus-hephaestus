"""SQLAlchemy models for the finance tracker.

The tables share Odysseus's ``Base`` and live in ``data/app.db`` like Notes
and Calendar, but they are declared here so the feature stays in its own
module. ``core.database.init_db()`` runs before this module is imported, so
the finance module creates its own tables with :func:`ensure_tables`.

Every row carries an ``owner``; all queries in ``src.finance.service`` filter
on it. Amounts are integer pence (``*_pence`` columns).
"""

from __future__ import annotations

import threading
import weakref

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from core.database import Base, TimestampMixin

ACCOUNT_TYPES = ("current", "savings", "credit_card", "cash")
CATEGORY_KINDS = ("income", "expense")
DIRECTIONS = ("in", "out")
# Named colours shared with the Notes and Calendar colour pickers; the
# frontend maps names to its palette, so no colour values are stored.
CATEGORY_COLORS = ("", "red", "orange", "yellow", "green", "blue", "purple")


class FinanceAccount(TimestampMixin, Base):
    __tablename__ = "finance_accounts"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    account_type = Column(String, nullable=False, default="current")
    opening_balance_pence = Column(Integer, nullable=False, default=0)
    archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('current', 'savings', 'credit_card', 'cash')",
            name="ck_finance_account_type",
        ),
    )


class FinanceCategory(TimestampMixin, Base):
    __tablename__ = "finance_categories"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="expense")
    color = Column(String, nullable=False, default="")
    # The protected "Uncategorised" fallback; it cannot be deleted.
    is_system = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("kind IN ('income', 'expense')", name="ck_finance_category_kind"),
    )


class FinanceImportBatch(TimestampMixin, Base):
    __tablename__ = "finance_import_batches"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    account_id = Column(String, ForeignKey("finance_accounts.id"), nullable=False)
    filename = Column(String, nullable=False, default="")
    mapping_name = Column(String, nullable=False, default="")
    imported_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    undone_at = Column(DateTime, nullable=True)


class FinanceTransaction(TimestampMixin, Base):
    __tablename__ = "finance_transactions"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    account_id = Column(String, ForeignKey("finance_accounts.id"), nullable=False)
    date = Column(Date, nullable=False)
    amount_pence = Column(Integer, nullable=False)
    direction = Column(String, nullable=False)
    category_id = Column(String, ForeignKey("finance_categories.id"), nullable=True)
    description = Column(String, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    # Both sides of a transfer share this id; transfers have no category and
    # are left out of income/spending totals.
    transfer_id = Column(String, nullable=True, index=True)
    import_batch_id = Column(String, ForeignKey("finance_import_batches.id"), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint("amount_pence > 0", name="ck_finance_tx_amount_positive"),
        CheckConstraint("direction IN ('in', 'out')", name="ck_finance_tx_direction"),
        Index("ix_finance_tx_owner_date", "owner", "date"),
        Index("ix_finance_tx_account_date", "account_id", "date"),
    )


class FinanceRule(TimestampMixin, Base):
    """'Description contains X' -> category, applied on import."""

    __tablename__ = "finance_rules"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    pattern = Column(String, nullable=False)
    category_id = Column(String, ForeignKey("finance_categories.id"), nullable=False)


class FinanceBankMapping(TimestampMixin, Base):
    """A saved CSV column mapping for one bank, e.g. "Monzo"."""

    __tablename__ = "finance_bank_mappings"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    config_json = Column(Text, nullable=False, default="{}")


FINANCE_TABLES = [
    FinanceAccount.__table__,
    FinanceCategory.__table__,
    FinanceImportBatch.__table__,
    FinanceTransaction.__table__,
    FinanceRule.__table__,
    FinanceBankMapping.__table__,
]

_ensured: "weakref.WeakSet" = weakref.WeakSet()
_ensure_lock = threading.Lock()


def ensure_tables(bind) -> None:
    """Create the finance tables on *bind* if they are missing (idempotent)."""
    if bind in _ensured:
        return
    with _ensure_lock:
        if bind in _ensured:
            return
        Base.metadata.create_all(bind=bind, tables=FINANCE_TABLES)
        _ensured.add(bind)
