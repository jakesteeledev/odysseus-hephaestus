"""UK PAYE rates and thresholds, by tax year (6 April to 5 April).

Every figure lives here; nothing else in the code holds a rate or threshold.
Each section records where it came from and when it was checked. Amounts are
pence, rates are basis points (2000 = 20%).

Bands are on taxable income (income after the personal allowance), as HMRC
publishes them for employers. Wales currently uses the same rates as England
and Northern Ireland, so it shares the "ruk" bands.

A tax year without published rules uses the latest year's (thresholds are
frozen anyway); ``rules_for`` marks such years as assumed. Changes already
in law with a known start date are listed in ``SCHEDULED`` and applied from
their date regardless.
"""

from __future__ import annotations

import copy
from datetime import date

CHECKED = "2026-10-28"
SOURCES = {
    "2026": "https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2026-to-2027",
    "2025": "https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2025-to-2026",
    "auto_enrolment": ("https://www.thepensionsregulator.gov.uk/business-advisers/"
                       "automatic-enrolment-guide-for-business-advisers/automatic-enrolment-earnings-threshold"),
    "salary_sacrifice_cap": ("https://www.gov.uk/government/publications/"
                             "salary-sacrifice-reform-for-pension-contributions-effective-from-6-april-2029/"
                             "salary-sacrifice-reform-for-pension-contributions"),
}


def _src(key: str) -> dict:
    return {"source": SOURCES[key], "checked": CHECKED}


# Employee NI rates by category letter: (PT to UEL, above UEL), in bp.
# Category X (no NI due) isn't in the rates table; it means NICs aren't payable.
_NI_CATEGORIES = {
    "A": (800, 200), "B": (185, 200), "C": (0, 0), "F": (800, 200), "H": (800, 200), "I": (185, 200),
    "J": (200, 200), "L": (200, 200), "M": (800, 200), "N": (800, 200), "S": (0, 0), "V": (800, 200),
    "X": (0, 0), "Z": (200, 200),
}

RULES = {
    2025: {
        "label": "2025/26",
        "personal_allowance": {"amount": 1_257_000, "taper_from": 10_000_000, **_src("2025")},
        "income_tax": {
            # (upper limit of the band on taxable income or None for the top band, rate bp)
            "ruk": [(3_770_000, 2000), (12_514_000, 4000), (None, 4500)],
            "scotland": [(282_700, 1900), (1_492_100, 2000), (3_109_200, 2100), (6_243_000, 4200),
                         (12_514_000, 4500), (None, 4800)],
            **_src("2025"),
        },
        "ni": {
            # Per period, pence. Fortnightly is 2 x weekly and four-weekly 4 x weekly.
            "weekly": {"lel": 12_500, "pt": 24_200, "uel": 96_700},
            "monthly": {"lel": 54_200, "pt": 104_800, "uel": 418_900},
            "annual": {"lel": 650_000, "pt": 1_257_000, "uel": 5_027_000},
            "categories": dict(_NI_CATEGORIES),
            "employer_rate": 1500, "employer_st": {"weekly": 9_600, "monthly": 41_700, "annual": 500_000},
            **_src("2025"),
        },
        "student_loans": {
            # Plan 5 repayments start in April 2026, so 2025/26 has no Plan 5 threshold.
            "plan_1": {"annual": 2_606_500, "rate": 900},
            "plan_2": {"annual": 2_847_000, "rate": 900},
            "plan_4": {"annual": 3_274_500, "rate": 900},
            "postgraduate": {"annual": 2_100_000, "rate": 600},
            **_src("2025"),
        },
        "auto_enrolment": {
            "weekly": (12_000, 96_700), "fortnightly": (24_000, 193_400), "four_weekly": (48_000, 386_700),
            "monthly": (52_000, 418_900), "annual": (624_000, 5_027_000), **_src("auto_enrolment"),
        },
    },
    2026: {
        "label": "2026/27",
        "personal_allowance": {"amount": 1_257_000, "taper_from": 10_000_000, **_src("2026")},
        "income_tax": {
            "ruk": [(3_770_000, 2000), (12_514_000, 4000), (None, 4500)],
            "scotland": [(396_700, 1900), (1_695_600, 2000), (3_109_200, 2100), (6_243_000, 4200),
                         (12_514_000, 4500), (None, 4800)],
            **_src("2026"),
        },
        "ni": {
            "weekly": {"lel": 12_900, "pt": 24_200, "uel": 96_700},
            "monthly": {"lel": 55_900, "pt": 104_800, "uel": 418_900},
            "annual": {"lel": 670_800, "pt": 1_257_000, "uel": 5_027_000},
            "categories": dict(_NI_CATEGORIES),
            "employer_rate": 1500, "employer_st": {"weekly": 9_600, "monthly": 41_700, "annual": 500_000},
            **_src("2026"),
        },
        "student_loans": {
            "plan_1": {"annual": 2_690_000, "rate": 900},
            "plan_2": {"annual": 2_938_500, "rate": 900},
            "plan_4": {"annual": 3_379_500, "rate": 900},
            "plan_5": {"annual": 2_500_000, "rate": 900},
            "postgraduate": {"annual": 2_100_000, "rate": 600},
            **_src("2026"),
        },
        "auto_enrolment": {
            "weekly": (12_000, 96_700), "fortnightly": (24_000, 193_400), "four_weekly": (48_000, 386_700),
            "monthly": (52_000, 418_900), "annual": (624_000, 5_027_000), **_src("auto_enrolment"),
        },
    },
}

# Changes already in law, applied from their start date in every later year.
SCHEDULED = {
    "salary_sacrifice_cap": {
        # Pension contributions made by salary sacrifice above £2,000 a year
        # attract employee and employer Class 1 NI. Income tax relief is unchanged.
        "from": date(2029, 4, 6), "annual_cap": 200_000, **_src("salary_sacrifice_cap"),
    },
}

STUDENT_PLANS = ("plan_1", "plan_2", "plan_4", "plan_5")
PERIODS_PER_YEAR = {"weekly": 52, "fortnightly": 26, "four_weekly": 13, "monthly": 12}


def tax_year_start(day: date) -> date:
    return date(day.year if day >= date(day.year, 4, 6) else day.year - 1, 4, 6)


def tax_year_label(start_year: int) -> str:
    return f"{start_year}/{str(start_year + 1)[2:]}"


def rules_for(day_or_year) -> dict:
    """Rules for the tax year containing a date (or starting in a year).

    A later year without published rules copies the latest year and is
    marked ``assumed``; an earlier one copies the earliest.
    """
    year = day_or_year if isinstance(day_or_year, int) else tax_year_start(day_or_year).year
    known = sorted(RULES)
    base_year = year if year in RULES else (known[-1] if year > known[-1] else known[0])
    rules = copy.deepcopy(RULES[base_year])
    rules["year"] = year
    rules["label"] = tax_year_label(year)
    rules["assumed"] = year not in RULES
    rules["based_on"] = RULES[base_year]["label"]
    cap = SCHEDULED["salary_sacrifice_cap"]
    rules["salary_sacrifice_cap"] = cap["annual_cap"] if date(year, 4, 6) >= cap["from"] else None
    return rules


def sections():
    """Every (year, section name, section) with figures, for checking sources."""
    for year, rules in RULES.items():
        for name, section in rules.items():
            if isinstance(section, dict):
                yield year, name, section
    for name, section in SCHEDULED.items():
        yield None, name, section
