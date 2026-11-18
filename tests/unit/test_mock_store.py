"""Mock store API: seed determinism, pagination, auth, rate limit, faults, simulate."""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mock_store.app import API, create_app
from mock_store.catalog import build_catalog
from mock_store.store import SHOP_TZ, Store

ANCHOR = datetime(2026, 11, 1, tzinfo=SHOP_TZ)
TOKEN = "shpat_test"
ADMIN = "admin_test"
H = {"X-Shopify-Access-Token": TOKEN}
A = {"X-Admin-Token": ADMIN}


@pytest.fixture
def client():
    app = create_app(seed=42, api_token=TOKEN, admin_token=ADMIN, fail_rate=0, bucket_size=10_000,
                     leak_rps=100, anchor=ANCHOR, fault_seed=1)
    return TestClient(app)


def next_link(resp):
    link = resp.headers.get("link")
    if not link:
        return None
    return link.split(";")[0].strip("<>")


def fetch_all(client, path, params):
    rows, url, n = [], f"{API}/{path}", 0
    resp = client.get(url, params=params, headers=H)
    while True:
        assert resp.status_code == 200, resp.text
        n += 1
        rows.extend(next(iter(resp.json().values())))
        nxt = next_link(resp)
        if not nxt:
            return rows, n
        q = urlparse(nxt)
        resp = client.get(q.path, params={k: v[0] for k, v in parse_qs(q.query).items()}, headers=H)


def test_catalog_and_seed_are_deterministic():
    assert build_catalog(42) == build_catalog(42)
    assert build_catalog(42) != build_catalog(7)
    a, b = Store(seed=42, anchor=ANCHOR), Store(seed=42, anchor=ANCHOR)
    assert [o.to_json() for o in a.orders.values()] == [o.to_json() for o in b.orders.values()]


def test_seed_shape():
    s = Store(seed=42, anchor=ANCHOR)
    assert len(s.products) == 52
    assert 1000 < len(s.orders) < 2000
    assert len({p.sku for p in s.products}) == 52
    assert all(o.updated_at < ANCHOR for o in s.orders.values())
    assert all(o.updated_at >= o.created_at for o in s.orders.values())
    recent = [o for o in s.orders.values() if o.created_at >= ANCHOR - timedelta(days=14)]
    assert len(recent) > 150  # the low-stock alert needs recent sales
    assert any(p.initial_stock < p.reorder_point for p in s.products)


def test_auth_required(client):
    assert client.get(f"{API}/orders.json").status_code == 401
    assert client.get(f"{API}/orders.json", headers={"X-Shopify-Access-Token": "wrong"}).status_code == 401
    assert client.get("/_admin/state").status_code == 401


def test_cursor_pagination_returns_every_order_once_in_order(client):
    rows, pages = fetch_all(client, "orders.json", {"limit": 100})
    assert pages > 10
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) == client.get("/healthz").json()["orders"]
    keys = [(datetime.fromisoformat(r["updated_at"]), r["id"]) for r in rows]
    assert keys == sorted(keys)


def test_updated_at_min_is_inclusive_filter(client):
    rows, _ = fetch_all(client, "orders.json", {"limit": 250})
    cut = rows[len(rows) // 2]["updated_at"]
    later, _ = fetch_all(client, "orders.json", {"limit": 250, "updated_at_min": cut})
    assert later and all(datetime.fromisoformat(r["updated_at"]) >= datetime.fromisoformat(cut) for r in later)
    assert later[0]["updated_at"] == cut


def test_order_shape(client):
    o = client.get(f"{API}/orders.json", params={"limit": 1}, headers=H).json()["orders"][0]
    assert set(o) >= {"id", "name", "created_at", "updated_at", "financial_status", "line_items", "total_price"}
    li = o["line_items"][0]
    assert set(li) == {"id", "sku", "title", "quantity", "price"}
    assert float(o["total_price"]) == pytest.approx(sum(float(x["price"]) * x["quantity"] for x in o["line_items"]))


def test_inventory_levels_paginate(client):
    rows, pages = fetch_all(client, "inventory_levels.json", {"limit": 20})
    assert pages == 3 and len(rows) == 52
    assert {"inventory_item_id", "location_id", "available", "updated_at", "sku"} <= set(rows[0])


def test_bad_page_info_is_400(client):
    assert client.get(f"{API}/orders.json", params={"page_info": "!!"}, headers=H).status_code == 400


def test_rate_limit_429_with_retry_after():
    app = create_app(seed=42, api_token=TOKEN, admin_token=ADMIN, fail_rate=0, bucket_size=3, leak_rps=0.5,
                     anchor=ANCHOR)
    c = TestClient(app)
    codes = [c.get(f"{API}/orders.json", params={"limit": 1}, headers=H) for _ in range(5)]
    assert [r.status_code for r in codes] == [200, 200, 200, 429, 429]
    assert float(codes[3].headers["retry-after"]) >= 1
    assert codes[0].headers["x-shopify-shop-api-call-limit"] == "1/3"
    st = c.get("/_admin/state", headers=A).json()
    assert st["counters"]["rate_limited"] == 2


def test_fail_next_and_random_faults(client):
    client.post("/_admin/faults", json={"fail_next": 2}, headers=A)
    codes = [client.get(f"{API}/orders.json", params={"limit": 1}, headers=H).status_code for _ in range(3)]
    assert codes == [500, 500, 200]
    client.post("/_admin/faults", json={"fail_rate": 1.0, "reset_counters": True}, headers=A)
    assert client.get(f"{API}/orders.json", headers=H).status_code in (500, 502, 503)
    client.post("/_admin/faults", json={"fail_rate": 0.3}, headers=A)
    codes = [client.get(f"{API}/orders.json", params={"limit": 1}, headers=H).status_code for _ in range(200)]
    share = sum(c >= 500 for c in codes) / len(codes)
    assert 0.15 < share < 0.45


def test_simulate_creates_order_and_lowers_inventory(client):
    before = client.get("/_admin/state", headers=A).json()
    sku = "SKU-1001"
    out = client.post("/_admin/simulate", json={"orders": [[{"sku": sku, "quantity": 5}]], "updated_orders": 2},
                      headers=A).json()
    assert len(out["created"]) == 1 and len(out["updated"]) == 2 and out["inventory_changed"] == [sku]
    after = client.get("/_admin/state", headers=A).json()
    assert after["inventory"][sku] == max(0, before["inventory"][sku] - 5)
    assert after["orders"] == before["orders"] + 1
    # the new order and the touched orders are the newest by updated_at
    since = datetime.now(SHOP_TZ).replace(microsecond=0) - timedelta(minutes=1)
    rows, _ = fetch_all(client, "orders.json", {"updated_at_min": since.isoformat()})
    assert {r["id"] for r in rows} == set(out["created"]) | set(out["updated"])
    inv, _ = fetch_all(client, "inventory_levels.json", {"updated_at_min": since.isoformat()})
    assert [r["sku"] for r in inv] == [sku]


def test_simulate_restock_and_unknown_sku(client):
    st = client.get("/_admin/state", headers=A).json()["inventory"]["SKU-1002"]
    client.post("/_admin/simulate", json={"restock": {"SKU-1002": 50}}, headers=A)
    assert client.get("/_admin/state", headers=A).json()["inventory"]["SKU-1002"] == st + 50
    r = client.post("/_admin/simulate", json={"orders": [[{"sku": "NOPE", "quantity": 1}]]}, headers=A)
    assert r.status_code == 422
