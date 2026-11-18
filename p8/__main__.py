"""python -m p8 {migrate,seed-products,sample-pricelists} [--source mock|shopify]"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .config import db_name, ensure_database, load_env, pg_dsn
from .db import migrate, seed_products


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p8")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in [("migrate", "apply db/migrations"), ("seed-products", "insert the synthetic catalog")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--source", choices=["mock", "shopify"], default=None,
                        help="which store's database (default: STORE_SOURCE, else mock). "
                             "shopify = SHOPIFY_POSTGRES_DB, created if missing")
    sp = sub.add_parser("sample-pricelists", help="write synthetic supplier price lists to samples/")
    sp.add_argument("--out", default="samples")
    args = ap.parse_args(argv)

    env = load_env()
    if args.cmd == "sample-pricelists":
        from .samples import write_samples

        for path in write_samples(args.out, seed=int(env.get("MOCK_SEED", "42"))):
            print(path)
        return 0
    source = args.source or (env.get("STORE_SOURCE") or "mock")
    dbname = db_name(env, source)
    if source == "shopify" and ensure_database(env, dbname):
        print("created database", dbname)
    with psycopg.connect(pg_dsn(env, dbname=dbname)) as conn:
        if args.cmd == "migrate":
            applied = migrate(conn)
            print(f"[{dbname}] applied:", ", ".join(applied) if applied else "nothing (up to date)")
        elif args.cmd == "seed-products":
            # Shopify mode: the store assigns inventory item ids; scripts/seed_shopify.py writes them in.
            n = seed_products(conn, seed=int(env.get("MOCK_SEED", "42")), inventory_ids=source == "mock")
            print(f"[{dbname}] products:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
