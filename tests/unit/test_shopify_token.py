"""Shopify token cache: SQL functions, the token-response check, and the sub-workflow's shape.

Fixtures are recorded shapes with synthetic values; nothing here talks to Shopify.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from p8.config import ROOT

from ..jsnode import raises, run_code

SHOP = "p8-test-store.myshopify.com"
FAKE_TOKEN = "shpca_" + "0" * 32            # synthetic, never valid anywhere
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


# --- SQL cache ---------------------------------------------------------------------------------------------------
def get(db, now=NOW, min_valid="1 hour"):
    return db.execute("select p8_shopify_token_get(%s, %s::interval, %s) as t", (SHOP, min_valid, now)).fetchone()["t"]


def put(db, token=FAKE_TOKEN, expires_in=86399, now=NOW):
    return db.execute("select p8_shopify_token_put(%s, %s, %s, %s) as e", (SHOP, token, expires_in, now)).fetchone()["e"]


def test_missing_token_is_a_miss(db):
    assert get(db) is None


def test_fresh_token_is_a_hit_and_put_returns_only_the_expiry(db):
    expires = put(db)
    assert expires == NOW + timedelta(seconds=86399)
    assert get(db) == FAKE_TOKEN
    assert get(db, now=NOW + timedelta(hours=22)) == FAKE_TOKEN


def test_token_within_one_hour_of_expiry_is_a_miss(db):
    put(db)
    assert get(db, now=NOW + timedelta(seconds=86399) - timedelta(minutes=59)) is None
    assert get(db, now=NOW + timedelta(seconds=86399) - timedelta(minutes=61)) == FAKE_TOKEN


def test_expired_token_is_a_miss(db):
    put(db)
    assert get(db, now=NOW + timedelta(days=2)) is None


def test_put_replaces_the_shops_single_row(db):
    put(db)
    put(db, token=FAKE_TOKEN.replace("0", "1"), now=NOW + timedelta(hours=23))
    rows = db.execute("select token, expires_at from shopify_token").fetchall()
    assert len(rows) == 1 and rows[0]["token"] == FAKE_TOKEN.replace("0", "1")
    assert rows[0]["expires_at"] == NOW + timedelta(hours=23, seconds=86399)


@pytest.mark.parametrize("token,expires_in", [("", 86399), (None, 86399), (FAKE_TOKEN, 0), (FAKE_TOKEN, None)])
def test_put_rejects_unusable_answers(db, token, expires_in):
    with pytest.raises(psycopg.errors.RaiseException):
        put(db, token=token, expires_in=expires_in)


# --- token endpoint response check (workflows/src/shopify_token_check.js) ------------------------------------
OK = {"statusCode": 200, "body": {"access_token": FAKE_TOKEN, "scope": "read_orders,write_products",
                                  "expires_in": 86399}}


def test_check_returns_token_and_expiry():
    assert run_code("shopify_token_check", OK) == [{"token": FAKE_TOKEN, "expiresIn": 86399}]


@pytest.mark.parametrize("resp,expect", [
    ({"statusCode": 400, "body": {"error": "invalid_client", "error_description": "Client authentication failed"}},
     "HTTP 400 (invalid_client)"),
    ({"statusCode": 401, "body": {"errors": "[API] Invalid API key or access token"}}, "HTTP 401"),
    ({"statusCode": 400, "body": {"error": "shop_not_permitted"}}, "HTTP 400 (shop_not_permitted)"),
    ({"statusCode": 200, "body": {"scope": "read_orders"}}, "HTTP 200"),
    ({"statusCode": 0, "error": {"code": "ECONNRESET"}}, "network error: ECONNRESET"),
])
def test_check_fails_with_status_only(resp, expect):
    msg = raises("shopify_token_check", json_in=resp)
    assert expect in msg and "README" in msg
    assert "Client authentication failed" not in msg      # descriptions/bodies are not echoed


def test_check_never_echoes_token_or_secret_on_odd_answers():
    secretish = "shpss_" + "f" * 32
    resp = {"statusCode": 200, "body": {"access_token": FAKE_TOKEN, "expires_in": "soon"}}
    msg = raises("shopify_token_check", json_in=resp)
    assert FAKE_TOKEN not in msg
    resp = {"statusCode": 400, "body": {"error": secretish + " <script>"}}
    msg = raises("shopify_token_check", json_in=resp)
    assert "<script>" not in msg and len(msg) < 400


def test_new_token_return_passes_token_and_expiry():
    out = run_code("shopify_token_new", {"expires_at": "2026-11-20T12:00:00+00:00"},
                   nodes={"Check token response": {"token": FAKE_TOKEN, "expiresIn": 86399}})
    assert out == [{"token": FAKE_TOKEN, "cached": False, "expiresAt": "2026-11-20T12:00:00+00:00"}]


# --- sub-workflow shape --------------------------------------------------------------------------------------
WF = json.loads((ROOT / "workflows" / "shopify_token.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}


def test_sub_workflow_saves_no_execution_data():
    s = WF["settings"]
    assert s["saveDataSuccessExecution"] == "none" and s["saveDataErrorExecution"] == "none"
    assert s["saveManualExecutions"] is False and s["saveExecutionProgress"] is False


def test_token_request_uses_the_custom_auth_credential_only():
    http = NODES["Fetch new token"]
    p = http["parameters"]
    assert http["credentials"] == {"httpCustomAuth": {"id": "p8ShopifyApp0001", "name": "P8 Shopify app"}}
    assert p["genericAuthType"] == "httpCustomAuth" and not p.get("sendHeaders") and not p.get("sendQuery")
    assert json.loads(p["jsonBody"]) == {"grant_type": "client_credentials"}
    assert p["url"] == "=https://{{ $env.SHOPIFY_SHOP }}/admin/oauth/access_token"


def test_cache_paths_are_wired():
    c = WF["connections"]
    outs = c["Still valid for 1 h?"]["main"]
    assert [x["node"] for x in outs[0]] == ["Return cached token"]
    assert [x["node"] for x in outs[1]] == ["Fetch new token"]
    assert c["Store token"]["main"][0][0]["node"] == "Return new token"
