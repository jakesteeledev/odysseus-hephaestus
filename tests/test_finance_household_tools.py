"""Finance v3 step 5: the person parameter, children view, manage_members and routing."""

import asyncio
import json
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.tools.finance as tools
import src.tools.finance_household as household
from src.action_intents import classify_tool_intent
from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.finance import children as kids
from src.finance import members as mem
from src.finance import networth as nw
from src.finance import recurring as rec
from src.finance import service as fin
from src.finance.models import ensure_tables
from src.finance.routing import FINANCE_TOOLS
from src.tool_capabilities import ToolEffect, capabilities_for_action
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import requested_capabilities

OWNER = "alice"
TODAY = date(2026, 10, 28)


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    ensure_tables(engine)
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(tools, "_session", _session)
    monkeypatch.setattr(tools, "_today", lambda: TODAY)
    for module in (fin, mem, kids, rec):
        monkeypatch.setattr(module, "_today_for_owner", lambda: TODAY)
    yield factory
    engine.dispose()


def run(fn, args, owner=OWNER):
    return asyncio.run(fn(json.dumps(args), owner=owner))


@pytest.fixture
def home(db_factory):
    """Alice with her wife Sarah, son Alfie (8) and daughter Bea."""
    db = db_factory()
    me = mem.ensure_household(db, OWNER)
    sarah = mem.create_member(db, OWNER, name="Sarah", relationship="wife")
    alfie = mem.create_member(db, OWNER, name="Alfie", relationship="son", date_of_birth="2018-03-01")
    bea = mem.create_member(db, OWNER, name="Bea", relationship="daughter", date_of_birth="2021-07-10")
    joint = fin.create_account(db, OWNER, name="Joint current", opening_balance="1000.01")
    mem.set_owners(db, OWNER, "account", joint.id, [me.id, sarah.id])
    mine = fin.create_account(db, OWNER, name="Alice savings", account_type="savings", opening_balance="500")
    house = nw.create_item(db, OWNER, "asset", name="Home", asset_type="property", value="300000")
    mem.set_owners(db, OWNER, "asset", house.id, [me.id, sarah.id])
    mortgage = nw.create_item(db, OWNER, "liability", name="Mortgage", liability_type="mortgage",
                              linked_asset_id=house.id, value="200000")
    mem.set_owners(db, OWNER, "liability", mortgage.id, [me.id, sarah.id])
    a_isa = nw.create_item(db, OWNER, "asset", name="Alfie JISA", asset_type="investment", subtype="junior isa",
                           value="2000")
    mem.set_owners(db, OWNER, "asset", a_isa.id, [alfie.id])
    b_isa = nw.create_item(db, OWNER, "asset", name="Bea JISA", asset_type="investment", subtype="junior isa",
                           value="750")
    mem.set_owners(db, OWNER, "asset", b_isa.id, [bea.id])
    nw.add_contribution(db, OWNER, a_isa.id, amount="1000", on="2026-04-05")  # last tax year
    nw.add_contribution(db, OWNER, a_isa.id, amount="1200", on="2026-04-06")  # this tax year
    nw.add_contribution(db, OWNER, a_isa.id, amount="300", on="2026-09-01")
    cats = {c["name"]: c["id"] for c in fin.list_categories(db, OWNER)}
    fin.create_transaction(db, OWNER, account_id=joint.id, date="2026-10-03", amount="100.01", direction="out",
                           category_id=cats["Groceries"], description="Tesco")
    fin.create_transfer(db, OWNER, from_account_id=mine.id, to_account_id=joint.id, date="2026-10-04", amount="50")
    db.commit()
    ids = {"me": me.id, "sarah": sarah.id, "alfie": alfie.id, "bea": bea.id, "joint": joint.id, "mine": mine.id,
           "house": house.id}
    db.close()
    return ids


def _worth(result):
    assert result["exit_code"] == 0, result
    return result["data"]["net_worth_pence"]


# ── person ──────────────────────────────────────────────────────────────

HOUSE_NET = 300_000_00 - 200_000_00


def test_household_is_the_default_and_equals_the_sum_of_people(home):
    household_total = _worth(run(tools.do_query_finances, {"view": "net_worth"}))
    assert household_total == 1_000_01 + 500_00 + HOUSE_NET + 2_000_00 + 750_00 - 100_01
    people = [_worth(run(tools.do_query_finances, {"view": "net_worth", "person": who}))
              for who in ("me", "Sarah", "Alfie", "Bea")]
    assert sum(people) == household_total
    for word in ("household", "our", "family", "everyone"):
        assert _worth(run(tools.do_query_finances, {"view": "net_worth", "person": word})) == household_total
    text = run(tools.do_query_finances, {"view": "net_worth"})["response"]
    assert "Showing: the whole household." in text and "By person" in text and "- Sarah:" in text


def test_me_my_and_names(home):
    mine = run(tools.do_query_finances, {"view": "net_worth", "person": "me"})
    assert mine["data"]["person"]["name"] == "Alice"
    assert "Alice (their share" in mine["response"]
    # Joint account: odd penny to the first owner (Alice); her own savings; half the house equity.
    joint_after = 1_000_01 - 100_01 + 50_00  # 950.00
    assert _worth(mine) == joint_after // 2 + (500_00 - 50_00) + HOUSE_NET // 2
    assert _worth(run(tools.do_query_finances, {"view": "net_worth", "person": "my"})) == _worth(mine)
    sarah = run(tools.do_query_finances, {"view": "net_worth", "person": "Sarah's"})
    assert _worth(sarah) == joint_after // 2 + HOUSE_NET // 2
    assert _worth(run(tools.do_query_finances, {"view": "net_worth", "person": "my wife"})) == _worth(sarah)
    assert _worth(run(tools.do_query_finances, {"view": "net_worth", "person": "sar"})) == _worth(sarah)


def test_unknown_or_ambiguous_people_ask(home, db_factory):
    unknown = run(tools.do_query_finances, {"view": "net_worth", "person": "Zed"})
    assert unknown["exit_code"] == 1 and "Members: Alice, Sarah, Alfie, Bea" in unknown["error"]
    assert "Ask the user" in unknown["error"]
    # Members are "child" whether son or daughter, so with two children "my son" asks which one.
    either = run(tools.do_query_finances, {"view": "net_worth", "person": "my son"})
    assert either["exit_code"] == 1 and "Alfie, Bea" in either["error"] and "Ask the user" in either["error"]
    db = db_factory()
    bea = next(m for m in mem.list_members(db, OWNER) if m["name"] == "Bea")
    mem.update_member(db, OWNER, bea["id"], archived=True)
    db.commit()
    db.close()
    kid = run(tools.do_query_finances, {"view": "net_worth", "person": "my son"})
    assert kid["data"]["person"]["name"] == "Alfie"
    husband = run(tools.do_query_finances, {"view": "net_worth", "person": "my husband"})
    assert husband["data"]["person"]["name"] == "Sarah"  # the one partner, whatever the word


def test_person_views_use_shares(home):
    bal = run(tools.do_query_finances, {"view": "balances", "person": "Sarah"})
    assert [a["name"] for a in bal["data"]["accounts"]] == ["Joint current"]
    assert "£475.00 (their share of £950.00)" in bal["response"]
    summary = run(tools.do_query_finances, {"view": "summary", "person": "Sarah"})
    s = summary["data"]["summary"]
    assert (s["total_out_pence"], s["moved_in_pence"]) == (50_00, 25_00)
    assert "Moved between members" in summary["response"]
    alice = run(tools.do_query_finances, {"view": "summary", "person": "me"})["data"]["summary"]
    assert (alice["total_out_pence"], alice["moved_out_pence"]) == (50_01, 25_00)
    tx = run(tools.do_query_finances, {"view": "transactions", "person": "Sarah"})
    assert "(share of £100.01)" in tx["response"]
    assets = run(tools.do_query_finances, {"view": "assets", "person": "Sarah"})
    assert "Home: £150,000.00" in assets["response"] and "(their share of £300,000.00)" in assets["response"]
    household = run(tools.do_query_finances, {"view": "summary"})["data"]["summary"]
    assert household["total_out_pence"] == alice["total_out_pence"] + s["total_out_pence"] == 100_01
    assert household["moved_in_pence"] == household["moved_out_pence"] == 0


def test_children_view(home):
    result = run(tools.do_query_finances, {"view": "children"})
    text = result["response"]
    assert result["exit_code"] == 0
    assert "tax year 2026/27" in text and "Junior ISA allowance £9,000.00 per child" in text
    assert "- Alfie (age 8): £2,000.00 in their name" in text
    assert "Junior ISA Alfie JISA: £2,000.00, locked until 2036-03-01 (their 18th birthday); paid in this tax year £1,500.00" in text
    assert "£1,500.00 paid in of £9,000.00; £7,500.00 of the allowance left" in text
    assert "- Bea (age 5)" in text
    # "the kids" switches an overview to the children view.
    assert run(tools.do_query_finances, {"person": "the kids'"})["data"]["view"] == "children"
    only_alfie = run(tools.do_query_finances, {"view": "children", "person": "Alfie"})
    assert "Alfie" in only_alfie["response"] and "Bea" not in only_alfie["response"]
    assert [c["member"]["name"] for c in only_alfie["data"]["children"]["children"]] == ["Alfie"]
    assert run(tools.do_query_finances, {"view": "jisa"})["data"]["view"] == "children"


def test_single_member_household_reads_as_before(db_factory):
    db = db_factory()
    fin.create_account(db, OWNER, name="Current", opening_balance="10")
    db.commit()
    db.close()
    result = run(tools.do_query_finances, {"view": "net_worth"})
    assert not result["response"].startswith("Showing") and "By person" not in result["response"]
    assert _worth(run(tools.do_query_finances, {"view": "net_worth", "person": "me"})) == 1000


# ── manage_members ──────────────────────────────────────────────────────


def test_manage_members_list_and_add(home):
    listed = run(household.do_manage_members, {})
    assert listed["exit_code"] == 0
    assert "- Alice (Me) (Me" in listed["response"] and "- Alfie (Child, age 8, born 2018-03-01" in listed["response"]
    added = run(household.do_manage_members, {"action": "add", "name": "Gran", "relationship": "other"})
    assert added["exit_code"] == 0 and "Added Gran" in added["response"]
    assert run(household.do_manage_members, {"action": "add", "name": "gran"})["exit_code"] == 1
    assert run(household.do_manage_members, {"action": "add"})["exit_code"] == 1
    assert run(household.do_manage_members, {"action": "explode"})["exit_code"] == 1


def test_manage_members_update_archive_restore(home):
    updated = run(household.do_manage_members, {"action": "update", "member": "Bea", "new_name": "Beatrice",
                                                "date_of_birth": "2021-07-11"})
    assert updated["exit_code"] == 0 and updated["data"]["member"]["name"] == "Beatrice"
    archived = run(household.do_manage_members, {"action": "archive", "member": "my wife"})
    assert archived["exit_code"] == 0 and archived["data"]["member"]["archived"] is True
    assert run(household.do_manage_members, {"action": "restore", "member": "Sarah"})["data"]["member"]["archived"] is False
    assert run(household.do_manage_members, {"action": "archive", "member": "me"})["exit_code"] == 1  # primary
    assert run(household.do_manage_members, {"action": "update", "member": "Nobody", "new_name": "X"})["exit_code"] == 1


def test_manage_members_set_owners(home, db_factory):
    result = run(household.do_manage_members, {"action": "set_owners", "item": "savings", "owners": ["me", "Sarah"]})
    assert result["exit_code"] == 0 and result["data"]["owner_ids"] == [home["me"], home["sarah"]]
    assert "past ones" in result["response"] and "split equally" in result["response"]
    text = run(household.do_manage_members, {"action": "set_owners", "item": "Alfie JISA", "owners": "Alfie"})
    assert text["exit_code"] == 0 and "past ones" not in text["response"]
    joint = run(household.do_manage_members, {"action": "set_owners", "item": "home", "owners": "me and my wife"})
    assert joint["data"]["owner_ids"] == [home["me"], home["sarah"]]
    assert run(household.do_manage_members, {"action": "set_owners", "item": "JISA", "owners": ["me"]})["exit_code"] == 1
    assert run(household.do_manage_members, {"action": "set_owners", "item": "boat", "owners": ["me"]})["exit_code"] == 1
    assert run(household.do_manage_members, {"action": "set_owners", "item": "savings", "owners": []})["exit_code"] == 1
    assert run(household.do_manage_members, {"action": "set_owners", "item": "savings",
                                             "owners": ["Zed"]})["exit_code"] == 1
    db = db_factory()
    assert mem.owners_of(db, OWNER, "account", [home["mine"]])[home["mine"]] == [home["me"], home["sarah"]]
    db.close()


# ── Registration and routing ────────────────────────────────────────────


def test_manage_members_is_registered_like_the_other_finance_tools():
    assert "manage_members" in FINANCE_TOOLS
    assert "manage_members" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    query = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "query_finances")
    assert "person" in query["function"]["parameters"]["properties"]
    assert "children" in query["function"]["parameters"]["properties"]["view"]["enum"]
    assert capabilities_for_action("manage_members", '{"action": "list"}').effects == {ToolEffect.READ_PRIVATE}
    assert capabilities_for_action("manage_members", "{}").effects == {ToolEffect.READ_PRIVATE}
    for action in ("add", "update", "archive", "restore", "set_owners"):
        caps = capabilities_for_action("manage_members", json.dumps({"action": action}))
        assert ToolEffect.WRITE_PRIVATE in caps.effects, action


def test_member_writes_need_approval_once_outside_content_is_in_play():
    from src.tool_capabilities import ToolRunSecurityContext

    tainted = ToolRunSecurityContext()
    tainted.external_untrusted_context_seen = True
    assert not tainted.decision_for("manage_members", '{"action": "set_owners"}').allowed
    assert not tainted.decision_for("manage_members", '{"action": "add", "name": "X"}').allowed


HOUSEHOLD_REQUESTS = [
    "what's our household net worth?",
    "what's my net worth?",
    "what's Sarah's net worth?",
    "how much is in the kids' junior ISAs?",
    "how much have I put in Alfie's junior ISA this tax year?",
    "how much does my son have in savings",
    "set up £5 pocket money for Alfie every week",
    "make the house joint with my wife",
    "add Arabella to the household",
    "what's our joint account balance",
]


@pytest.mark.parametrize("request_text", HOUSEHOLD_REQUESTS)
def test_household_requests_route_to_finance(request_text):
    assert classify_tool_intent(request_text).category == "finance"
    assert "finance" in requested_capabilities(request_text)
    for tool in FINANCE_TOOLS:
        assert create_request_authority(request_text).permits(ExactOperation.normalize(tool, "{}")), tool


@pytest.mark.parametrize("request_text,family", [
    ("add the kids dentist appointment to my calendar", "calendar"),
    ("remind me to buy the kids new shoes", "notes"),
    ("email my wife about dinner", "email"),
])
def test_family_words_alone_do_not_route_to_finance(request_text, family):
    assert classify_tool_intent(request_text).category == family
    assert "finance" not in requested_capabilities(request_text)


@pytest.mark.parametrize("request_text", ["show me photos of the kids", "tell me a story for my children",
                                          "what does my family think of the film"])
def test_family_chat_is_not_promoted(request_text):
    assert classify_tool_intent(request_text).category != "finance"
