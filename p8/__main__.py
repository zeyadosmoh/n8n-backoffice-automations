"""python -m p8 {migrate,seed-products,sample-pricelists}"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .config import load_env, pg_dsn
from .db import migrate, seed_products


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p8")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply db/migrations")
    sub.add_parser("seed-products", help="insert the synthetic catalog into products")
    sp = sub.add_parser("sample-pricelists", help="write synthetic supplier price lists to samples/")
    sp.add_argument("--out", default="samples")
    args = ap.parse_args(argv)

    env = load_env()
    if args.cmd == "sample-pricelists":
        from .samples import write_samples

        for path in write_samples(args.out, seed=int(env.get("MOCK_SEED", "42"))):
            print(path)
        return 0
    with psycopg.connect(pg_dsn(env)) as conn:
        if args.cmd == "migrate":
            applied = migrate(conn)
            print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
        elif args.cmd == "seed-products":
            print("products:", seed_products(conn, seed=int(env.get("MOCK_SEED", "42"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
