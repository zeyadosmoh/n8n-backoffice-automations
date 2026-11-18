"""WF1 Shopify branch: GraphQL answers -> the mock's staging shape -> the unchanged SQL functions.

Runs the shipped Code-node scripts under node with the fixtures in tests/fixtures/shopify (synthetic data, the
GraphQL Admin API 2026-10 response shape). No network.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from p8.config import ROOT

from ..jsnode import raises, run_code

FIX = ROOT / "tests" / "fixtures" / "shopify"
CURSORS = {"orders_since": "2026-11-19T06:55:00+00:00", "inventory_since": None}


def _mock_order():
    from decimal import Decimal

    from mock_store.store import LineItem, Order

    ts = datetime(2026, 11, 19, tzinfo=timezone.utc)
    return Order(1, 1001, ts, ts, "paid", None, None, [LineItem(1, "SKU-1001", "x", 1, Decimal("1.00"))]).to_json()


MOCK_ORDER = _mock_order()


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


def http(name=None, status=200, body=None, headers=None):
    return {"statusCode": status, "headers": headers or {}, "body": fixture(name) if name else body}


def start_state():
    [st] = run_code("shopify_start_streams", {"token": "shpca_" + "0" * 32, "cached": True},
                    nodes={"Load cursors": CURSORS})
    return st


def check(state, response):
    return run_code("shopify_check_response", response, nodes={"Next GraphQL request": state})[0]


def next_page(routed, sql_result=None):
    json_in = {"result": sql_result} if sql_result is not None else routed
    return run_code("shopify_next_page", json_in, nodes={"Route Shopify page": routed})[0]


def apply_sql(db, out):
    fn = "p8_sync_orders_page" if out["stream"] == "orders" else "p8_sync_inventory_page"
    return db.execute(f"select {fn}(%s) as r", (Jsonb(out["payload"]),)).fetchone()["r"]


# --- start -------------------------------------------------------------------------------------------------------
def test_start_builds_cursor_filters_and_keeps_the_token_out_of_the_state():
    st = start_state()
    assert [s["stream"] for s in st["streams"]] == ["orders", "inventory_levels"]
    assert st["streams"][0]["q"] == "updated_at:>='2026-11-19T06:55:00.000Z'"
    assert st["streams"][1]["q"] is None                         # no cursor yet = full sync
    assert "sortKey: UPDATED_AT" in st["streams"][0]["query"] and "lineItems(first: 50)" in st["streams"][0]["query"]
    assert "shpca_" not in json.dumps(st)
    assert st["source"] == "shopify" and st["idx"] == 0 and st["after"] is None


# --- mapping -----------------------------------------------------------------------------------------------------
def test_orders_page_maps_to_the_mock_staging_shape():
    out = check(start_state(), http("orders_page1"))
    assert out["action"] == "apply" and out["stream"] == "orders"
    assert out["nextAfter"] == fixture("orders_page1")["data"]["orders"]["pageInfo"]["endCursor"]
    o = out["payload"]["orders"][0]
    assert o == {
        "id": 6000000000001, "name": "#1001",
        "created_at": "2026-10-25T09:15:00Z",            # processedAt, not createdAt (the import moment)
        "updated_at": "2026-11-19T08:00:01Z", "cancelled_at": None,
        "financial_status": "paid", "fulfillment_status": None, "currency": "EGP", "total_price": "1309.97",
        "line_items": [
            {"id": 15000000000001, "sku": "SKU-1001", "title": "Ceramic Mug 350ml - Sand", "quantity": 2,
             "price": "179.99"},
            {"id": 15000000000002, "sku": "SKU-1010", "title": "Cotton Throw Blanket - Cream", "quantity": 1,
             "price": "949.99"}]}
    # every key the SQL reads from a mock REST order is there, with the same shape
    assert set(o) == set(MOCK_ORDER) - {"order_number", "subtotal_price"}
    assert set(o["line_items"][0]) == set(MOCK_ORDER["line_items"][0])


def test_status_enums_and_missing_sku():
    o = check(start_state(), http("orders_page2"))["payload"]["orders"][0]
    assert o["financial_status"] == "refunded" and o["fulfillment_status"] == "partial"
    assert o["cancelled_at"] == "2026-11-19T08:02:00Z"
    assert o["line_items"][1]["sku"] == ""                       # custom line without SKU: kept, never NULL


def test_more_than_50_line_items_fails_without_writing():
    resp = http("orders_page1")
    resp["body"]["data"]["orders"]["nodes"][0]["lineItems"]["pageInfo"]["hasNextPage"] = True
    assert "more than 50 line items" in raises("shopify_check_response", json_in=resp,
                                               nodes={"Next GraphQL request": start_state()})


def test_inventory_pages_are_collected_then_applied_once():
    st = {**start_state(), "idx": 1}
    first = check(st, http("inventory_page1"))
    assert first["action"] == "next" and len(first["buffer"]) == 2 and "payload" not in first
    st2 = next_page(first)
    assert st2["after"] == "aW52LXBhZ2UtMg==" and st2["idx"] == 1 and st2["done"] is False
    last = check(st2, http("inventory_page2"))
    assert last["action"] == "apply" and last["buffer"] == []
    levels = last["payload"]["inventory_levels"]
    assert [lv["sku"] for lv in levels] == ["SKU-1001", "SKU-1005", "SKU-1010", "UNKNOWN-SKU"]
    assert levels[0] == {"inventory_item_id": 48000000000001, "sku": "SKU-1001", "available": 41,
                         "updated_at": "2026-11-19T08:02:00Z"}


# --- throttling and retries ----------------------------------------------------------------------------------------
def test_low_points_after_a_page_ask_for_a_pause():
    out = check(start_state(), http("orders_page2"))           # 20 points left, the query costs 62, +100/s
    assert out["pauseSeconds"] == 0.9                          # (62-20)/100 + 0.5 s margin, to 0.1 s
    assert check(start_state(), http("orders_page1"))["pauseSeconds"] == 0


def test_throttled_waits_for_the_points_then_gives_up_after_20():
    out = check(start_state(), http("throttled"))
    assert out["action"] == "retry" and out["lastError"] == "THROTTLED"
    assert out["waitSeconds"] == 1                              # (62-12)/100+0.5 = 1.0 s, never below 1 s
    assert out["rateLimited"] == 1 and out["after"] is None    # same page again
    st = {**start_state(), "throttled": 20}
    assert "still THROTTLED after 20 waits" in raises("shopify_check_response", json_in=http("throttled"),
                                                      nodes={"Next GraphQL request": st})


def test_429_uses_retry_after():
    out = check(start_state(), http(status=429, body={}, headers={"retry-after": "4"}))
    assert out["action"] == "retry" and out["waitSeconds"] == 4


@pytest.mark.parametrize("status", [500, 502, 503, 0])
def test_5xx_and_network_errors_back_off_exponentially_then_fail(status):
    st = start_state()
    waits = []
    for _ in range(5):
        st = check(st, http(status=status, body={}))
        assert st["action"] == "retry"
        waits.append(st["waitSeconds"])
    assert waits == [1, 2, 4, 8, 16]                            # jitter is 0 under the test harness
    assert "still failing after 5 retries" in raises("shopify_check_response", json_in=http(status=status, body={}),
                                                     nodes={"Next GraphQL request": st})


def test_401_asks_to_forget_the_cached_token():
    out = check(start_state(), http("unauthorized", status=401))
    assert out["action"] == "reauth"
    msg = raises("shopify_token_rejected", json_in={})
    assert "HTTP 401" in msg and "cursor was not moved" in msg


def test_graphql_errors_fail_with_the_message():
    msg = raises("shopify_check_response", json_in=http("graphql_error"), nodes={"Next GraphQL request": start_state()})
    assert "Field 'colour' doesn't exist" in msg and "HTTP 200" in msg


def test_403_fails_without_retry():
    msg = raises("shopify_check_response", json_in=http(status=403, body={"errors": "Access denied for orders"}),
                 nodes={"Next GraphQL request": start_state()})
    assert "HTTP 403" in msg and "Access denied" in msg


# --- through the unchanged SQL: same cursor and idempotency rules as the mock ---------------------------------------
def run_pages(db):
    st = start_state()
    for name in ["orders_page1", "orders_page2"]:
        out = check(st, http(name))
        st = next_page(out, apply_sql(db, out))
    for name in ["inventory_page1", "inventory_page2"]:
        out = check(st, http(name))
        st = next_page(out, apply_sql(db, out) if out["action"] == "apply" else None)
    assert st["done"] is True
    return st


def test_full_run_through_sql_and_rerun_writes_nothing(db):
    db.execute("update products set inventory_item_id = 48000000000000 + substr(sku, 5)::int - 1000")
    st = run_pages(db)
    t = st["totals"]
    assert t["orders"] == {"pages": 2, "received": 3, "changed": 3, "cursor": t["orders"]["cursor"]}
    assert t["inventory_levels"]["pages"] == 2 and t["inventory_levels"]["received"] == 4
    assert t["inventory_levels"]["changed"] == 3                 # UNKNOWN-SKU is reported, not written
    assert db.execute("select count(*) n from order_items").fetchone()["n"] == 5
    stock = {r["sku"]: r["stock"] for r in db.execute(
        "select sku, stock from products where sku in ('SKU-1001','SKU-1005','SKU-1010')")}
    assert stock == {"SKU-1001": 41, "SKU-1005": 7, "SKU-1010": 55}
    created = db.execute("select created_at from orders where id = 6000000000001").fetchone()["created_at"]
    assert created == datetime(2026, 10, 25, 9, 15, tzinfo=timezone.utc)   # processedAt
    cursor = db.execute("select cursor_updated_at, cursor_id from sync_state where stream='orders'").fetchone()
    assert cursor["cursor_id"] == 6000000000003

    again = run_pages(db)["totals"]
    assert again["orders"]["changed"] == 0 and again["inventory_levels"]["changed"] == 0


def test_next_page_moves_through_streams_and_passes_the_pause():
    st = start_state()
    out = check(st, http("orders_page2"))                        # last orders page, low points
    nxt = next_page(out, {"received": 1, "changed": 1, "cursor": {"cursor_updated_at": "2026-11-19T08:02:00+00:00"}})
    assert nxt["idx"] == 1 and nxt["after"] is None and nxt["done"] is False
    assert nxt["waitSeconds"] == pytest.approx(0.9)
    assert "payload" not in nxt and "action" not in nxt


# --- workflow wiring -----------------------------------------------------------------------------------------------
WF = json.loads((ROOT / "workflows" / "orders_sync.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
CONN = WF["connections"]


def targets(node, out=0):
    return [c["node"] for c in CONN[node]["main"][out]]


def test_store_source_switch_defaults_to_the_mock():
    cond = NODES["Shopify store?"]["parameters"]["conditions"]["conditions"][0]
    assert cond["leftValue"] == "={{ $env.STORE_SOURCE }}" and cond["rightValue"] == "shopify"
    assert targets("Shopify store?", 0) == ["Get Shopify token"]
    assert targets("Shopify store?", 1) == ["Start streams"]    # anything but "shopify" -> mock


def test_both_branches_use_the_same_sql_and_summary():
    q = lambda n: NODES[n]["parameters"]["query"]  # noqa: E731
    assert q("Apply page (one transaction)") == q("Apply Shopify page (one transaction)")
    assert targets("More to fetch?", 1) == targets("More Shopify pages?", 1) == ["Sync summary"]


def test_shopify_loop_wiring():
    assert NODES["Get Shopify token"]["parameters"]["workflowId"]["value"] == "p8ShopifyToken01"
    assert targets("Route Shopify page", 0) == ["Apply Shopify page (one transaction)"]
    assert targets("Route Shopify page", 1) == ["Next Shopify page?"]
    assert targets("Route Shopify page", 2) == ["Wait (Shopify)"]
    assert targets("Route Shopify page", 3) == ["Forget cached token"]
    assert targets("Wait (Shopify)") == ["Next GraphQL request"]
    assert targets("Pause for points?", 0) == ["Wait (Shopify)"]
    assert targets("Pause for points?", 1) == ["Next GraphQL request"]
    url = NODES["Fetch GraphQL page"]["parameters"]["url"]
    assert "{{ $env.SHOPIFY_API_VERSION }}/graphql.json" in url


# --- import prep (scripts/prepare_import.py) -----------------------------------------------------------------------
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("prep", ROOT / "scripts" / "prepare_import.py")
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
ENV = {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "POSTGRES_DB": "p8", "STORE_API_TOKEN": "t",
       "N8N_WEBHOOK_TOKEN": "w"}


def pg(env):
    return next(c for c in prep.credentials(env) if c["id"] == "p8PostgresCred01")["data"]["database"]


def test_mock_mode_is_unchanged():
    assert pg(ENV) == "p8"
    assert prep.prepare_workflow(copy.deepcopy(WF), ENV)["settings"] == WF["settings"]


def test_shopify_mode_uses_its_own_database_and_saves_failed_runs_only():
    env = {**ENV, "STORE_SOURCE": "shopify"}
    assert pg(env) == "p8_shopify" and pg({**env, "SHOPIFY_POSTGRES_DB": "p8_shop2"}) == "p8_shop2"
    s = prep.prepare_workflow(copy.deepcopy(WF), env)["settings"]
    assert s["saveDataSuccessExecution"] == "none" and s["saveDataErrorExecution"] == "all"
    assert s["saveManualExecutions"] is False and s["errorWorkflow"] == "p8ErrorHandler01"
    other = json.loads((ROOT / "workflows" / "low_stock_alert.json").read_text())
    assert prep.prepare_workflow(other, env) == other


def test_shopify_app_credential_carries_the_client_fields_in_the_body():
    fake = "synthetic-test-value"
    env = {**ENV, "SHOPIFY_CLIENT_ID": "id-123", "SHOPIFY_CLIENT_SECRET": fake}
    cred = next(c for c in prep.credentials(env) if c["id"] == "p8ShopifyApp0001")
    assert cred["type"] == "httpCustomAuth"
    assert json.loads(cred["data"]["json"]) == {"body": dict(client_id="id-123", client_secret=fake)}
    unset = next(c for c in prep.credentials(ENV) if c["id"] == "p8ShopifyApp0001")
    assert json.loads(unset["data"]["json"])["body"]["client_secret"] == "not-configured"


def test_bad_store_source_is_refused():
    with pytest.raises(SystemExit):
        prep.credentials({**ENV, "STORE_SOURCE": "shopfy"})
