"""Words that route a chat turn to the finance tools.

One place for the finance vocabulary, imported by the shared routing
modules (turn contract, tool index, chat-to-agent promotion, router
prompt) so they cannot drift apart. Pure regex, no imports.

``FINANCE_FAMILY_RE`` decides whether a turn may use the finance tools and
is deliberately broader. ``FINANCE_INTENT_PATTERNS`` decide whether a plain
chat message is promoted to agent mode, so they need a clearer money
question or action (avoiding e.g. "token budget" or "load balance").
"""

from __future__ import annotations

import re

# Projections and salary: read tools first, then the writes behind approval.
PROJECTION_TOOLS = frozenset({"project_finances", "manage_projection_scenario"})
SALARY_TOOLS = frozenset({"query_salary", "update_salary"})
FINANCE_TOOLS = frozenset({"query_finances", "add_transaction", "add_transfer", "update_valuation",
                           "manage_recurring", "set_budget", "manage_members"}) | PROJECTION_TOOLS | SALARY_TOOLS

_MONEY_AMOUNT = r"(?:£\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:pounds?|quid|gbp)\b)"
# v3: people in the household, and money words that make a mention of them a finance question.
_PEOPLE = (r"family|partner|kids?|children|my\s+(?:wife|husband|son|daughter|partner)|"
           r"(?:our|the)\s+(?:kids|children|family)")
_MONEY_WORDS = (r"\b(?:money|net\s*worth|worth|savings|accounts?|isas?|finances|balances?|spending|spent|"
                r"pension|owe|debts?|budget|share|assets)\b")

# Projection questions: the future of the user's money (the engine works these out).
_PROJECTION_WORDS = (
    r"\b(?:projections?|forecasts?|debt[\s-]free|balloon(?:\s+payment)?|can\s+(?:i|we)\s+afford)\b"
    r"|\bproject\s+(?:my|our|the)\b"
    r"|\b(?:in|over|after)\s+(?:the\s+next\s+)?(?:\d{1,2}|one|two|three|five|ten|twenty)\s+years?\b"
    r"|\b(?:by|in)\s+20\d\d\b(?=.{0,60}\b(?:worth|balance|owe|mortgage|loan|debts?|savings|pension|money|cash)\b)"
    r"|\b(?:worth|balance|owe|mortgage|loan|debts?|savings|pension|money|cash)\b.{0,60}\b(?:by|in)\s+20\d\d\b"
    r"|\bwhat\s+if\s+(?:i|we)\b.{0,50}\b(?:earn(?:ed)?|sold?|sell|overpa(?:y|id)|saved?|spent?|spend|stop(?:ped)?|quit|"
    r"lost|hand(?:ed)?\s+(?:the\s+car\s+)?back|refinanc\w*|paid\s+off|pay\s+off|moved?|retire\w*)\b"
    r"|\bwhen\s+(?:will|can|could)\s+(?:i|we)\b.{0,40}\b(?:pay\s+off|paid\s+off|clear|retire|be\s+debt|afford|own)\b"
    r"|\b(?:retire|retirement)\b.{0,30}\b(?:afford|money|pension|savings|when|early|at\s+\d\d)\b"
    r"|\b(?:save|create|add|make|delete|remove|compare|show|list)\b.{0,20}\b(?:projection\s+)?scenarios?\b"
    r"|\bscenarios?\b.{0,60}\b(?:sell|car|house|mortgage|pcp|loan|pay|salary|overpay|balloon)\b"
    r"|\bsell(?:ing)?\s+(?:the|my|our)\s+(?:car|house|home|flat|van)\b|\bpcp\b"
)
# Salary and payroll questions.
_SALARY_WORDS = (
    r"\b(?:take[\s-]?home|net\s+pay|pay\s*slips?|payslips?|tax\s+codes?|paye|income\s+tax|national\s+insurance|"
    r"salary\s+sacrifice|pay\s+rise|payrise|student\s+loan\s+deductions?|pension\s+contributions?|marginal\s+rate|"
    r"personal\s+allowance|wages?|salary)\b"
    r"|\bmy\s+ni\b|\bni\s+(?:contributions?|category|deductions?)\b"
    r"|\b(?:a|my|the)\s+(?:£\s?[\d,.]+k?\s+)?raise\b|\bbonus\b.{0,40}\b(?:tax|take[\s-]?home|pay|net|after)\b"
    r"|\bsacrific\w*\s+\d{1,2}(?:\.\d+)?\s*%"
    r"|\bpension\b.{0,20}\b\d{1,2}(?:\.\d+)?\s*%"
)

FINANCE_FAMILY_PATTERN = (
    r"\b(?:financ(?:e|es|ial)|net\s*worth|outgoings|expenses|income|salary|wages|savings|"
    r"credit\s+cards?|money|bank\s+accounts?|current\s+account|isa)\b"
    r"|\b(?:my|account|bank|total)\s+balances?\b"
    r"|\bbalances?\b(?=.{0,30}\b(?:accounts?|savings|current|card|bank)\b)"
    r"|\bspen(?:d|t|ding)\b|\bearn(?:ed|ings)\b"
    r"|\bmy\s+(?:budget|transactions?|accounts)\b|\b(?:recent|last|latest)\s+transactions\b"
    rf"|{_MONEY_AMOUNT}"
    r"|\btransfer(?:red)?\b.{0,40}\b(?:to|into|from)\s+(?:my\s+)?(?:savings|current|isa|credit\s+card)\b"
    # v2: assets and liabilities.
    r"|\b(?:assets|liabilit(?:y|ies)|equity|mortgages?|loans?|debts?|owe|owed|pensions?|investments|"
    r"student\s+loans?|klarna|clearpay|bnpl|crypto)\b"
    r"|\bmy\s+(?:property|house|home|flat|car|shares|isa|sipp)\b|\b(?:house|property|car|home)\s+value\b"
    r"|\bshares\s+in\b|\bworth\s+(?:now|£|\d)|\bwhat\b.{0,30}\bworth\b"
    # v2 stage 3: recurring items.
    r"|\b(?:recurring|direct\s+debits?|standing\s+orders?|subscriptions|bills)\b"
    r"|\b(?:upcoming|regular)\s+(?:payments?|income)\b|\bcoming\s+out\s+of\s+my\b"
    # v2 stage 4: budgets.
    r"|\b(?:left\s+to\s+spend|overspent|over\s+budget|under\s+budget|spending\s+limit)\b"
    # v3: household members, joint ownership and children.
    r"|\b(?:household|joint\s+(?:accounts?|mortgage|savings|names?)|junior\s+(?:isa|sipp)s?|jisas?|"
    r"pocket\s+money|tax\s+year|child\s+(?:savings|current)\s+accounts?|jointly|who\s+owns)\b"
    r"|\bmake\b.{0,40}\bjoint\b|\bjoint\s+with\b"
    rf"|\b(?:{_PEOPLE})\b(?=.{{0,40}}{_MONEY_WORDS})"
    rf"|{_MONEY_WORDS}(?=.{{0,40}}\b(?:{_PEOPLE})\b)"
    r"|\b[a-z]+(?:'s|s')\s+(?:net\s*worth|savings|isas?|accounts?|pension|money|share|balances?)\b"
    # Projections and salary.
    rf"|{_PROJECTION_WORDS}|{_SALARY_WORDS}"
)
FINANCE_FAMILY_RE = re.compile(FINANCE_FAMILY_PATTERN, re.I)

_ASK = r"\b(?:what(?:'s|\s+is|\s+are|\s+was)?|show|tell\s+me|check|give\s+me|list)\b"
# A message about email ("show me the email about my pension") stays with email.
_NOT_EMAIL = r"^(?!.*\b(?:e-?mails?|inbox|mail|messages?)\b)"
# "How much time/battery is left" is not a money question.
_NOT_TIME = r"^(?!.*\b(?:time|timer|minutes?|hours?|days?|battery|storage|disk|space|tokens?|credits?)\b)"
_V2_NOUNS = (r"assets|liabilit(?:y|ies)|debts?|loans?|mortgage|pensions?|isas?|investments|shares|crypto|"
             r"property|equity|student\s+loan")

FINANCE_INTENT_PATTERNS = tuple(
    (reason, re.compile(_NOT_EMAIL + r"(?s:.*?)" + pattern, re.I))
    for reason, pattern in (
        ("net worth question", r"\bnet\s*worth\b"),
        ("finance lookup question",
         rf"{_ASK}.{{0,40}}\bmy\s+(?:balances?|finances|savings|spending|outgoings|income|budget|"
         rf"bank\s+accounts?|credit\s+cards?|transactions|{_V2_NOUNS})\b"),
        ("worth question", r"\b(?:what(?:'s|\s+is)|how\s+much\s+is)\s+(?:my|our)\s+[\w\s]{1,30}?\bworth\b"),
        ("how much owed question", _NOT_TIME + r".*\bhow\s+much\b.{0,30}\b(?:owe|owed|left\s+on|outstanding)\b"),
        ("update value request",
         rf"\b(?:update|set|change)\b.{{0,50}}\b(?:to|at)\s+{_MONEY_AMOUNT}"),
        ("new value statement", rf"\bmy\b.{{0,40}}\b(?:is|are)\s+(?:now\s+)?(?:worth|at)\s+(?:about\s+)?{_MONEY_AMOUNT}"),
        ("bills due question",
         r"\b(?:bills?|payments?|direct\s+debits?|standing\s+orders?|subscriptions?|rent|salary|pay\s*day)\b"
         r".{0,40}\b(?:due|coming\s+(?:up|out)|upcoming|go(?:ing)?\s+out)\b"),
        ("upcoming payments question", r"\b(?:upcoming|regular|recurring)\s+(?:bills?|payments?|income|items?)\b"),
        ("coming out question", r"\bwhat(?:'s|\s+is)\s+(?:coming|going)\s+out\b"),
        ("list recurring request",
         r"\b(?:my|list|show|what|which)\b.{0,20}\b(?:subscriptions|direct\s+debits|standing\s+orders|"
         r"recurring\s+(?:items|payments|bills))\b"),
        ("skip or confirm occurrence", r"\b(?:skip|confirm)\b.{0,40}\b(?:this|next|last)\s+(?:month|week)(?:'s)?\b"),
        # "token budget" / "time budget" are settings, not money.
        ("budget question",
         r"\bmy\s+budgets?\b|\b(?!(?:token|tokens|time|context|compute|agent|round|tool|gpu|memory|"
         r"the|a|an|this|that|your)\b)[a-z]+\s+budgets?\b"
         r"|\b(?:left\s+to\s+spend|overspent|over\s+budget|under\s+budget|spending\s+limit)\b"),
        ("money left question", _NOT_TIME + r".*\bhow\s+much\b.{0,30}\bleft\b.{0,30}\b(?:for|on|in)\b"),
        ("add recurring request",
         r"\b(?:add|set\s+up|create)\b.{0,30}\b(?:recurring|monthly|weekly|yearly|regular)\s+(?:payment|bill|subscription|income|direct\s+debit)\b"),
        ("how much spent/earned question",
         r"\bhow\s+much\b.{0,40}\b(?:did|have|do|am|was)\s+i\b.{0,30}"
         r"\b(?:spen[dt]|earn(?:ed)?|save[d]?|owe|have|got|left|paid|make|made)\b"),
        ("how much money question", r"\bhow\s+much\s+(?:money|is\s+in|is\s+left|have\s+i\s+got|do\s+i\s+have)\b"),
        ("record money request", rf"\b(?:add|log|record|put|enter)\b.{{0,60}}{_MONEY_AMOUNT}"),
        ("spent/paid statement", rf"\bi\s+(?:just\s+)?(?:spent|paid|earned|got\s+paid|received)\b.{{0,40}}{_MONEY_AMOUNT}"),
        ("money transfer request", rf"\b(?:transfer|move)\b.{{0,30}}{_MONEY_AMOUNT}"),
        ("recent transactions request", r"\b(?:my|recent|last|latest)\s+transactions\b"),
        # v3: household members and children.
        ("junior isa question", r"\b(?:junior\s+(?:isa|sipp)s?|jisas?)\b"),
        ("pocket money question", r"\bpocket\s+money\b"),
        ("household money question",
         rf"\b(?:household|joint)\b.{{0,30}}{_MONEY_WORDS}|{_ASK}.{{0,30}}\bour\s+(?:finances|savings|"
         r"money|balances?|accounts|spending|budget|assets|debts?|mortgage)\b"),
        ("person's money question",
         rf"{_ASK}.{{0,30}}\b(?:[a-z]+(?:'s|s')|my\s+(?:wife|husband|partner|son|daughter)'s)\s+"
         r"(?:net\s*worth|savings|isas?|accounts?|pension|money|balances?|debts?|spending|share)\b"),
        ("joint ownership request",
         r"\b(?:make|set|mark|change)\b.{0,40}\bjoint(?:ly)?\b|\bjoint(?:ly)?\s+(?:owned|with)\b"
         r"|\bin\s+(?:both|our|joint)\s+names\b|\bwho\s+owns\b"),
        ("household member request",
         r"\b(?:add|remove|archive)\b.{0,40}\b(?:to|from)\s+(?:the|my|our)\s+household\b"
         r"|\bhousehold\s+members?\b"),
        # Projections and salary.
        ("salary question", _SALARY_WORDS),
        ("projection question", _PROJECTION_WORDS),
        ("children's money question",
         rf"\b(?:{_PEOPLE})(?:'s|s'|')?\b.{{0,20}}{_MONEY_WORDS}(?!.*\b(?:calendar|appointment|note)\b)"),
    )
)

# Plain keywords for keyword-matching routers (word-boundary matched).
FINANCE_KEYWORDS = (
    "finance", "finances", "money", "net worth", "balance", "balances", "account balance",
    "spent", "spend", "spending", "income", "earned", "salary", "budget", "transaction",
    "transactions", "transfer", "savings", "credit card", "outgoings", "how much", "pounds",
    # v2
    "assets", "liabilities", "worth", "equity", "mortgage", "loan", "debt", "owe", "pension", "isa",
    "investments", "shares", "crypto", "house value", "car value", "student loan", "klarna",
    # v2 stage 3
    "recurring", "subscriptions", "direct debit", "direct debits", "standing order", "bills", "bills due",
    "upcoming payments",
    # v2 stage 4
    "budgets", "left to spend", "overspent", "over budget",
    # v3: household and children
    "household", "family", "partner", "joint", "joint account", "my wife", "my husband", "my son",
    "my daughter", "kids", "children", "junior isa", "junior isas", "jisa", "junior sipp", "pocket money",
    "household members", "tax year",
)

# Plain keywords for the projection and salary tools (substring routers add these first).
PROJECTION_KEYWORDS = (
    "project", "projection", "forecast", "in 5 years", "in 10 years", "years time", "by 2030", "by 2035",
    "will i", "can i afford", "what if", "pay off", "paid off", "debt free", "debt-free", "retire", "balloon",
    "scenario", "scenarios", "pcp", "sell the car", "sell my car", "sell the house",
)
SALARY_KEYWORDS = (
    "salary", "wage", "wages", "payslip", "pay slip", "take-home", "take home", "net pay", "tax code", "paye",
    "income tax", "national insurance", "my ni", "pay rise", "payrise", "a raise", "my raise", "bonus",
    "salary sacrifice", "sacrifice", "pension contribution", "student loan deduction", "marginal rate",
)
_SALARY_FOCUS = re.compile(_SALARY_WORDS, re.I)
_PROJECTION_FOCUS = re.compile(_PROJECTION_WORDS, re.I)


def finance_tool_focus(text: str):
    """The most specific finance read tool for a message: ``query_salary`` for
    pay, tax and payslip questions, ``project_finances`` for anything about
    the future, otherwise ``query_finances`` (None when it isn't finance)."""
    text = text or ""
    # "what if I earned £1,000 more a month" is a projection; "how much would a
    # £3k pay rise change my take-home" is salary. Salary words win when both match.
    if _SALARY_FOCUS.search(text):
        return "query_salary"
    if _PROJECTION_FOCUS.search(text):
        return "project_finances"
    if FINANCE_FAMILY_RE.search(text):
        return "query_finances"
    return None


def finance_intent(text: str):
    """Return the reason a message is a finance request, or None."""
    for reason, pattern in FINANCE_INTENT_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None
