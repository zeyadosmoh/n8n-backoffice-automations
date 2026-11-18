"""Shared fixtures. DB tests use a separate database (p8_test) on the dev Postgres (or the CI service)."""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from p8.config import load_env, pg_dsn
from p8.db import migrate, seed_products

TEST_DB = "p8_test"
TABLES = "price_changes, import_rows, import_log, alert_log, order_items, orders, sync_state, products"


def _require(var: str) -> bool:
    return os.environ.get(var, "") == "1"


def skip_or_fail(var: str, reason: str):
    if _require(var):
        pytest.fail(f"{reason} ({var}=1 turns this skip into a failure)")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def test_db_dsn():
    env = load_env()
    try:
        with psycopg.connect(pg_dsn(env, dbname=env.get("POSTGRES_DB", "p8")), autocommit=True,
                             connect_timeout=3) as admin:
            admin.execute(f"drop database if exists {TEST_DB} with (force)")
            admin.execute(f"create database {TEST_DB}")
    except psycopg.OperationalError as exc:
        skip_or_fail("REQUIRE_DB", f"Postgres not reachable: {exc}")
    dsn = pg_dsn(env, dbname=TEST_DB)
    with psycopg.connect(dsn) as conn:
        migrate(conn)
    return dsn


@pytest.fixture
def db(test_db_dsn):
    """A connection to a freshly reset p8_test (catalog seeded, everything else empty). Autocommit, like n8n."""
    with psycopg.connect(test_db_dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"truncate {TABLES} restart identity cascade")
        seed_products(conn)
        yield conn
