"""Plain-SQL migrations (db/migrations/NNN_*.sql, applied in order, each in its own transaction)."""

from __future__ import annotations

from pathlib import Path

import psycopg

from mock_store.catalog import build_catalog

from .config import ROOT

MIGRATIONS = ROOT / "db" / "migrations"


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        "create table if not exists schema_migrations (version text primary key, applied_at timestamptz default now())"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("select version from schema_migrations")}
    applied = []
    for path in sorted(Path(MIGRATIONS).glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("insert into schema_migrations (version) values (%s)", (path.name,))
        applied.append(path.name)
    return applied


def seed_products(conn: psycopg.Connection, seed: int = 42) -> int:
    """Insert the synthetic catalog. Existing SKUs are left alone (prices/stock may have changed since)."""
    rows = [
        (p.sku, p.name, p.price, p.initial_stock, p.reorder_point, p.inventory_item_id)
        for p in build_catalog(seed)
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "insert into products (sku, name, price, stock, reorder_point, inventory_item_id) "
            "values (%s, %s, %s, %s, %s, %s) on conflict (sku) do nothing",
            rows,
        )
        row = conn.execute("select count(*) as n from products").fetchone()
        return row["n"] if isinstance(row, dict) else row[0]
