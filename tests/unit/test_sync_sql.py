"""Orders + inventory sync functions: idempotent upserts, cursor only moves with committed rows."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from mock_store.store import SHOP_TZ, Store

ANCHOR = datetime(2026, 11, 1, tzinfo=SHOP_TZ)


def pages(rows, size=250):
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def apply_orders(db, orders):
    return db.execute("select p8_sync_orders_page(%s) as r", (Jsonb({"orders": orders}),)).fetchone()["r"]


def apply_inventory(db, levels):
    return db.execute("select p8_sync_inventory_page(%s) as r",
                      (Jsonb({"inventory_levels": levels}),)).fetchone()["r"]


@pytest.fixture
def store():
    return Store(seed=42, anchor=ANCHOR)


def all_orders(store, since=None):
    return [o.to_json() for o in store.orders_since(since, None)]


def snapshot(db):
    return (
        db.execute("select * from orders order by id").fetchall(),
        db.execute("select * from order_items order by id").fetchall(),
    )


def test_full_sync_then_rerun_changes_nothing(db, store):
    rows = all_orders(store)
    results = [apply_orders(db, p) for p in pages(rows)]
    assert sum(r["changed"] for r in results) == len(rows)
    n_items = sum(len(o["line_items"]) for o in rows)
    assert db.execute("select count(*) c from order_items").fetchone()["c"] == n_items
    before = snapshot(db)
    synced_at = db.execute("select max(synced_at) m from orders").fetchone()["m"]

    again = [apply_orders(db, p) for p in pages(rows)]
    assert sum(r["changed"] for r in again) == 0
    assert sum(r["items_written"] for r in again) == 0
    assert snapshot(db) == before
    assert db.execute("select max(synced_at) m from orders").fetchone()["m"] == synced_at


def test_cursor_is_newest_committed_row_and_never_moves_back(db, store):
    rows = all_orders(store)
    for p in pages(rows):
        apply_orders(db, p)
    st = db.execute("select * from sync_state where stream = 'orders'").fetchone()
    last = rows[-1]
    assert st["cursor_id"] == last["id"]
    assert st["cursor_updated_at"] == datetime.fromisoformat(last["updated_at"])
    # an older page (overlap window) does not move the cursor back
    apply_orders(db, rows[:10])
    st2 = db.execute("select * from sync_state where stream = 'orders'").fetchone()
    assert (st2["cursor_updated_at"], st2["cursor_id"]) == (st["cursor_updated_at"], st["cursor_id"])
    since = db.execute("select p8_sync_since('orders') s").fetchone()["s"]
    assert since == st["cursor_updated_at"] - timedelta(minutes=5)


def test_empty_page_records_run_but_keeps_cursor(db, store):
    apply_orders(db, all_orders(store)[:5])
    before = db.execute("select cursor_updated_at, cursor_id from sync_state").fetchone()
    r = apply_orders(db, [])
    assert r["received"] == 0 and r["changed"] == 0
    assert db.execute("select cursor_updated_at, cursor_id from sync_state").fetchone() == before


def test_first_empty_run_means_full_sync(db):
    apply_orders(db, [])
    assert db.execute("select p8_sync_since('orders') s").fetchone()["s"] is None


def test_failed_page_leaves_rows_and_cursor_untouched(db, store):
    rows = all_orders(store)
    apply_orders(db, rows[:100])
    before = snapshot(db), db.execute("select cursor_updated_at, cursor_id from sync_state").fetchone()
    bad = [dict(o) for o in rows[100:150]]
    bad[-1] = {**bad[-1], "line_items": [{**bad[-1]["line_items"][0], "quantity": 0}]}  # violates the check
    with pytest.raises(psycopg.errors.CheckViolation):
        apply_orders(db, bad)
    after = snapshot(db), db.execute("select cursor_updated_at, cursor_id from sync_state").fetchone()
    assert after == before


def test_updated_order_replaces_row_and_line_items(db, store):
    rows = all_orders(store)
    apply_orders(db, rows)
    o = dict(rows[0])
    newer = (datetime.fromisoformat(o["updated_at"]) + timedelta(days=200)).isoformat()
    o.update(updated_at=newer, fulfillment_status="fulfilled",
             line_items=[{**o["line_items"][0], "quantity": 9}])
    r = apply_orders(db, [o])
    assert r["changed"] == 1
    items = db.execute("select quantity from order_items where order_id = %s", (o["id"],)).fetchall()
    assert [i["quantity"] for i in items] == [9]
    # an older copy arriving late is ignored
    assert apply_orders(db, [rows[0]])["changed"] == 0
    assert db.execute("select fulfillment_status f from orders where id = %s", (o["id"],)).fetchone()["f"] == \
        "fulfilled"


def test_same_second_update_is_applied(db, store):
    """Two versions with the same updated_at second (store granularity) must not be lost."""
    rows = all_orders(store)
    apply_orders(db, rows[:1])
    o = {**rows[0], "financial_status": "partially_refunded"}
    assert apply_orders(db, [o])["changed"] == 1


def test_inventory_sync_idempotent_and_ordered(db, store):
    levels = [lv.to_json() for lv in store.inventory_since(None, None)]
    first = apply_inventory(db, levels)
    assert first["received"] == 52 and first["changed"] == 52  # stock_updated_at set for the first time
    assert apply_inventory(db, levels)["changed"] == 0
    # newer level wins, older one is ignored, same-second change is applied
    lv = dict(levels[0])
    t = datetime.fromisoformat(lv["updated_at"]) + timedelta(hours=1)
    assert apply_inventory(db, [{**lv, "available": 3, "updated_at": t.isoformat()}])["changed"] == 1
    assert apply_inventory(db, [{**lv, "available": 99}])["changed"] == 0
    assert apply_inventory(db, [{**lv, "available": 2, "updated_at": t.isoformat()}])["changed"] == 1
    assert db.execute("select stock from products where sku = %s", (lv["sku"],)).fetchone()["stock"] == 2
    st = db.execute("select * from sync_state where stream = 'inventory_levels'").fetchone()
    assert st["cursor_updated_at"] == t


def test_inventory_negative_and_unknown(db, store):
    lv = store.inventory_since(None, None)[0].to_json()
    r = apply_inventory(db, [{**lv, "available": -4}, {**lv, "inventory_item_id": 1, "sku": "SKU-X"}])
    assert r["unknown_items"] == 1
    assert db.execute("select stock from products where sku = %s", (lv["sku"],)).fetchone()["stock"] == 0


def test_orders_export_rows_for_a_cairo_day(db, store):
    rows = all_orders(store)
    apply_orders(db, rows)
    day = (ANCHOR - timedelta(days=1)).date()
    out = db.execute("select * from p8_orders_export(%s)", (day,)).fetchall()
    expected = [o for o in rows if datetime.fromisoformat(o["created_at"]).astimezone(SHOP_TZ).date() == day]
    assert len(out) == sum(len(o["line_items"]) for o in expected) > 0
    assert all(r["line_total"] == r["unit_price"] * r["quantity"] for r in out)
