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

FINANCE_TOOLS = frozenset({"query_finances", "add_transaction", "add_transfer"})

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
)
FINANCE_FAMILY_RE = re.compile(FINANCE_FAMILY_PATTERN, re.I)

_ASK = r"\b(?:what(?:'s|\s+is|\s+are|\s+was)?|show|tell\s+me|check|give\s+me|list)\b"

FINANCE_INTENT_PATTERNS = tuple(
    (reason, re.compile(pattern, re.I))
    for reason, pattern in (
        ("net worth question", r"\bnet\s*worth\b"),
        ("finance lookup question",
         rf"{_ASK}.{{0,40}}\bmy\s+(?:balances?|finances|savings|spending|outgoings|income|budget|"
         r"bank\s+accounts?|credit\s+cards?|transactions)\b"),
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
)


def finance_intent(text: str):
    """Return the reason a message is a finance request, or None."""
    for reason, pattern in FINANCE_INTENT_PATTERNS:
        if pattern.search(text or ""):
            return reason
    return None
