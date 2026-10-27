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

FINANCE_TOOLS = frozenset({"query_finances", "add_transaction", "add_transfer", "update_valuation",
                           "manage_recurring"})

_MONEY_AMOUNT = r"(?:£\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:pounds?|quid|gbp)\b)"

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
)
FINANCE_FAMILY_RE = re.compile(FINANCE_FAMILY_PATTERN, re.I)

_ASK = r"\b(?:what(?:'s|\s+is|\s+are|\s+was)?|show|tell\s+me|check|give\s+me|list)\b"
# A message about email ("show me the email about my pension") stays with email.
_NOT_EMAIL = r"^(?!.*\b(?:e-?mails?|inbox|mail|messages?)\b)"
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
        ("how much owed question", r"\bhow\s+much\b.{0,30}\b(?:owe|owed|left\s+on|outstanding)\b"),
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
)


def finance_intent(text: str):
    """Return the reason a message is a finance request, or None."""
    for reason, pattern in FINANCE_INTENT_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None
