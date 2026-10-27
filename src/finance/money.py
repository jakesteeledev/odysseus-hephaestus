"""Money helpers for the finance tracker.

Amounts are always stored as integer pence. These helpers are the only place
that converts between pence and text, so rounding and formatting stay
consistent across routes, CSV import/export and agent tools.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CURRENCY = "GBP"
CURRENCY_SYMBOL = "£"

# Thousands separators, currency marks and spaces that banks put in CSVs.
_STRIP_RE = re.compile(r"[£$€,\s ]|GBP", re.IGNORECASE)


class MoneyError(ValueError):
    """Raised when a value cannot be read as an amount of money."""


def parse_pence(value) -> int:
    """Read an amount in pounds and return signed integer pence.

    Accepts ints (whole pounds), floats, Decimals and strings such as
    ``"12.50"``, ``"£1,234.56"``, ``"-3.2"``, ``"(4.99)"`` (accounting
    negative) or ``"4.99-"`` / ``"4.99 CR"``-style trailing signs. Rounds
    half-pennies away from zero.
    """
    if isinstance(value, bool):
        raise MoneyError("not an amount")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        value = repr(value)
    if isinstance(value, Decimal):
        number = value
    else:
        text = str(value or "").strip()
        if not text:
            raise MoneyError("empty amount")
        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative, text = True, text[1:-1]
        if text.endswith("-"):
            negative, text = True, text[:-1]
        text = _STRIP_RE.sub("", text)
        if text.startswith("+"):
            text = text[1:]
        if text.startswith("-"):
            negative, text = not negative, text[1:]
        if not re.fullmatch(r"\d+(\.\d*)?|\.\d+", text):
            raise MoneyError(f"not an amount: {value!r}")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - guarded by regex
            raise MoneyError(f"not an amount: {value!r}") from exc
        if negative:
            number = -number
    if not number.is_finite():
        raise MoneyError("not an amount")
    pence = (number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(pence)


def parse_positive_pence(value) -> int:
    """Like :func:`parse_pence` but the amount must be greater than zero."""
    pence = parse_pence(value)
    if pence <= 0:
        raise MoneyError("amount must be greater than zero")
    return pence


def format_pence(pence: int) -> str:
    """Display form, e.g. ``£1,234.56`` or ``-£12.50``."""
    pence = int(pence)
    sign = "-" if pence < 0 else ""
    pounds, rem = divmod(abs(pence), 100)
    return f"{sign}{CURRENCY_SYMBOL}{pounds:,}.{rem:02d}"


def pence_to_plain(pence: int) -> str:
    """Plain pounds for CSV export, e.g. ``12.50`` or ``-3.07`` (no symbol or commas)."""
    pence = int(pence)
    sign = "-" if pence < 0 else ""
    pounds, rem = divmod(abs(pence), 100)
    return f"{sign}{pounds}.{rem:02d}"
