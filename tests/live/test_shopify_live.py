"""WF1 against the real Shopify dev store, twice, then a new order through to the low-stock alert.

The tests run in file order and share `state`. Every count is compared with what Shopify itself reports.
Results (counts only) are written to .e2e/live_results.json for the README.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

import pytest

from tests.e2e.stack import E2E_DIR, PROJECT

state: dict = {}
RESULTS = E2E_DIR / "live_results.json"


def record(**kw):
    state.setdefault("results", {}).update(kw)
    RESULTS.write_text(json.dumps(state["results"], indent=2, default=str) + "\n")


def shopify_counts(api) -> dict:
    d = api.gql("""{ ordersCount(limit: null) { count precision }
                    location { inventoryLevels(first: 250) { nodes { quantities(names: ["available"]) { quantity }
                                                                      item { sku } } } } }""")
    levels = {n["item"]["sku"]: n["quantities"][0]["quantity"] for n in d["location"]["inventoryLevels"]["nodes"]}
    return {"orders": d["ordersCount"]["count"], "precision": d["ordersCount"]["precision"], "levels": levels}


def available(api, sku) -> int:
    return shopify_counts(api)["levels"][sku]


# 0. the token endpoint still accepts the JSON body n8n's Custom Auth credential sends (undocumented by Shopify)
def test_token_endpoint_accepts_json_body(real_env):
    body = json.dumps(dict(grant_type="client_credentials", client_id=real_env["SHOPIFY_CLIENT_ID"],
                           client_secret=real_env["SHOPIFY_CLIENT_SECRET"])).encode()
    req = urllib.request.Request(f"https://{real_env['SHOPIFY_SHOP']}/admin/oauth/access_token", data=body,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        answer = json.loads(r.read())
    assert r.status == 200 and answer.get("access_token"), (
        "Shopify no longer accepts a JSON body on the token endpoint: the n8n token sub-workflow will fail. "
        "See README 'Real Shopify store'.")
    assert answer.get("expires_in") == 86399


# 1. first run = full sync; DB matches Shopify
def test_first_run_matches_shopify(shop_stack, api):
    r = shop_stack.run("p8/orders-sync")
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["ok"] and out["source"] == "shopify", out
    want = shopify_counts(api)
    db = shop_stack.q("select (select count(*) from orders)::int orders, (select count(*) from order_items)::int "
                      "items, (select count(*) from orders where cancelled_at is null)::int open")[0]
    assert want["precision"] == "EXACT"
    assert db["orders"] == want["orders"] == out["orders"]["received"]
    stock = {r["sku"]: r["stock"] for r in shop_stack.q("select sku, stock from products")}
    assert len(want["levels"]) == 52
    assert {s: stock[s] for s in want["levels"]} == {s: max(0, q) for s, q in want["levels"].items()}
    tokens = shop_stack.q("select count(*)::int n from shopify_token")[0]["n"]
    assert tokens == 1
    state["run1"] = out
    record(run1=out, db_orders=db["orders"], db_order_items=db["items"], shopify_orders=want["orders"],
           skus=len(want["levels"]))


# 2. second run writes nothing
def test_second_run_writes_zero_rows(shop_stack):
    out = shop_stack.run("p8/orders-sync").json()
    assert out["ok"] and out["orders"]["changed"] == 0 and out["inventory_levels"]["changed"] == 0, out
    record(run2=out)


# 3. a new order lowers stock and shows up in the next low-stock alert exactly once
def pick_sku(shop_stack, api):
    """A SKU that is above its reorder point now and can be pushed below it with the fewest units."""
    rows = shop_stack.q("select sku, stock, reorder_point from products where stock >= reorder_point "
                        "order by stock - reorder_point, sku limit 1")
    return rows[0]


def test_new_order_lowers_stock_and_alerts_once(shop_stack, api):
    target = pick_sku(shop_stack, api)
    qty = target["stock"] - target["reorder_point"] + 1
    before = available(api, target["sku"])
    assert before == target["stock"]
    order = {"email": "live-test@example.com", "processedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "financialStatus": "PAID", "tags": ["p8-synthetic", "p8-live-test"],
             "note": "Synthetic live-test order (P8); cancelled with restock by the test.",
             "lineItems": [{"variantId": shop_stack.variants[target["sku"]]["id"], "quantity": qty}]}
    res = api.gql("""mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
        orderCreate(order: $order, options: $options) { order { id name } userErrors { field message } } }""",
                  {"order": order, "options": {"inventoryBehaviour": "DECREMENT_OBEYING_POLICY",
                                               "sendReceipt": False, "sendFulfillmentReceipt": False}})["orderCreate"]
    assert not res["userErrors"], res["userErrors"]
    state["order_gid"] = res["order"]["id"]
    order_id = int(res["order"]["id"].rsplit("/", 1)[-1])
    assert available(api, target["sku"]) == before - qty

    out = shop_stack.run("p8/orders-sync").json()
    assert out["orders"]["changed"] >= 1 and out["inventory_levels"]["changed"] >= 1, out
    synced = shop_stack.q("select o.name, oi.sku, oi.quantity from orders o join order_items oi on oi.order_id = o.id "
                          "where o.id = %s", (order_id,))
    assert synced == [{"name": res["order"]["name"], "sku": target["sku"], "quantity": qty}]
    stock = shop_stack.q("select stock from products where sku = %s", (target["sku"],))[0]["stock"]
    assert stock == before - qty < target["reorder_point"]

    shop_stack.clear_mail()
    first = shop_stack.run("p8/low-stock").json()
    mail = shop_stack.wait_mail("Low stock")
    assert target["sku"] in mail["HTML"] and "synthetic" in mail["HTML"].lower()
    second = shop_stack.run("p8/low-stock").json()
    time.sleep(2)
    later = [m for m in shop_stack.messages() if "Low stock" in m["Subject"]]
    assert len(later) == 1, [m["Subject"] for m in later]       # the same day: never alerted twice
    alerts = shop_stack.q("select count(*)::int n from alert_log where sku = %s", (target["sku"],))[0]["n"]
    assert alerts == 1
    record(run3=out, live_order=res["order"]["name"], live_sku=target["sku"], live_qty=qty, stock_before=before,
           stock_after=stock, reorder_point=target["reorder_point"], low_stock_first=first, low_stock_second=second)


# 4. n8n kept no token: successful runs are not saved in Shopify mode, the token sub-workflow saves nothing
def test_no_token_in_saved_executions(shop_stack):
    cid = subprocess.run(["docker", "compose", "-p", PROJECT, "ps", "-q", "n8n"], capture_output=True,
                         text=True).stdout.strip()
    js = ('const S=require("sqlite3");const db=new S.Database("/home/node/.n8n/database.sqlite");'
          'db.all("select count(*) n, coalesce(sum(instr(d.data,\'shpca_\')>0 or instr(d.data,\'shpat_\')>0),0) tok '
          'from execution_entity e join execution_data d on d.executionId=e.id",'
          '(e,r)=>console.log(e?e.message:JSON.stringify(r[0])))')
    out = subprocess.run(["docker", "exec", "-w", "/usr/local/lib/node_modules/n8n", cid, "node", "-e", js],
                         capture_output=True, text=True).stdout.strip()
    res = json.loads(out)
    assert res["tok"] == 0, "a saved n8n execution contains a Shopify token"
    record(saved_executions=res["n"], saved_with_token=res["tok"])


# cleanup: cancel the live-test order with restock, so the test can be repeated
@pytest.fixture(scope="module", autouse=True)
def cancel_live_order(api):
    yield
    gid = state.get("order_gid")
    if not gid:
        return
    res = api.gql("""mutation($id: ID!) { orderCancel(orderId: $id, reason: OTHER, restock: true,
        notifyCustomer: false, staffNote: "P8 live test cleanup") { job { id } orderCancelUserErrors { message } } }""",
                  {"id": gid})["orderCancel"]
    assert not res["orderCancelUserErrors"], res["orderCancelUserErrors"]
    sku, before = state.get("results", {}).get("live_sku"), state.get("results", {}).get("stock_before")
    if sku is not None:
        for _ in range(30):
            if available(api, sku) == before:
                break
            time.sleep(2)
        record(restocked_after_cancel=available(api, sku) == before)
