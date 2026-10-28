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
    UniqueConstraint,
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

# v3 adds accounts held for children (a child's savings or current account).
ACCOUNT_TYPES = ("current", "savings", "credit_card", "cash", "child_savings", "child_current")
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
            "account_type IN ('current', 'savings', 'credit_card', 'cash', 'child_savings', 'child_current')",
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
    # v2 (schema step 3): matching payments also count towards this liability.
    liability_id = Column(String, ForeignKey("finance_liabilities.id"), nullable=True)


class FinanceBankMapping(TimestampMixin, Base):
    """A saved CSV column mapping for one bank, e.g. "Monzo"."""

    __tablename__ = "finance_bank_mappings"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    config_json = Column(Text, nullable=False, default="{}")


# ── v2: assets, liabilities and their history ──────────────────────────

ASSET_TYPES = ("property", "investment", "pension", "vehicle", "other")
LIABILITY_TYPES = ("mortgage", "loan", "student_loan", "bnpl", "other")
ITEM_KINDS = ("asset", "liability")


class FinanceAsset(TimestampMixin, Base):
    """Something the owner holds outside their accounts (house, ISA, pension...)."""

    __tablename__ = "finance_assets"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    asset_type = Column(String, nullable=False, default="other")
    subtype = Column(String, nullable=False, default="")  # e.g. isa, sipp, crypto
    provider = Column(String, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    archived = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("asset_type IN ('property', 'investment', 'pension', 'vehicle', 'other')",
                        name="ck_finance_asset_type"),
    )


class FinanceLiability(TimestampMixin, Base):
    """Money owed outside credit cards (mortgage, loans, student loan, BNPL...)."""

    __tablename__ = "finance_liabilities"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    liability_type = Column(String, nullable=False, default="other")
    subtype = Column(String, nullable=False, default="")  # e.g. personal, pcp, hp, plan_2
    lender = Column(String, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    archived = Column(Boolean, nullable=False, default=False)
    # A mortgage secured on a property asset, for equity.
    linked_asset_id = Column(String, ForeignKey("finance_assets.id"), nullable=True)
    # Interest rate in basis points (450 = 4.5%) for liabilities without a
    # schedule, e.g. student loans (schema step 3).
    interest_bp = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("liability_type IN ('mortgage', 'loan', 'student_loan', 'bnpl', 'other')",
                        name="ck_finance_liability_type"),
    )


class FinanceValuation(TimestampMixin, Base):
    """A dated value (asset) or balance owed (liability). The latest one counts."""

    __tablename__ = "finance_valuations"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    item_kind = Column(String, nullable=False)
    item_id = Column(String, nullable=False, index=True)
    date = Column(Date, nullable=False)
    value_pence = Column(Integer, nullable=False)
    note = Column(String, nullable=False, default="")

    __table_args__ = (
        CheckConstraint("item_kind IN ('asset', 'liability')", name="ck_finance_valuation_kind"),
        CheckConstraint("value_pence >= 0", name="ck_finance_valuation_non_negative"),
        Index("ix_finance_valuation_item_date", "item_kind", "item_id", "date"),
    )


class FinanceContribution(TimestampMixin, Base):
    """Money paid into an investment or pension, optionally a linked transaction."""

    __tablename__ = "finance_contributions"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    asset_id = Column(String, ForeignKey("finance_assets.id"), nullable=False, index=True)
    date = Column(Date, nullable=False)
    amount_pence = Column(Integer, nullable=False)
    note = Column(String, nullable=False, default="")
    # Deleting the transaction (or undoing its import) removes the contribution.
    transaction_id = Column(String, ForeignKey("finance_transactions.id", ondelete="CASCADE"),
                            nullable=True, unique=True)

    __table_args__ = (
        CheckConstraint("amount_pence > 0", name="ck_finance_contribution_positive"),
    )


class FinanceLoanSchedule(TimestampMixin, Base):
    """Repayment terms for a mortgage, loan, car finance or BNPL (not student loans)."""

    __tablename__ = "finance_loan_schedules"

    liability_id = Column(String, ForeignKey("finance_liabilities.id", ondelete="CASCADE"), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    start_date = Column(Date, nullable=False)
    start_balance_pence = Column(Integer, nullable=False)
    apr_bp = Column(Integer, nullable=False, default=0)  # 4.5% APR = 450
    term_months = Column(Integer, nullable=True)
    monthly_payment_pence = Column(Integer, nullable=True)  # fixed payment, if known
    payment_day = Column(Integer, nullable=False, default=1)
    balloon_pence = Column(Integer, nullable=False, default=0)  # PCP final payment

    __table_args__ = (
        CheckConstraint("start_balance_pence >= 0", name="ck_finance_schedule_balance"),
        CheckConstraint("apr_bp >= 0", name="ck_finance_schedule_apr"),
        CheckConstraint("payment_day BETWEEN 1 AND 31", name="ck_finance_schedule_day"),
        CheckConstraint("balloon_pence >= 0", name="ck_finance_schedule_balloon"),
    )


class FinanceRepayment(TimestampMixin, Base):
    """A payment against a liability: a linked transaction, or entered by hand."""

    __tablename__ = "finance_repayments"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    liability_id = Column(String, ForeignKey("finance_liabilities.id"), nullable=False, index=True)
    date = Column(Date, nullable=False)
    amount_pence = Column(Integer, nullable=False)
    note = Column(String, nullable=False, default="")
    transaction_id = Column(String, ForeignKey("finance_transactions.id", ondelete="CASCADE"),
                            nullable=True, unique=True)

    __table_args__ = (
        CheckConstraint("amount_pence > 0", name="ck_finance_repayment_positive"),
    )


FREQUENCIES = ("weekly", "four_weekly", "monthly", "quarterly", "yearly")


class FinanceRecurring(TimestampMixin, Base):
    """A regular payment or income: expected on schedule, confirmed by the user."""

    __tablename__ = "finance_recurring"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    account_id = Column(String, ForeignKey("finance_accounts.id"), nullable=False)
    amount_pence = Column(Integer, nullable=False)
    direction = Column(String, nullable=False, default="out")
    category_id = Column(String, ForeignKey("finance_categories.id"), nullable=True)
    frequency = Column(String, nullable=False, default="monthly")
    anchor_date = Column(Date, nullable=False)  # first due date; later ones follow the frequency
    end_date = Column(Date, nullable=True)
    match_text = Column(String, nullable=False, default="")  # words expected in the bank description
    liability_id = Column(String, ForeignKey("finance_liabilities.id"), nullable=True)  # loan payment
    asset_id = Column(String, ForeignKey("finance_assets.id"), nullable=True)  # regular contribution
    # Set for a regular transfer (e.g. pocket money): money moves from account_id to here.
    to_account_id = Column(String, ForeignKey("finance_accounts.id"), nullable=True)
    archived = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("amount_pence > 0", name="ck_finance_recurring_amount"),
        CheckConstraint("direction IN ('in', 'out')", name="ck_finance_recurring_direction"),
        CheckConstraint("frequency IN ('weekly', 'four_weekly', 'monthly', 'quarterly', 'yearly')",
                        name="ck_finance_recurring_frequency"),
    )


class FinanceRecurringEvent(TimestampMixin, Base):
    """The user's decision for one due date: confirmed (with a transaction) or skipped."""

    __tablename__ = "finance_recurring_events"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    recurring_id = Column(String, ForeignKey("finance_recurring.id", ondelete="CASCADE"), nullable=False, index=True)
    due_date = Column(Date, nullable=False)
    status = Column(String, nullable=False)
    # Deleting the transaction makes the occurrence expected again.
    transaction_id = Column(String, ForeignKey("finance_transactions.id", ondelete="CASCADE"), nullable=True)
    created_transaction = Column(Boolean, nullable=False, default=False)  # made by "confirm"

    __table_args__ = (
        CheckConstraint("status IN ('confirmed', 'skipped')", name="ck_finance_recurring_event_status"),
        UniqueConstraint("recurring_id", "due_date", name="uq_finance_recurring_event_due"),
    )


class FinanceBudget(TimestampMixin, Base):
    """A monthly spending limit for a category, from `month` until changed.

    The limit for a month is the latest row on or before that month, so past
    months keep the limit they had. ``limit_pence`` NULL means no budget from
    that month. No rollover: each month starts fresh.
    """

    __tablename__ = "finance_budgets"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    category_id = Column(String, ForeignKey("finance_categories.id", ondelete="CASCADE"), nullable=False)
    month = Column(Date, nullable=False)  # first day of the month it starts
    limit_pence = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("limit_pence IS NULL OR limit_pence >= 0", name="ck_finance_budget_limit"),
        UniqueConstraint("owner", "category_id", "month", name="uq_finance_budget_month"),
    )


# ── v3: household members and ownership ───────────────────────────────

RELATIONSHIPS = ("me", "partner", "child", "other")
OWNED_KINDS = ("account", "asset", "liability")


class FinanceMember(TimestampMixin, Base):
    """A person in the household. Tracked profiles only: they don't log in.

    ``owner`` is still the Odysseus user whose data this is. ``linked_user``
    is reserved for linking a member to their own login later (unused).
    """

    __tablename__ = "finance_members"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    relationship = Column(String, nullable=False, default="other")
    date_of_birth = Column(Date, nullable=True)
    color = Column(String, nullable=False, default="")  # a CATEGORY_COLORS name
    initials = Column(String, nullable=False, default="")  # blank = from the name
    is_primary = Column(Boolean, nullable=False, default=False)
    archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    linked_user = Column(String, nullable=True)

    __table_args__ = (
        CheckConstraint("relationship IN ('me', 'partner', 'child', 'other')",
                        name="ck_finance_member_relationship"),
    )


class FinanceItemOwner(Base):
    """One owner of an account, asset or liability. Joint items have several rows."""

    __tablename__ = "finance_item_owners"

    owner = Column(String, nullable=False, index=True)
    item_kind = Column(String, primary_key=True)
    item_id = Column(String, primary_key=True)
    member_id = Column(String, ForeignKey("finance_members.id"), primary_key=True, index=True)

    __table_args__ = (
        CheckConstraint("item_kind IN ('account', 'asset', 'liability')", name="ck_finance_item_owner_kind"),
    )


class FinanceSetting(Base):
    """Per-user finance settings (key/value), e.g. the Junior ISA allowance."""

    __tablename__ = "finance_settings"

    owner = Column(String, primary_key=True)
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


# ── Projections: assumptions and saved scenarios ───────────────────────


class FinanceProjectionSetting(Base):
    """One projection assumption (key/value), e.g. ``growth:property`` = 150 (bp).

    Keys are listed in ``src.finance.projections.ASSUMPTION_KEYS``; an asset
    override is ``growth_asset:<asset id>``.
    """

    __tablename__ = "finance_projection_settings"

    owner = Column(String, primary_key=True)
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


class FinanceProjectionScenario(TimestampMixin, Base):
    """A named what-if: a list of changes applied on top of the base projection."""

    __tablename__ = "finance_projection_scenarios"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    changes_json = Column(Text, nullable=False, default="[]")
    notes = Column(Text, nullable=False, default="")


class FinanceMeta(Base):
    """Finance schema bookkeeping (key/value), e.g. schema_version."""

    __tablename__ = "finance_meta"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


V1_TABLES = [
    FinanceAccount.__table__,
    FinanceCategory.__table__,
    FinanceImportBatch.__table__,
    FinanceTransaction.__table__,
    FinanceRule.__table__,
    FinanceBankMapping.__table__,
]
V2_TABLES = [
    FinanceAsset.__table__,
    FinanceLiability.__table__,
    FinanceValuation.__table__,
    FinanceContribution.__table__,
]
V3_TABLES = [
    FinanceLoanSchedule.__table__,
    FinanceRepayment.__table__,
]
V4_TABLES = [
    FinanceRecurring.__table__,
    FinanceRecurringEvent.__table__,
]
V5_TABLES = [FinanceBudget.__table__]
V6_TABLES = [FinanceMember.__table__, FinanceItemOwner.__table__]
V7_TABLES = [FinanceSetting.__table__]
V8_TABLES = [FinanceProjectionSetting.__table__, FinanceProjectionScenario.__table__]
FINANCE_TABLES = (V1_TABLES + V2_TABLES + V3_TABLES + V4_TABLES + V5_TABLES + V6_TABLES + V7_TABLES
                  + V8_TABLES + [FinanceMeta.__table__])

_ensured: "weakref.WeakSet" = weakref.WeakSet()
_ensure_lock = threading.Lock()


def ensure_tables(bind) -> None:
    """Bring the finance schema on *bind* up to date (idempotent, once per engine).

    See ``src.finance.migrations``: existing finance data is backed up before
    any migration step runs.
    """
    if bind in _ensured:
        return
    with _ensure_lock:
        if bind in _ensured:
            return
        from src.finance.migrations import migrate

        migrate(bind)
        _ensured.add(bind)
