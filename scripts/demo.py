"""Replay the README scenario against the running dev stack and print the numbers (synthetic data).

    .venv/bin/python scripts/demo.py            # needs: docker compose up, migrate, seed-products, import_workflows.sh

It drives the workflows through their run-now webhooks and the price-list form, exactly like a user or a schedule
would, and leaves every email in Mailpit (http://127.0.0.1:8026) for screenshots. Note: it changes the dev
database (orders, stock, prices) and the mock store's state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from p8.config import ROOT, load_env, pg_dsn

ENV = load_env()
N8N = f"http://127.0.0.1:{ENV.get('N8N_PORT_HOST', '5680')}"
MOCK = f"http://127.0.0.1:{ENV.get('MOCK_STORE_PORT', '8780')}"
MAIL = f"http://127.0.0.1:{ENV.get('MAILPIT_UI_PORT', '8026')}"


def q(sql, params=None):
    with psycopg.connect(pg_dsn(ENV), autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def run(path, body=None):
    t = time.monotonic()
    r = httpx.post(f"{N8N}/webhook/{path}", json=body or {}, timeout=600,
                   headers={"X-P8-Token": ENV["N8N_WEBHOOK_TOKEN"]})
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text), \
        round(time.monotonic() - t, 1)


def admin(what, body=None):
    h = {"X-Admin-Token": ENV["MOCK_ADMIN_TOKEN"]}
    r = httpx.post(f"{MOCK}/_admin/{what}", json=body, headers=h) if body is not None else \
        httpx.get(f"{MOCK}/_admin/{what}", headers=h)
    r.raise_for_status()
    return r.json()


def upload(name):
    path = ROOT / "samples" / name
    start = q("select coalesce(max(id), 0) m from import_log")[0]["m"]
    with path.open("rb") as f:
        httpx.post(f"{N8N}/form/p8-price-list", files={"field-0": (path.name, f)}, timeout=120)
    for _ in range(120):
        rows = q("select * from import_log where id > %s and status <> 'validated' order by id desc limit 1", (start,))
        if rows:
            r = rows[0]
            return {k: r[k] for k in ("id", "status", "rows_total", "rows_valid", "rows_changed", "rows_unchanged",
                                      "rows_rejected", "rows_review", "file_error")}
        time.sleep(0.5)
    raise RuntimeError("import did not finish")


def wait_mail(fragment, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for m in httpx.get(f"{MAIL}/api/v1/messages").json()["messages"]:
            if fragment in m["Subject"]:
                return httpx.get(f"{MAIL}/api/v1/message/{m['ID']}").json()
        time.sleep(0.5)
    return None


def show(title, value):
    print(f"\n== {title}")
    print(json.dumps(value, indent=1, default=str) if not isinstance(value, str) else value)


def main():
    admin("faults", {"fail_rate": 0, "fail_next": 0, "bucket_size": 40, "leak_rps": 2, "reset_counters": True})
    httpx.delete(f"{MAIL}/api/v1/messages")

    code, out, secs = run("p8/orders-sync")
    show(f"1. first sync ({secs}s, HTTP {code})", out)
    code, out, secs = run("p8/orders-sync")
    show(f"2. second sync, nothing new ({secs}s)", out)
    show("   DB", q("select (select count(*) from orders) orders, (select count(*) from order_items) items, "
                   "(select sum(stock) from products) stock")[0])

    admin("simulate", {"random_orders": 30, "updated_orders": 10})
    code, out, secs = run("p8/orders-sync")
    show(f"3. 30 new + 10 updated orders ({secs}s)", {"orders": out["orders"], "inventory": out["inventory_levels"]})

    q("truncate orders, order_items, sync_state cascade")
    admin("faults", {"fail_rate": 0.3, "bucket_size": 2, "leak_rps": 0.5, "reset_counters": True})
    code, out, secs = run("p8/orders-sync")
    st = admin("state")
    admin("faults", {"fail_rate": 0, "bucket_size": 40, "leak_rps": 2})
    show(f"3b. full resync from empty under 30% random 5xx + 2-request bucket at 0.5 req/s ({secs}s, HTTP {code})",
         {"workflow": out, "store counters": st["counters"]})
    show("   DB vs store", {"db_orders": q("select count(*) n from orders")[0]["n"], "store_orders": st["orders"],
                            "db_items": q("select count(*) n from order_items")[0]["n"],
                            "db_stock": q("select sum(stock) s from products")[0]["s"],
                            "store_stock": sum(max(0, v) for v in st["inventory"].values())})
    code, out, secs = run("p8/orders-sync")
    show("   rerun", {"orders_changed": out["orders"]["changed"], "inventory_changed": out["inventory_levels"]["changed"]})

    admin("simulate", {"random_orders": 3})
    before = q("select cursor_updated_at, cursor_id from sync_state where stream = 'orders'")[0]
    admin("faults", {"fail_next": 1000})
    code, out, secs = run("p8/orders-sync")
    admin("faults", {"fail_next": 0})
    after = q("select cursor_updated_at, cursor_id from sync_state where stream = 'orders'")[0]
    mail = wait_mail("Orders sync")
    show(f"4. store API returns 500 on every call ({secs}s, HTTP {code})",
         {"cursor_unchanged": before == after, "failure_email": mail and mail["Subject"],
          "link": mail and [ln for ln in mail["Text"].splitlines() if ln.startswith("Execution:")]})
    code, out, secs = run("p8/orders-sync")
    show("   next healthy run", out["orders"])

    day = q("select (max(created_at) at time zone 'Africa/Cairo')::date - 1 d from orders")[0]["d"]
    code, out, secs = run("p8/orders-export", {"day": str(day)})
    show(f"5. Excel export ({secs}s)", out)

    q("delete from alert_log where alert_date = (now() at time zone 'Africa/Cairo')::date")
    code, out, secs = run("p8/low-stock")
    show(f"6. low-stock alert ({secs}s)", out)
    show("   rows", q("select sku, stock, reorder_point, units_14d, days_left from p8_low_stock()"))
    code, out, secs = run("p8/low-stock")
    show("   same day again", out)

    cand = q("select sku, stock, reorder_point from products where stock >= reorder_point "
             "order by stock - reorder_point, sku limit 1")[0]
    qty = cand["stock"] - cand["reorder_point"] + 1
    admin("simulate", {"orders": [[{"sku": cand["sku"], "quantity": qty}]]})
    sync = run("p8/orders-sync")[1]
    alert = run("p8/low-stock")[1]
    again = run("p8/low-stock")[1]
    show(f"7. simulated order of {qty} x {cand['sku']} ({cand['stock']}/{cand['reorder_point']})",
         {"sync_inventory_changed": sync["inventory_levels"]["changed"],
          "stock_now": q("select stock from products where sku = %s", (cand["sku"],))[0]["stock"],
          "next_alert": alert["skus"], "again_same_day": again["alerted"]})

    show("8. price list: bad file", upload("pricelist_bad.csv"))
    show("   good file", upload("pricelist_good.csv"))
    show("   good file again", upload("pricelist_good.csv"))
    show("   names only (xlsx)", upload("pricelist_names_only.xlsx"))
    print(f"\nEmails in Mailpit: {len(httpx.get(f'{MAIL}/api/v1/messages').json()['messages'])}  ->  {MAIL}")
    print(f"Excel exports: {sorted(p.name for p in Path(ROOT / 'exports').glob('*.xlsx'))}")


if __name__ == "__main__":
    main()
