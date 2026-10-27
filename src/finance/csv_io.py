"""CSV reading (bank statement import) and writing (export) for finance.

Pure functions with no database access, so parsing and mapping rules can be
tested on their own. ``src.finance.imports`` combines them with storage.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from src.finance.money import MoneyError, parse_pence, pence_to_plain

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000

# User-facing date formats -> strptime patterns. dd/mm/yyyy is the default.
DATE_FORMATS = {
    "dd/mm/yyyy": "%d/%m/%Y",
    "dd/mm/yy": "%d/%m/%y",
    "dd-mm-yyyy": "%d-%m-%Y",
    "dd.mm.yyyy": "%d.%m.%Y",
    "yyyy-mm-dd": "%Y-%m-%d",
    "mm/dd/yyyy": "%m/%d/%Y",
    "dd mmm yyyy": "%d %b %Y",
}
DEFAULT_DATE_FORMAT = "dd/mm/yyyy"
AMOUNT_MODES = ("signed", "split")

EXPORT_COLUMNS = ["date", "account", "description", "category", "direction", "amount", "notes"]


class CsvError(ValueError):
    """The file or mapping cannot be used; routes map this to 400."""


# ── Reading ─────────────────────────────────────────────────────────────


def decode_csv(data: bytes) -> str:
    if len(data) > MAX_IMPORT_BYTES:
        raise CsvError(f"file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("could not read the file as text")  # pragma: no cover - cp1252 decodes nearly anything


def read_rows(text: str) -> list[list[str]]:
    """Split CSV text into rows of trimmed cells, dropping blank lines."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for row in csv.reader(io.StringIO(text), dialect):
        cells = [cell.strip() for cell in row]
        if any(cells):
            rows.append(cells)
        if len(rows) > MAX_IMPORT_ROWS + 1:
            raise CsvError(f"file has more than {MAX_IMPORT_ROWS} rows")
    if not rows:
        raise CsvError("the file has no rows")
    return rows


@dataclass
class Mapping:
    """How to read one bank's CSV. Columns are header names when
    ``has_header`` is true, otherwise 0-based column numbers."""

    date_column: object
    description_column: object
    amount_mode: str = "signed"
    amount_column: object = None
    paid_in_column: object = None
    paid_out_column: object = None
    date_format: str = DEFAULT_DATE_FORMAT
    has_header: bool = True
    # Some banks show money out as positive in a signed column; flip it.
    invert_amounts: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Mapping":
        if not isinstance(data, dict):
            raise CsvError("mapping must be an object")
        mapping = cls(
            date_column=data.get("date_column"),
            description_column=data.get("description_column"),
            amount_mode=str(data.get("amount_mode") or "signed").strip().lower(),
            amount_column=data.get("amount_column"),
            paid_in_column=data.get("paid_in_column"),
            paid_out_column=data.get("paid_out_column"),
            date_format=str(data.get("date_format") or DEFAULT_DATE_FORMAT).strip().lower(),
            has_header=bool(data.get("has_header", True)),
            invert_amounts=bool(data.get("invert_amounts", False)),
        )
        mapping.validate()
        return mapping

    def to_dict(self) -> dict:
        return {
            "date_column": self.date_column,
            "description_column": self.description_column,
            "amount_mode": self.amount_mode,
            "amount_column": self.amount_column,
            "paid_in_column": self.paid_in_column,
            "paid_out_column": self.paid_out_column,
            "date_format": self.date_format,
            "has_header": self.has_header,
            "invert_amounts": self.invert_amounts,
        }

    def validate(self) -> None:
        if self.amount_mode not in AMOUNT_MODES:
            raise CsvError("amount mode must be 'signed' or 'split'")
        if self.date_format not in DATE_FORMATS:
            raise CsvError("date format must be one of: " + ", ".join(DATE_FORMATS))
        required = {"date": self.date_column, "description": self.description_column}
        if self.amount_mode == "signed":
            required["amount"] = self.amount_column
        else:
            if _blank(self.paid_in_column) and _blank(self.paid_out_column):
                raise CsvError("choose a paid-in column, a paid-out column, or both")
        for name, column in required.items():
            if _blank(column):
                raise CsvError(f"choose the {name} column")
        if not self.has_header:
            for column in self._columns():
                if not _blank(column) and not _is_index(column):
                    raise CsvError("without a header row, columns must be numbers")

    def _columns(self):
        return (self.date_column, self.description_column, self.amount_column,
                self.paid_in_column, self.paid_out_column)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_index(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, str) and value.strip().isdigit()


def _column_index(column, header: Optional[list[str]]) -> Optional[int]:
    if _blank(column):
        return None
    if header is not None:
        wanted = str(column).strip().lower()
        for index, name in enumerate(header):
            if name.strip().lower() == wanted:
                return index
        if _is_index(column) and int(column) < len(header):
            return int(column)
        raise CsvError(f"column {column!r} is not in this file")
    return int(column)


def parse_date_text(text: str, date_format: str = DEFAULT_DATE_FORMAT) -> date:
    pattern = DATE_FORMATS.get(date_format)
    if pattern is None:
        raise CsvError(f"unknown date format {date_format!r}")
    value = str(text or "").strip()
    # Some banks add a time: "27/10/2026 14:03".
    for candidate in (value, value.split(" ")[0] if date_format != "dd mmm yyyy" else value):
        try:
            return datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"date {value!r} does not match {date_format}")


@dataclass
class ParsedRow:
    row_number: int  # 1-based line in the file, counting the header
    raw: list[str]
    date: Optional[date] = None
    description: str = ""
    amount_pence: Optional[int] = None  # always positive
    direction: Optional[str] = None
    error: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def _cell(row: list[str], index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def parse_statement(rows: list[list[str]], mapping: Mapping) -> tuple[Optional[list[str]], list[ParsedRow]]:
    """Apply *mapping* to CSV rows. Bad rows are kept with an ``error``."""
    mapping.validate()
    header = rows[0] if mapping.has_header else None
    body = rows[1:] if mapping.has_header else rows
    offset = 2 if mapping.has_header else 1
    idx_date = _column_index(mapping.date_column, header)
    idx_desc = _column_index(mapping.description_column, header)
    idx_amount = _column_index(mapping.amount_column, header) if mapping.amount_mode == "signed" else None
    idx_in = _column_index(mapping.paid_in_column, header) if mapping.amount_mode == "split" else None
    idx_out = _column_index(mapping.paid_out_column, header) if mapping.amount_mode == "split" else None

    parsed = []
    for number, row in enumerate(body, start=offset):
        item = ParsedRow(row_number=number, raw=row)
        try:
            item.date = parse_date_text(_cell(row, idx_date), mapping.date_format)
        except ValueError as exc:
            item.error = str(exc)
        item.description = " ".join(_cell(row, idx_desc).split())[:200]
        if not item.error:
            try:
                item.amount_pence, item.direction = _row_amount(row, mapping, idx_amount, idx_in, idx_out)
            except (MoneyError, ValueError) as exc:
                item.error = str(exc)
        parsed.append(item)
    return header, parsed


def _row_amount(row, mapping: Mapping, idx_amount, idx_in, idx_out) -> tuple[int, str]:
    if mapping.amount_mode == "signed":
        pence = parse_pence(_cell(row, idx_amount))
        if mapping.invert_amounts:
            pence = -pence
    else:
        paid_in = _cell(row, idx_in)
        paid_out = _cell(row, idx_out)
        pence_in = abs(parse_pence(paid_in)) if paid_in.strip() else 0
        pence_out = abs(parse_pence(paid_out)) if paid_out.strip() else 0
        if pence_in and pence_out:
            raise ValueError("row has both paid in and paid out")
        pence = pence_in - pence_out
    if pence == 0:
        raise ValueError("amount is zero or missing")
    return abs(pence), ("in" if pence > 0 else "out")


# ── Writing ─────────────────────────────────────────────────────────────


def _safe_cell(value) -> str:
    """Stop spreadsheet apps treating text as a formula (CSV injection)."""
    text = str(value if value is not None else "")
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def export_csv(rows: list[dict]) -> str:
    """Rows of export dicts -> CSV text that opens cleanly in Excel.

    UTF-8 with a byte-order mark (so £ and accents survive), CRLF line ends,
    ISO dates and plain amounts such as ``12.50``.
    """
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([
            row["date"],
            _safe_cell(row.get("account", "")),
            _safe_cell(row.get("description", "")),
            _safe_cell(row.get("category", "")),
            row["direction"],
            pence_to_plain(row["amount_pence"]),
            _safe_cell(row.get("notes", "")),
        ])
    return out.getvalue()
