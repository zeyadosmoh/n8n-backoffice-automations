"""Sample price lists are deterministic and cover the cases the README promises; the fake Gemini behaves."""

from __future__ import annotations

import csv
import json

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from mock_store.app import create_app
from mock_store.catalog import build_catalog
from mock_store.fake_gemini import match
from p8.config import ROOT
from p8.samples import bad_rows, good_rows, write_samples


def test_samples_are_deterministic_and_written(tmp_path):
    assert good_rows() == good_rows()
    paths = write_samples(tmp_path)
    assert [p.name for p in paths] == ["pricelist_good.csv", "pricelist_bad.csv", "pricelist_names_only.xlsx"]
    with (tmp_path / "pricelist_good.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 24 and len({r["Item Code"] for r in rows}) == 24
    ws = load_workbook(tmp_path / "pricelist_names_only.xlsx").active
    assert [c.value for c in ws[1]] == ["Product", "Price"]


def test_committed_samples_match_generator(tmp_path):
    write_samples(tmp_path)
    for name in ("pricelist_good.csv", "pricelist_bad.csv"):
        committed = (ROOT / "samples" / name).read_text()
        assert committed == (tmp_path / name).read_text(), f"samples/{name} is stale: run python -m p8 sample-pricelists"


def test_bad_sample_has_every_reason():
    rows = bad_rows()
    skus = [r["Item Code"] for r in rows]
    assert "SKU-2999" in skus and skus.count("SKU-1007") == 2
    prices = [r["Unit Price (EGP)"] for r in rows]
    assert {"call us", "0", "-12.50", "", "12,5"} <= set(prices)


def test_fake_gemini_match_rules():
    cat = [{"sku": p.sku, "name": p.name} for p in build_catalog()]
    out = match([{"row_no": 2, "name": "Ceramic Mugs 350ml Olive"}, {"row_no": 3, "name": "Marble Coaster Set"},
                 {"row_no": 4, "name": "Linen Towel Natural"}], cat)
    assert out[0] == {"row_no": 2, "sku": "SKU-1002", "confidence": 1.0}
    assert out[1]["sku"] is None
    assert out[2]["sku"] == "SKU-1005" and out[2]["confidence"] == 0.75


def test_fake_gemini_endpoint_speaks_generate_content():
    c = TestClient(create_app(api_token="t", admin_token="a"))
    prompt = 'x\nROWS_JSON:\n[{"row_no": 5, "name": "Scented Soy Candles - Jasmine"}]\nCATALOG_JSON:\n' + json.dumps(
        [{"sku": p.sku, "name": p.name} for p in build_catalog()])
    r = c.post("/_fake_gemini/v1beta/models/m:generateContent", json={"contents": [{"parts": [{"text": prompt}]}]})
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    assert json.loads(text) == [{"row_no": 5, "sku": "SKU-1016", "confidence": 1.0}]
