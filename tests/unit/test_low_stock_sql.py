"""Low-stock functions: days-left maths and ordering, one alert per SKU per Cairo day, unsent alerts retried."""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from mock_store.store import SHOP_TZ

NOW = datetime(2026, 11, 18, 8, 0, tzinfo=SHOP_TZ)
_next_id = iter(range(9_000_000, 10_000_000))


def order(db, sku, qty, created, cancelled=False):
    oid = next(_next_id)
    o = {"id": oid, "name": f"#{oid}", "created_at": created.isoformat(), "updated_at": created.isoformat(),
         "cancelled_at": created.isoformat() if cancelled else None, "financial_status": "paid",
         "fulfillment_status": None, "currency": "EGP", "total_price": "10.00",
         "line_items": [{"id": oid, "sku": sku, "title": sku, "quantity": qty, "price": "10.00"}]}
    db.execute("select p8_sync_orders_page(%s)", (Jsonb({"orders": [o]}),))


def set_stock(db, sku, stock, reorder):
    db.execute("update products set stock = %s, reorder_point = %s where sku = %s", (stock, reorder, sku))


def healthy(db):
    db.execute("update products set stock = reorder_point + 100")


def low(db, now=NOW):
    return db.execute("select * from p8_low_stock(%s)", (now,)).fetchall()


def claim(db, now=NOW):
    return db.execute("select * from p8_claim_low_stock_alerts(%s)", (now,)).fetchall()


def test_days_left_and_order(db):
    healthy(db)
    set_stock(db, "SKU-1001", 4, 20)   # 28 sold in 14d -> 2/day -> 2.0 days
    set_stock(db, "SKU-1002", 9, 20)   # 14 sold -> 1/day -> 9.0 days
    set_stock(db, "SKU-1003", 0, 10)   # 7 sold -> 0 days
    set_stock(db, "SKU-1004", 5, 10)   # no sales -> NULL, last
    order(db, "SKU-1001", 28, NOW - timedelta(days=3))
    order(db, "SKU-1002", 14, NOW - timedelta(days=13, hours=23))
    order(db, "SKU-1003", 7, NOW - timedelta(hours=1))
    order(db, "SKU-1004", 50, NOW - timedelta(days=15))           # outside the window
    order(db, "SKU-1004", 50, NOW - timedelta(days=1), cancelled=True)  # cancelled orders do not count
    order(db, "SKU-1002", 99, NOW + timedelta(hours=1))           # after p_now
    rows = low(db)
    assert [(r["sku"], r["units_14d"], r["days_left"]) for r in rows] == [
        ("SKU-1003", 7, 0), ("SKU-1001", 28, 2), ("SKU-1002", 14, 9), ("SKU-1004", 0, None)]
    assert rows[1]["avg_daily"] == 2


def test_only_below_reorder_point(db):
    healthy(db)
    set_stock(db, "SKU-1005", 10, 10)  # equal is not below
    set_stock(db, "SKU-1006", 9, 10)
    assert [r["sku"] for r in low(db)] == ["SKU-1006"]


def test_one_alert_per_sku_per_day_and_retry_unsent(db):
    healthy(db)
    set_stock(db, "SKU-1001", 1, 20)
    first = claim(db)
    assert [r["sku"] for r in first] == ["SKU-1001"]
    # email failed: nothing marked sent, so the next run the same day sends it again (still one row)
    assert [r["sku"] for r in claim(db, NOW + timedelta(hours=2))] == ["SKU-1001"]
    db.execute("select p8_mark_alerts_sent(%s)", ([first[0]["alert_id"]],))
    assert claim(db, NOW + timedelta(hours=3)) == []
    # a new SKU going low later the same day is alerted alone
    set_stock(db, "SKU-1002", 0, 20)
    assert [r["sku"] for r in claim(db, NOW + timedelta(hours=4))] == ["SKU-1002"]
    assert db.execute("select count(*) n from alert_log").fetchone()["n"] == 2
    # next Cairo day: both again
    nxt = claim(db, NOW + timedelta(days=1))
    assert sorted(r["sku"] for r in nxt) == ["SKU-1001", "SKU-1002"]


def test_cairo_day_boundary(db):
    healthy(db)
    set_stock(db, "SKU-1001", 1, 20)
    late = datetime(2026, 11, 18, 23, 30, tzinfo=SHOP_TZ)  # 21:30 UTC, still the 18th in Cairo
    r = claim(db, late)
    assert str(r[0]["alert_date"]) == "2026-11-18"
    r2 = claim(db, late + timedelta(hours=1))  # 00:30 Cairo on the 19th
    assert str(r2[0]["alert_date"]) == "2026-11-19"


def test_mark_sent_counts(db):
    healthy(db)
    set_stock(db, "SKU-1001", 1, 20)
    ids = [r["alert_id"] for r in claim(db)]
    assert db.execute("select p8_mark_alerts_sent(%s) n", (ids,)).fetchone()["n"] == 1
    assert db.execute("select p8_mark_alerts_sent(%s) n", (ids,)).fetchone()["n"] == 0
