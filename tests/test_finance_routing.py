"""Finance requests reach the finance tools; other requests do not."""

import pytest

from src.action_intents import classify_tool_intent
from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.finance.routing import FINANCE_TOOLS
from src.tool_capabilities import ToolEffect, capabilities_for_action
from src.tool_index import ToolIndex
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.turn_contract import FAMILY_TOOLS, requested_capabilities

FINANCE_REQUESTS = [
    "what's my net worth?",
    "how much did I spend this month",
    "add £12.50 for lunch to my current account",
    "how much did I spend on subscriptions",
    "what's in my savings account",
    "transfer £200 from current to savings",
    "show me my recent transactions",
    "I spent £4 on coffee",
    "add £5 coffee today",
    # v2
    "how much do I owe on my car?",
    "what's my pension worth",
    "update my ISA to £12,500",
    "my house is now worth £310,000",
    "show me my liabilities",
    "how much is left on my mortgage",
    "what's my student loan balance",
    "list my investments",
    # v2 stage 3
    "what bills are due this week?",
    "what's coming out of my account this month",
    "list my subscriptions",
    "skip netflix this month",
    "what direct debits do I have",
    "add a monthly payment of £10.99 for Netflix",
]

OTHER_REQUESTS = {
    "summarise my unread emails": "email",
    "add a note to buy milk": "notes",
    "add lunch with Sam to my calendar tomorrow": "calendar",
    "remind me to pay rent tomorrow": "notes",
    "show me the email about my pension": "email",
}


def _permits(request, tool):
    return create_request_authority(request).permits(ExactOperation.normalize(tool, "{}"))


@pytest.mark.parametrize("request_text", FINANCE_REQUESTS)
def test_finance_requests_select_the_finance_tools(request_text):
    # Plain chat is promoted to agent mode for these.
    assert classify_tool_intent(request_text).category == "finance"
    assert "finance" in requested_capabilities(request_text)
    for tool in FINANCE_TOOLS:
        assert _permits(request_text, tool), (request_text, tool)


@pytest.mark.parametrize("request_text,family", OTHER_REQUESTS.items())
def test_other_requests_keep_their_own_tools(request_text, family):
    assert classify_tool_intent(request_text).category == family
    families = requested_capabilities(request_text)
    assert family in families and "finance" not in families
    assert not _permits(request_text, "query_finances")


@pytest.mark.parametrize("request_text", [
    "what's the token budget setting", "how do I configure load balancing", "what is a credit score?",
    "set the CSS property to 10px", "what is a mortgage?", "what tasks are due tomorrow",
])
def test_non_money_questions_are_not_promoted_to_finance(request_text):
    assert classify_tool_intent(request_text).category != "finance"


def test_finance_family_registers_the_three_tools():
    assert FAMILY_TOOLS["finance"] == FINANCE_TOOLS == {"query_finances", "add_transaction", "add_transfer",
                                                          "update_valuation", "manage_recurring"}
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert FINANCE_TOOLS <= names


def test_query_is_read_only_and_writes_are_writes():
    """query_finances never needs approval; the add tools are private writes,
    gated by the approval card exactly like notes and calendar writes."""
    read = capabilities_for_action("query_finances", "{}")
    assert read.known and read.effects == {ToolEffect.READ_PRIVATE}
    for tool in ("add_transaction", "add_transfer", "update_valuation"):
        caps = capabilities_for_action(tool, '{"amount": "1"}')
        assert caps.known and ToolEffect.WRITE_PRIVATE in caps.effects
        assert caps.effects == capabilities_for_action("manage_notes", '{"action": "add"}').effects


def test_approval_gate_treats_finance_like_notes():
    """Normal turns run finance tools without a card. Once outside content
    (a web page, an email) is in play, private reads and writes need the
    approval card, the same as Notes: this stops injected text exfiltrating
    or changing the user's data."""
    from src.tool_capabilities import ToolRunSecurityContext

    calm = ToolRunSecurityContext()
    for tool in FINANCE_TOOLS:
        assert calm.decision_for(tool, '{"amount": "1"}').allowed

    tainted = ToolRunSecurityContext()
    tainted.external_untrusted_context_seen = True
    for tool in FINANCE_TOOLS:
        assert not tainted.decision_for(tool, '{"amount": "1"}').allowed
    assert not tainted.decision_for("manage_notes", '{"action": "add"}').allowed
    assert not tainted.decision_for("manage_notes", '{"action": "list"}').allowed


def test_plan_mode_blocks_finance_writes():
    from src.tool_security import plan_mode_disabled_tools

    disabled = plan_mode_disabled_tools()
    assert {"add_transaction", "add_transfer", "update_valuation", "manage_recurring"} <= disabled
    assert "query_finances" not in disabled


def test_native_calls_convert_to_tool_blocks():
    block = function_call_to_tool_block("add_transaction", '{"amount": "12.50", "account": "current"}')
    assert block.tool_type == "add_transaction" and '"12.50"' in block.content


def test_keyword_hints_offer_finance_tools():
    hints = [tools for words, tools in ToolIndex._KEYWORD_HINTS.items() if "net worth" in words]
    assert hints and hints[0] == set(FINANCE_TOOLS)


def test_manage_recurring_list_is_a_read_and_changes_are_writes():
    assert capabilities_for_action("manage_recurring", '{"action": "list"}').effects == {ToolEffect.READ_PRIVATE}
    for action in ("add", "confirm", "skip"):
        assert ToolEffect.WRITE_PRIVATE in capabilities_for_action("manage_recurring", f'{{"action": "{action}"}}').effects
