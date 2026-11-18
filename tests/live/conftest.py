"""Opt-in live test against the real Shopify dev store (STORE_SOURCE=shopify on the throwaway p8-e2e stack).

    python -m pytest -m shopify_live tests/live -v

Runs only when selected with `-m shopify_live` AND SHOPIFY_SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET are set
(.env or environment). Never in CI. Creates one synthetic order on the store and cancels it (with restock)
afterwards. Prints counts only, never the token or the client secret.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

import pytest

from p8.config import ROOT, load_env

LIVE_VARS = ("SHOPIFY_SHOP", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")


def pytest_collection_modifyitems(config, items):
    selected = "shopify_live" in (config.getoption("markexpr") or "")
    for item in items:
        if "tests/live/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.shopify_live)
            if not selected:
                item.add_marker(pytest.mark.skip(reason="opt-in: python -m pytest -m shopify_live tests/live"))


def seed_module():
    spec = importlib.util.spec_from_file_location("seed_shopify", ROOT / "scripts" / "seed_shopify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def real_env():
    env = load_env()
    missing = [k for k in LIVE_VARS if not env.get(k)]
    if missing:
        pytest.skip(f"Shopify live test needs {', '.join(missing)}")
    if not shutil.which("docker"):
        pytest.skip("docker is not available")
    return env


@pytest.fixture(scope="module")
def api(real_env):
    seed = seed_module()
    return seed.Shopify(real_env["SHOPIFY_SHOP"], real_env["SHOPIFY_CLIENT_ID"], real_env["SHOPIFY_CLIENT_SECRET"],
                        real_env.get("SHOPIFY_API_VERSION") or "2026-10")


@pytest.fixture(scope="module")
def shop_stack(real_env, api):
    from tests.e2e.stack import local_stack, teardown

    seed = seed_module()
    variants = {v["sku"]: v for p in seed.paged(api, seed.Q_PRODUCTS, "products") for v in p["variants"]["nodes"]}
    if len(variants) < 52:
        pytest.skip(f"seed the store first (python3 scripts/seed_shopify.py): {len(variants)}/52 SKUs found")
    extra = {k: real_env[k] for k in LIVE_VARS}
    extra.update(STORE_SOURCE="shopify", SHOPIFY_API_VERSION=real_env.get("SHOPIFY_API_VERSION") or "2026-10",
                 SHOPIFY_POSTGRES_DB="p8_shopify")

    def prepare(stack):
        stack.dbname = "p8_shopify"
        env = {**stack.env, "POSTGRES_HOST": "127.0.0.1"}
        for _ in range(60):
            try:
                seed.write_inventory_ids(env, {sku: seed.gid_num(v["inventoryItem"]["id"])
                                               for sku, v in variants.items()})
                return
            except Exception:  # noqa: BLE001 - Postgres still starting
                import time

                time.sleep(1)
        raise RuntimeError("Postgres for the live stack is not reachable")

    try:
        s = local_stack(extra, prepare=prepare)
    except Exception:
        from tests.e2e.stack import E2E_DIR

        (E2E_DIR / ".env").unlink(missing_ok=True)       # holds the client secret
        raise
    s.variants = variants
    yield s
    teardown(s, forget_env=True)
    if os.environ.get("E2E_KEEP") == "1":
        print("E2E_KEEP=1: .e2e/.env (with the client secret) was kept; delete it when done")
