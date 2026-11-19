"""Seed the Shopify dev store with the synthetic catalog and ~150 synthetic test orders (GraphQL Admin API).

    python3 scripts/seed_shopify.py --dry-run          # read the store, print the plan, write nothing
    python3 scripts/seed_shopify.py                    # create what is missing (idempotent; resumable)

What it creates (everything tagged "p8-synthetic"; every name and email is invented):
  * the same 52 SKUs as the mock seed (mock_store/catalog.py): one product per product type, one variant per SKU,
    same prices, tracked inventory at the store's primary location set to the mock's starting stock.
    Reorder points have no Shopify field; they live in Postgres (`python -m p8 seed-products --source shopify`).
  * ~150 orders spread over the last 30 days (processedAt, inside read_orders' 60-day window), 1-4 lines picked by
    the catalog's popularity weights, buyer = a synthetic @example.com email on the order (no customer record),
    paid, most of them fulfilled. Inventory behaviour BYPASS: like the mock's seeded history, past orders do not
    move the starting stock. No receipts are sent.
  * then writes Shopify's inventory item ids into products in SHOPIFY_POSTGRES_DB, so WF1 can match levels.

Idempotent: products are skipped when a "p8-synthetic" product with the same title exists; order N is skipped when
an order tagged "p8-seed-NNNN" exists. Development stores accept at most 5 new orders per minute, so orders are
paced at one per 12.5 s (~32 min for 150) and the script can be stopped and re-run at any time.

Never prints the client secret or the access token.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mock_store.catalog import PRODUCT_TYPES, Product, build_catalog  # noqa: E402
from p8.config import db_name, load_env  # noqa: E402

TAG = "p8-synthetic"
VENDOR = "P8 Synthetic Goods"
CAIRO = ZoneInfo("Africa/Cairo")
ORDER_PACE_S = 12.5            # dev stores: max 5 new orders per minute


# --- API client ----------------------------------------------------------------------------------------------------
class ShopifyError(RuntimeError):
    pass


class Shopify:
    """Minimal GraphQL Admin API client: client-credentials token, cost-aware throttling, 5xx backoff."""

    def __init__(self, shop: str, client_id: str, client_secret: str, version: str):
        self.shop, self.version = shop, version
        self._client_id, self._client_secret = client_id, client_secret
        self._token: str | None = None

    def __repr__(self) -> str:                 # never show the secret or the token in tracebacks / test output
        return f"Shopify(shop={self.shop!r}, version={self.version!r})"

    def _post(self, url: str, data: bytes, headers: dict) -> tuple[int, dict, dict]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            return e.code, body, dict(e.headers)

    def token(self) -> str:
        if self._token is None:
            data = urllib.parse.urlencode(dict(grant_type="client_credentials", client_id=self._client_id,
                                               client_secret=self._client_secret)).encode()
            code, body, _ = self._post(f"https://{self.shop}/admin/oauth/access_token", data,
                                       {"Content-Type": "application/x-www-form-urlencoded"})
            if code != 200 or not body.get("access_token"):
                raise ShopifyError(f"token request failed: HTTP {code} {str(body.get('error', ''))[:60]}")
            self._token = body["access_token"]
        return self._token

    def gql(self, query: str, variables: dict | None = None) -> dict:
        url = f"https://{self.shop}/admin/api/{self.version}/graphql.json"
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        attempt = throttled = 0
        while True:
            code, body, headers = self._post(url, payload, {"Content-Type": "application/json",
                                                            "X-Shopify-Access-Token": self.token()})
            cost = (body.get("extensions") or {}).get("cost") or {}
            errors = body.get("errors") if isinstance(body.get("errors"), list) else []
            if code == 200 and not errors:
                ts = cost.get("throttleStatus") or {}
                need = (cost.get("requestedQueryCost") or 0) - ts.get("currentlyAvailable", 1e9)
                if need > 0 and ts.get("restoreRate"):
                    time.sleep(need / ts["restoreRate"] + 0.5)
                return body["data"]
            if code == 429 or any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                throttled += 1
                if throttled > 20:
                    raise ShopifyError("still THROTTLED after 20 waits")
                ts = cost.get("throttleStatus") or {}
                missing = (cost.get("requestedQueryCost") or 0) - ts.get("currentlyAvailable", 0)
                time.sleep(max(1.0, float(headers.get("Retry-After") or 0),
                               missing / ts["restoreRate"] + 0.5 if ts.get("restoreRate") else 1.0))
                continue
            if code >= 500:
                attempt += 1
                if attempt > 5:
                    raise ShopifyError(f"HTTP {code} after 5 retries")
                time.sleep(2 ** (attempt - 1))
                continue
            msg = "; ".join(e.get("message", "") for e in errors) or str(body.get("errors", ""))[:200]
            raise ShopifyError(f"HTTP {code}: {msg}")


# --- plan ---------------------------------------------------------------------------------------------------------
def product_inputs(catalog: list[Product], location_id: str) -> list[dict]:
    """One productSet input per product type (title = type), one variant per SKU."""
    by_type: dict[str, list[Product]] = defaultdict(list)
    for p in catalog:
        by_type[p.name.split(" - ", 1)[0]].append(p)
    inputs = []
    for type_name, _base, _variants in PRODUCT_TYPES:
        items = by_type[type_name]
        names = [p.name.split(" - ", 1)[1] for p in items]
        inputs.append({
            "title": type_name, "vendor": VENDOR, "productType": "Home goods (synthetic)", "status": "ACTIVE",
            "tags": [TAG], "descriptionHtml": "<p>Synthetic demo product (P8 portfolio project).</p>",
            "productOptions": [{"name": "Variant", "values": [{"name": n} for n in names]}],
            "variants": [{
                "optionValues": [{"optionName": "Variant", "name": n}],
                "sku": p.sku, "price": str(p.price), "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True},
                "inventoryQuantities": [{"locationId": location_id, "name": "available",
                                         "quantity": p.initial_stock}],
            } for p, n in zip(items, names)],
        })
    return inputs


def order_plan(catalog: list[Product], n: int, days: int, now: datetime, seed: int = 42) -> list[dict]:
    """n synthetic orders over the last `days` days (oldest first), deterministic for a given seed and day."""
    rng = random.Random(seed * 104729)
    weights = [p.popularity for p in catalog]
    now = now.astimezone(CAIRO)
    first_day = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    stamps: list[datetime] = []
    while len(stamps) < n:                                          # shop hours 08:00-23:59 Cairo, in the past
        ts = first_day + timedelta(days=rng.randint(1, days), seconds=rng.randint(8 * 3600, 24 * 3600 - 1))
        if ts < now - timedelta(hours=1):
            stamps.append(ts)
    stamps.sort()
    plan = []
    for i, ts in enumerate(stamps, start=1):
        n_lines = rng.choices([1, 2, 3, 4], weights=[55, 28, 12, 5])[0]
        chosen: dict[str, Product] = {}
        while len(chosen) < n_lines:
            p = rng.choices(catalog, weights=weights)[0]
            chosen[p.sku] = p
        lines = [(p, rng.choices([1, 2, 3], weights=[80, 15, 5])[0]) for p in chosen.values()]
        old_enough = ts < now - timedelta(days=2)
        plan.append({
            "index": i, "processedAt": ts.isoformat(timespec="seconds"),
            "email": f"buyer{rng.randint(1, 60):03d}@example.com",
            "fulfilled": old_enough and rng.random() < 0.85,
            "lines": [{"sku": p.sku, "quantity": q} for p, q in lines],
        })
    return plan


def order_input(o: dict, variant_ids: dict[str, str]) -> dict:
    inp = {
        "email": o["email"], "processedAt": o["processedAt"], "financialStatus": "PAID",
        "tags": [TAG, f"p8-seed-{o['index']:04d}"], "note": "Synthetic test order (P8 portfolio project).",
        "lineItems": [{"variantId": variant_ids[li["sku"]], "quantity": li["quantity"]} for li in o["lines"]],
    }
    if o["fulfilled"]:
        inp["fulfillmentStatus"] = "FULFILLED"
    return inp


# --- store reads --------------------------------------------------------------------------------------------------
Q_SHOP = """{ shop { currencyCode ianaTimezone plan { partnerDevelopment } } location { id name } }"""
Q_PRODUCTS = """query($after: String) {
  products(first: 50, after: $after, query: "tag:p8-synthetic") {
    pageInfo { hasNextPage endCursor }
    nodes { title variants(first: 20) { nodes { id sku inventoryItem { id } } } }
  }
}"""
Q_ORDERS = """query($after: String) {
  orders(first: 250, after: $after, query: "tag:p8-synthetic") {
    pageInfo { hasNextPage endCursor } nodes { tags }
  }
}"""
M_PRODUCT = """mutation($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product { id title variants(first: 20) { nodes { id sku inventoryItem { id } } } }
    userErrors { field message code }
  }
}"""
M_ORDER = """mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order { id name }
    userErrors { field message code }
  }
}"""


def paged(api: Shopify, query: str, key: str):
    after = None
    while True:
        conn = api.gql(query, {"after": after})[key]
        yield from conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


def gid_num(gid: str) -> int:
    return int(gid.split("?")[0].rsplit("/", 1)[-1])


# --- main ---------------------------------------------------------------------------------------------------------
def write_inventory_ids(env: dict, items: dict[str, int]) -> int:
    import psycopg

    from p8.config import ensure_database, pg_dsn
    from p8.db import migrate, seed_products

    dbname = db_name(env, "shopify")
    ensure_database(env, dbname)
    with psycopg.connect(pg_dsn(env, dbname=dbname)) as conn:
        migrate(conn)
        seed_products(conn, seed=int(env.get("MOCK_SEED", "42")), inventory_ids=False)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("update products set inventory_item_id = %s where sku = %s",
                            [(iid, sku) for sku, iid in items.items()])
        return conn.execute("select count(*) from products where inventory_item_id is not null").fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="read the store and print the plan; write nothing")
    ap.add_argument("--orders", type=int, default=150)
    ap.add_argument("--days", type=int, default=30, help="spread orders over the last N days (max 55)")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= 55:
        ap.error("--days must be 1..55 (read_orders only returns the last 60 days)")

    env = load_env()
    missing = [k for k in ("SHOPIFY_SHOP", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET") if not env.get(k)]
    if missing:
        print("missing in .env:", ", ".join(missing))
        return 2
    api = Shopify(env["SHOPIFY_SHOP"], env["SHOPIFY_CLIENT_ID"], env["SHOPIFY_CLIENT_SECRET"],
                  env.get("SHOPIFY_API_VERSION") or "2026-10")
    catalog = build_catalog(int(env.get("MOCK_SEED", "42")))
    info = api.gql(Q_SHOP)
    shop, loc = info["shop"], info["location"]
    print(f"store: {env['SHOPIFY_SHOP']} | currency {shop['currencyCode']} | tz {shop['ianaTimezone']} | "
          f"dev store {shop['plan']['partnerDevelopment']} | location {loc['name']}")
    if shop["currencyCode"] != "EGP" or shop["ianaTimezone"] != "Africa/Cairo":
        print("note: the mock and the reports use EGP / Africa/Cairo; prices are created as plain numbers in the "
              "store's currency")

    # products
    existing = {p["title"]: p for p in paged(api, Q_PRODUCTS, "products")}
    variants = {v["sku"]: v for p in existing.values() for v in p["variants"]["nodes"]}
    todo = [p for p in product_inputs(catalog, loc["id"]) if p["title"] not in existing]
    print(f"products: {len(existing)} exist, {len(todo)} to create "
          f"({sum(len(p['variants']) for p in todo)} SKUs)")

    # orders
    done = {t for o in paged(api, Q_ORDERS, "orders") for t in o["tags"] if t.startswith("p8-seed-")}
    plan = order_plan(catalog, args.orders, args.days, datetime.now(CAIRO))
    pending = [o for o in plan if f"p8-seed-{o['index']:04d}" not in done]
    units = sum(li["quantity"] for o in pending for li in o["lines"])
    print(f"orders: {len(done)} exist, {len(pending)} to create ({units} units, "
          f"~{len(pending) * ORDER_PACE_S / 60:.0f} min at 5/min); "
          f"{plan[0]['processedAt'][:10]} .. {plan[-1]['processedAt'][:10]}")

    if args.dry_run:
        print("dry run: nothing written")
        if todo:
            print("first product:", json.dumps({k: todo[0][k] for k in ("title", "tags")}),
                  "variants:", [v["sku"] for v in todo[0]["variants"]])
        if pending:
            print("first order:", json.dumps({k: pending[0][k] for k in ("index", "processedAt", "email", "lines")}))
        return 0

    for p in todo:
        res = api.gql(M_PRODUCT, {"input": p})["productSet"]
        if res["userErrors"]:
            print(f"productSet {p['title']!r} failed: {res['userErrors']}")
            return 1
        for v in res["product"]["variants"]["nodes"]:
            variants[v["sku"]] = v
        print(f"  created product {p['title']!r} ({len(p['variants'])} SKUs)")
    absent = [p.sku for p in catalog if p.sku not in variants]
    if absent:
        print("SKUs missing in the store after product creation:", absent)
        return 1
    n = write_inventory_ids(env, {sku: gid_num(v["inventoryItem"]["id"]) for sku, v in variants.items()})
    print(f"postgres {db_name(env, 'shopify')}: {n} products linked to Shopify inventory items")

    options = {"inventoryBehaviour": "BYPASS", "sendReceipt": False, "sendFulfillmentReceipt": False}
    vid = {sku: v["id"] for sku, v in variants.items()}
    created = 0
    for o in pending:
        for attempt in range(1, 6):
            res = api.gql(M_ORDER, {"order": order_input(o, vid), "options": options})["orderCreate"]
            errs = res["userErrors"]
            if not errs:
                break
            text = "; ".join(e["message"] for e in errs)
            if "too many attempts" in text.lower() and attempt < 5:
                time.sleep(60)
                continue
            print(f"orderCreate p8-seed-{o['index']:04d} failed: {errs}")
            if "customer" in text.lower():
                print("stopping: the store wants customer access for orderCreate (no customer scopes by design)")
            return 1
        created += 1
        if created % 10 == 0 or created == len(pending):
            print(f"  orders created: {created}/{len(pending)}")
        time.sleep(ORDER_PACE_S)
    print(f"done: {len(todo)} products, {created} orders created (all tagged {TAG})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
