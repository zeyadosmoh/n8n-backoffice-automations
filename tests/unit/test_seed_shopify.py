"""scripts/seed_shopify.py: the plan matches the mock catalog, is deterministic, and the inputs are well formed.

Offline: no store is contacted.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mock_store.catalog import build_catalog
from p8.config import ROOT

spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed_shopify.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

CAT = build_catalog(42)
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("Africa/Cairo"))
LOC = "gid://shopify/Location/1"


def test_products_cover_the_mock_catalog_exactly():
    inputs = seed.product_inputs(CAT, LOC)
    variants = [v for p in inputs for v in p["variants"]]
    assert len(inputs) == 19 and len(variants) == len(CAT) == 52
    by_sku = {p.sku: p for p in CAT}
    for v in variants:
        p = by_sku[v["sku"]]
        assert v["price"] == str(p.price)
        assert v["inventoryQuantities"] == [{"locationId": LOC, "name": "available", "quantity": p.initial_stock}]
        assert v["inventoryItem"] == {"tracked": True} and v["inventoryPolicy"] == "DENY"
    assert all(p["tags"] == ["p8-synthetic"] for p in inputs)
    mug = next(p for p in inputs if p["title"] == "Ceramic Mug 350ml")
    assert [o["name"] for o in mug["productOptions"][0]["values"]] == ["Sand", "Olive", "Charcoal", "Terracotta"]


def test_order_plan_is_deterministic_inside_the_window_and_synthetic():
    a = seed.order_plan(CAT, 150, 30, NOW)
    assert a == seed.order_plan(CAT, 150, 30, NOW)
    assert [o["index"] for o in a] == list(range(1, 151))
    stamps = [datetime.fromisoformat(o["processedAt"]) for o in a]
    assert stamps == sorted(stamps)
    assert all(NOW - timedelta(days=30) <= t < NOW for t in stamps)
    assert all(t.hour >= 8 for t in stamps)
    assert all(o["email"].endswith("@example.com") for o in a)
    assert all(1 <= len(o["lines"]) <= 4 and len({li["sku"] for li in o["lines"]}) == len(o["lines"]) for o in a)
    skus = {p.sku for p in CAT}
    assert all(li["sku"] in skus and 1 <= li["quantity"] <= 3 for o in a for li in o["lines"])
    recent = [o for o, t in zip(a, stamps) if t >= NOW - timedelta(days=2)]
    assert not any(o["fulfilled"] for o in recent)          # the last two days stay unfulfilled


def test_order_input_tags_buyer_and_no_customer_record():
    o = seed.order_plan(CAT, 3, 30, NOW)[1]
    vid = {p.sku: f"gid://shopify/ProductVariant/{i}" for i, p in enumerate(CAT, 1)}
    inp = seed.order_input(o, vid)
    assert inp["tags"] == ["p8-synthetic", "p8-seed-0002"]
    assert inp["email"] == o["email"] and "customer" not in inp
    assert inp["financialStatus"] == "PAID" and inp["processedAt"] == o["processedAt"]
    assert [li["variantId"] for li in inp["lineItems"]] == [vid[li["sku"]] for li in o["lines"]]


def test_gid_parsing():
    assert seed.gid_num("gid://shopify/InventoryItem/48000000000001") == 48000000000001
    assert seed.gid_num("gid://shopify/InventoryLevel/1?inventory_item_id=2") == 1
