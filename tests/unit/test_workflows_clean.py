"""The exported workflows are clean, up to date, and the leak check really catches leaks."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys

import pytest

from p8.config import ROOT

spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check_workflows_clean.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

TOKEN = "tok_" + "Zx9" * 10
FILES = sorted((ROOT / "workflows").glob("*.json"))


def problems(tmp_path, wf, secrets=None):
    f = tmp_path / "wf.json"
    f.write_text(json.dumps(wf))
    return check.check_file(f, secrets or {"N8N_WEBHOOK_TOKEN": TOKEN})


def load(name):
    return json.loads((ROOT / "workflows" / f"{name}.json").read_text())


def test_all_workflows_exported():
    assert {f.stem for f in FILES} == {"orders_sync", "low_stock_alert", "supplier_price_list", "error_handler",
                                    "shopify_token"}


def test_exports_are_up_to_date_with_generator():
    out = subprocess.run([sys.executable, "scripts/build_workflows.py", "--check"], cwd=ROOT, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_committed_workflows_are_clean():
    out = subprocess.run([sys.executable, "scripts/check_workflows_clean.py"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


@pytest.mark.parametrize("f", FILES, ids=lambda f: f.stem)
def test_every_workflow_uses_the_error_workflow_and_credentials_by_reference(f):
    wf = json.loads(f.read_text())
    # the error handler itself, and the token sub-workflow (its failures surface in the calling workflow)
    if wf["id"] not in {"p8ErrorHandler01", "p8ShopifyToken01"}:
        assert wf["settings"]["errorWorkflow"] == "p8ErrorHandler01"
    for n in wf["nodes"]:
        for ref in (n.get("credentials") or {}).values():
            assert set(ref) == {"id", "name"}


def test_detects_webhook_token_value(tmp_path):
    wf = load("orders_sync")
    wf["nodes"][0]["notes"] = f"token is {TOKEN}"
    assert any("N8N_WEBHOOK_TOKEN" in p for p in problems(tmp_path, wf))


def test_detects_credential_data(tmp_path):
    wf = load("low_stock_alert")
    node = next(n for n in wf["nodes"] if n.get("credentials"))
    node["credentials"] = {k: {**v, "data": {"password": "x"}} for k, v in node["credentials"].items()}
    assert any("more than id/name" in p for p in problems(tmp_path, wf))


def test_detects_webhook_without_header_auth(tmp_path):
    wf = load("orders_sync")
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    hook["parameters"]["authentication"] = "none"
    assert any("without Header Auth" in p for p in problems(tmp_path, wf))


def test_detects_hardcoded_headers_and_tokens(tmp_path):
    wf = load("orders_sync")
    http = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    http["parameters"]["sendHeaders"] = True
    http["parameters"]["headerParameters"] = {"parameters": [{"name": "X-Shopify-Access-Token",
                                                              "value": "shpat_" + "a1" * 16}]}
    found = problems(tmp_path, wf)
    assert any("custom headers" in p for p in found) and any("Shopify-style token" in p for p in found)


def test_detects_hardcoded_editor_link(tmp_path):
    wf = load("error_handler")
    code = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.code")
    code["parameters"]["jsCode"] = "const link = 'http://localhost:5680/workflow/' + id;"
    found = problems(tmp_path, wf)
    assert any("N8N_EDITOR_BASE_URL" in p for p in found) and any("hard-codes" in p for p in found)


def test_detects_real_email(tmp_path):
    wf = copy.deepcopy(load("error_handler"))
    wf["nodes"][-1]["parameters"]["toEmail"] = "someone@" + "gmail.com"
    assert any("email" in p for p in problems(tmp_path, wf))


def test_detects_shopify_client_secret_value(tmp_path):
    fake = "shpss_" + "0f" * 16
    wf = load("orders_sync")
    wf["nodes"][0]["notes"] = f"client secret {fake}"
    found = problems(tmp_path, wf, {"SHOPIFY_CLIENT_SECRET": fake})
    assert any("SHOPIFY_CLIENT_SECRET" in p for p in found)


@pytest.mark.parametrize("prefix", ["shpat_", "shpca_", "shpss_", "shppa_"])
def test_detects_every_shopify_token_shape(tmp_path, prefix):
    wf = load("orders_sync")
    wf["nodes"][0]["notes"] = prefix + "9c" * 16
    assert any("Shopify-style token" in p for p in problems(tmp_path, wf))


def test_client_secret_is_a_checked_secret():
    assert "SHOPIFY_CLIENT_SECRET" in check.SECRET_VARS
