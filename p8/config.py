"""Settings from the environment, with .env as a fallback (no extra dependency)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None, use_environ: bool = True) -> dict[str, str]:
    """Values from .env (if present), overridden by the real environment unless use_environ=False."""
    values: dict[str, str] = {}
    path = path or ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    if use_environ:
        values.update(os.environ)
    return values


def pg_dsn(env: dict[str, str] | None = None, dbname: str | None = None) -> str:
    env = env or load_env()
    return (
        f"host={env.get('POSTGRES_HOST', '127.0.0.1')} port={env.get('POSTGRES_PORT', '5438')} "
        f"user={env['POSTGRES_USER']} password={env['POSTGRES_PASSWORD']} "
        f"dbname={dbname or env.get('POSTGRES_DB', 'p8')}"
    )


def db_name(env: dict[str, str], source: str = "mock") -> str:
    """The database a store source uses: POSTGRES_DB for the mock, SHOPIFY_POSTGRES_DB for the real store."""
    if source == "shopify":
        return env.get("SHOPIFY_POSTGRES_DB") or "p8_shopify"
    return env.get("POSTGRES_DB", "p8")


def ensure_database(env: dict[str, str], dbname: str) -> bool:
    """Create dbname on the same server if it does not exist yet. True if it was created."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(pg_dsn(env), autocommit=True) as conn:
        if conn.execute("select 1 from pg_database where datname = %s", (dbname,)).fetchone():
            return False
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(dbname)))
        return True
