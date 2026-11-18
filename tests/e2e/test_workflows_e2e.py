"""End to end: real n8n + Postgres + Mailpit + mock store. Tests run in file order and share one stack."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from p8.config import ROOT

from .stack import xlsx_rows

CAIRO = ZoneInfo("Africa/Cairo")
SAMPLES = ROOT / "samples"
TABLES_SNAPSHOT = """
    select md5(coalesce((select string_agg(o::text, '|' order by id) from orders o), '')) as orders,
           md5(coalesce((select string_agg(i::text, '|' order by id) from order_items i), '')) as items,
           md5(coalesce((select string_agg(p.sku || ':' || p.stock || ':' || coalesce(p.stock_updated_at::text, ''),
                                           '|' order by sku) from products p), '')) as stock,
           (select row(cursor_updated_at, cursor_id)::text from sync_state where stream = 'orders') as cursor
"""


def counts(stack):
    return stack.q("select (select count(*) from orders) as orders, (select count(*) from order_items) as items")[0]


def store_orders(stack) -> int:
    return httpx.get(f"{stack.mock}/healthz").json()["orders"]


# --- run-now webhooks --------------------------------------------------------------------------------------------

def test_run_now_webhooks_require_the_token(stack):
    for path in ("p8/orders-sync", "p8/orders-export", "p8/low-stock"):
        assert httpx.post(f"{stack.n8n}/webhook/{path}", timeout=30).status_code in (401, 403)
        r = httpx.post(f"{stack.n8n}/webhook/{path}", headers={"X-P8-Token": "wrong"}, timeout=30)
        assert r.status_code in (401, 403)


# --- workflow 1: orders + inventory sync -------------------------------------------------------------------------

def test_full_sync_then_second_run_changes_nothing(stack):
    stack.calm()
    stack.x("truncate orders, order_items, sync_state cascade")
    r = stack.run("p8/orders-sync")
    assert r.status_code == 200, r.text
    n_store = store_orders(stack)
    # DB state, not the run's counters: the hourly schedule of the same n8n may run at :05 alongside the test
    assert counts(stack)["orders"] == n_store
    assert r.json()["orders"]["received"] >= n_store
    inv = stack.admin("state")["inventory"]
    db_stock = {x["sku"]: x["stock"] for x in stack.q("select sku, stock from products")}
    assert db_stock == {k: max(0, v) for k, v in inv.items()}
    before = stack.q(TABLES_SNAPSHOT)[0]

    second = stack.run("p8/orders-sync").json()
    assert second["orders"]["changed"] == 0 and second["inventory_levels"]["changed"] == 0
    assert stack.q(TABLES_SNAPSHOT)[0] == before


def synced(stack, ids) -> dict[int, str]:
    rows = stack.q("select id, coalesce(fulfillment_status, '-') f from orders where id = any(%s)", (list(ids),))
    return {r["id"]: r["f"] for r in rows}


def test_sync_survives_500s_and_429s(stack):
    stack.calm()
    sim = stack.admin("simulate", {"random_orders": 25, "updated_orders": 5})
    stack.admin("faults", {"fail_next": 2, "bucket_size": 1, "leak_rps": 1, "reset_counters": True})
    r = stack.run("p8/orders-sync")
    stack.calm()
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["retries"] >= 2 and out["rate_limited"] >= 1
    got = synced(stack, sim["created"] + sim["updated"])
    assert set(got) == set(sim["created"]) | set(sim["updated"])
    assert all(got[i] == "fulfilled" for i in sim["updated"])
    assert counts(stack)["orders"] == store_orders(stack)


def test_api_500s_send_failure_email_and_keep_cursor(stack):
    stack.calm()
    stack.clear_mail()
    sim = stack.admin("simulate", {"random_orders": 3})
    before = stack.q(TABLES_SNAPSHOT)[0]
    stack.admin("faults", {"fail_next": 1000})
    r = stack.run("p8/orders-sync")
    stack.calm()
    assert r.status_code == 500
    assert stack.q(TABLES_SNAPSHOT)[0] == before  # no rows, no cursor movement
    msg = stack.wait_mail("Orders sync")
    assert 'failed at "Check response"' in msg["Subject"]
    text = msg["Text"]
    assert "still failing after 5 retries: HTTP 500" in text
    # Change B: the link comes from N8N_EDITOR_BASE_URL (http://localhost:5681 in the e2e stack)
    assert f"Execution: {stack.editor_base}/workflow/p8OrdersSync0001/executions/" in text
    # the next healthy run picks the 3 orders up
    assert stack.run("p8/orders-sync").status_code == 200
    assert set(synced(stack, sim["created"])) == set(sim["created"])


def test_daily_excel_export(stack):
    day = stack.q("select (max(created_at) at time zone 'Africa/Cairo')::date - 1 as d from orders")[0]["d"]
    r = stack.run("p8/orders-export", {"day": str(day)})
    assert r.status_code == 200, r.text
    out = r.json()
    path = stack.exports_dir / f"orders_{day}.xlsx"
    for _ in range(20):
        if path.exists():
            break
        time.sleep(0.5)
    rows = xlsx_rows(path)
    expected = stack.q("select count(*) n from p8_orders_export(%s)", (day,))[0]["n"]
    assert out["rows"] == expected == len(rows) - 1 > 0
    assert rows[0][:3] == ["order_name", "order_id", "created_at_cairo"]
    assert str(rows[1][2]).startswith(str(day))


# --- workflow 2: low-stock alert ---------------------------------------------------------------------------------

def test_low_stock_one_email_sorted_and_once_per_day(stack):
    stack.x("truncate alert_log")
    stack.clear_mail()
    expected = [x["sku"] for x in stack.q("select sku from p8_low_stock()")]
    assert expected, "the seed should have SKUs below their reorder point"
    out = stack.run("p8/low-stock").json()
    assert out["skus"] == expected
    msg = stack.wait_mail("Low stock")
    html = msg["HTML"]
    positions = [html.index(sku) for sku in expected]
    assert positions == sorted(positions)
    assert "synthetic data" in html
    again = stack.run("p8/low-stock").json()
    assert again["alerted"] == 0
    time.sleep(2)
    assert len([m for m in stack.messages() if "Low stock" in m["Subject"]]) == 1


def test_simulated_order_pushes_sku_into_next_alert_once(stack):
    """Change A: inventory levels sync -> products.stock -> next low-stock alert, only once that day."""
    stack.calm()
    cand = stack.q("select sku, stock, reorder_point from products where stock >= reorder_point "
                   "order by stock - reorder_point, sku limit 1")[0]
    qty = cand["stock"] - cand["reorder_point"] + 1
    stack.clear_mail()
    stack.admin("simulate", {"orders": [[{"sku": cand["sku"], "quantity": qty}]]})
    assert stack.run("p8/low-stock").json()["alerted"] == 0  # not synced yet
    sync = stack.run("p8/orders-sync").json()
    assert sync["inventory_levels"]["changed"] >= 1
    assert stack.q("select stock from products where sku = %s", (cand["sku"],))[0]["stock"] == cand["reorder_point"] - 1
    out = stack.run("p8/low-stock").json()
    assert out["skus"] == [cand["sku"]]
    msg = stack.wait_mail("Low stock: 1 SKU")
    assert cand["sku"] in msg["HTML"]
    assert stack.run("p8/low-stock").json()["alerted"] == 0
    today = datetime.now(CAIRO).date()
    n = stack.q("select count(*) n from alert_log where sku = %s and alert_date = %s", (cand["sku"], today))[0]["n"]
    assert n == 1


# --- workflow 3: supplier price list -----------------------------------------------------------------------------

def wait_import(stack, after_id: int, timeout: float = 60) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = stack.q("select * from import_log where id > %s and status <> 'validated' order by id desc limit 1",
                       (after_id,))
        if rows:
            return rows[0]
        time.sleep(0.5)
    raise AssertionError("price import did not finish")


def last_import_id(stack) -> int:
    return stack.q("select coalesce(max(id), 0) m from import_log")[0]["m"]


def test_bad_price_list_rejects_with_reasons(stack):
    stack.clear_mail()
    old = {x["sku"]: float(x["price"]) for x in stack.q("select sku, price from products")}
    start = last_import_id(stack)
    assert stack.upload(SAMPLES / "pricelist_bad.csv").status_code == 200
    imp = wait_import(stack, start)
    assert (imp["status"], imp["rows_total"], imp["rows_valid"], imp["rows_rejected"]) == ("applied", 12, 3, 9)
    msg = stack.wait_mail("pricelist_bad.csv")
    rejects = list(csv.DictReader(io.StringIO(stack.attachment(msg, "rejects_"))))
    assert {int(r["row_no"]): r["reason"] for r in rejects} == {
        4: "price is not a number (call us)",
        5: "price must be greater than 0 (0)",
        6: "price must be greater than 0 (-12.50)",
        7: "price is missing",
        8: "unknown SKU SKU-2999",
        9: "duplicate SKU SKU-1007 in the file (rows 9, 10)",
        10: "duplicate SKU SKU-1007 in the file (rows 9, 10)",
        11: "SKU and name are both empty",
        13: "price is not a number (12,5)",
    }
    new = {x["sku"]: float(x["price"]) for x in stack.q("select sku, price from products")}
    assert new["SKU-1001"] == 189.99 and new["SKU-1002"] == 1250.0 and new["SKU-1012"] == 560.0
    for sku in ("SKU-1003", "SKU-1004", "SKU-1005", "SKU-1006", "SKU-1007", "SKU-1013"):
        assert new[sku] == old[sku]
    logged = stack.q("select sku from price_changes where import_id = %s order by sku", (imp["id"],))
    changed = [s for s in ("SKU-1001", "SKU-1002", "SKU-1012") if old[s] != new[s]]
    assert [x["sku"] for x in logged] == changed


def test_good_price_list_twice_changes_prices_once(stack):
    start = last_import_id(stack)
    stack.upload(SAMPLES / "pricelist_good.csv")
    first = wait_import(stack, start)
    assert first["rows_valid"] == 24 and first["rows_rejected"] == 0
    stack.upload(SAMPLES / "pricelist_good.csv")
    second = wait_import(stack, first["id"])
    assert (second["rows_valid"], second["rows_changed"], second["rows_unchanged"]) == (24, 0, 24)
    assert stack.q("select count(*) n from price_changes where import_id = %s", (second["id"],))[0]["n"] == 0


def test_ai_suggestions_go_to_review_and_are_never_applied(stack):
    stack.clear_mail()
    before = {x["sku"]: float(x["price"]) for x in stack.q("select sku, price from products")}
    start = last_import_id(stack)
    stack.upload(SAMPLES / "pricelist_names_only.xlsx")
    imp = wait_import(stack, start)
    assert imp["ai_enabled"] and imp["rows_review"] == 2 and imp["rows_valid"] == 2
    review = stack.q("select row_no, ai_sku, ai_confidence from import_rows where import_id = %s and status = 'review' "
                     "order by row_no", (imp["id"],))
    assert [(r["row_no"], r["ai_sku"]) for r in review] == [(4, "SKU-1002"), (5, "SKU-1016")]
    after = {x["sku"]: float(x["price"]) for x in stack.q("select sku, price from products")}
    assert after["SKU-1002"] == before["SKU-1002"] and after["SKU-1016"] == before["SKU-1016"]
    msg = stack.wait_mail("pricelist_names_only.xlsx")
    review_csv = list(csv.DictReader(io.StringIO(stack.attachment(msg, "review_"))))
    assert [r["suggested_sku"] for r in review_csv] == ["SKU-1002", "SKU-1016"]
    rejects = {int(r["row_no"]): r["reason"] for r in csv.DictReader(io.StringIO(stack.attachment(msg, "rejects_")))}
    assert rejects[6].endswith("(AI best guess SKU-1005 at 0.75, below 0.85)")


def test_unsupported_file_is_rejected_whole(stack, tmp_path):
    f = tmp_path / "prices.pdf"
    f.write_bytes(b"%PDF-1.4 synthetic")
    start = last_import_id(stack)
    stack.upload(f)
    imp = wait_import(stack, start)
    assert imp["status"] == "rejected" and imp["file_error"].startswith("unsupported file type .pdf")


@pytest.mark.parametrize("stream", ["orders", "inventory_levels"])
def test_cursors_recorded(stack, stream):
    row = stack.q("select cursor_updated_at, last_run_at from sync_state where stream = %s", (stream,))[0]
    assert row["cursor_updated_at"] is not None
    assert datetime.now(CAIRO) - row["last_run_at"] < timedelta(hours=1)
