"""Supplier price list: header and row validation, name rules, AI review list, single-transaction apply."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb


def validate(db, rows, ai=False, file="list.csv"):
    return db.execute("select p8_validate_price_list(%s, %s, %s) r", (file, Jsonb(rows), ai)).fetchone()["r"]


def apply(db, import_id):
    return db.execute("select p8_apply_price_list(%s) r", (import_id,)).fetchone()["r"]


def price(db, sku):
    return float(db.execute("select price from products where sku = %s", (sku,)).fetchone()["price"])


def name(db, sku):
    return db.execute("select name from products where sku = %s", (sku,)).fetchone()["name"]


def test_parse_price(db):
    cases = {"12.5": 12.5, " 1,250.50 ": 1250.5, "EGP 99": 99, "-3": -3, "12,5": None, "abc": None, "": None,
             "1e3": None, "0": 0}
    for raw, want in cases.items():
        got = db.execute("select p8_parse_price(%s) v", (raw,)).fetchone()["v"]
        assert (float(got) if got is not None else None) == want, raw


def test_missing_required_columns(db):
    r = validate(db, [{"SKU": "SKU-1001", "Cost": "10"}])
    assert r["status"] == "rejected" and r["file_error"] == "missing required column: price"
    r = validate(db, [{"Price": "10", "Colour": "red"}])
    assert r["file_error"].startswith("missing required column: sku or name")
    r = validate(db, [])
    assert r["file_error"] == "the file has no data rows"
    assert db.execute("select count(*) n from import_rows").fetchone()["n"] == 0


def test_every_reject_reason_and_apply(db):
    old_1001, old_1002 = price(db, "SKU-1001"), price(db, "SKU-1002")
    rows = [
        {"Item Code": "SKU-1001", "Product Name": "x", "Unit Price": "150.00"},       # 2 valid, changed
        {"Item Code": "sku-1002", "Product Name": "", "Unit Price": str(old_1002)},  # 3 valid, unchanged
        {"Item Code": "SKU-1003", "Product Name": "", "Unit Price": "abc"},          # 4 not a number
        {"Item Code": "SKU-1004", "Product Name": "", "Unit Price": "0"},            # 5 not > 0
        {"Item Code": "SKU-1005", "Product Name": "", "Unit Price": "-5"},           # 6 not > 0
        {"Item Code": "SKU-1006", "Product Name": "", "Unit Price": ""},             # 7 missing
        {"Item Code": "SKU-9999", "Product Name": "", "Unit Price": "10"},           # 8 unknown SKU
        {"Item Code": "SKU-1007", "Product Name": "", "Unit Price": "10"},           # 9 duplicate
        {"Item Code": "SKU-1007", "Product Name": "", "Unit Price": "11"},           # 10 duplicate
        {"Item Code": "", "Product Name": "", "Unit Price": "10"},                   # 11 no sku/name
        {"Item Code": "", "Product Name": "Nonexistent Gadget", "Unit Price": "10"},  # 12 no name match
        {"Item Code": "", "Product Name": name(db, "SKU-1008").upper(), "Unit Price": "1,234.50"},  # 13 by name
        {"Item Code": "SKU-1009", "Product Name": "", "Unit Price": "12,5"},         # 14 comma decimal
    ]
    r = validate(db, rows)
    assert r["status"] == "validated"
    reasons = {x["row_no"]: x["reason"] for x in r["rejects"]}
    assert reasons == {
        4: "price is not a number (abc)",
        5: "price must be greater than 0 (0)",
        6: "price must be greater than 0 (-5)",
        7: "price is missing",
        8: "unknown SKU SKU-9999",
        9: "duplicate SKU SKU-1007 in the file (rows 9, 10)",
        10: "duplicate SKU SKU-1007 in the file (rows 9, 10)",
        11: "SKU and name are both empty",
        12: "no SKU and the name matches no product",
        14: "price is not a number (12,5)",
    }
    assert (r["rows_total"], r["rows_valid"], r["rows_rejected"], r["rows_changed"], r["rows_unchanged"]) == \
        (13, 3, 10, 2, 1)
    changes = {c["sku"]: c for c in r["changes"]}
    assert set(changes) == {"SKU-1001", "SKU-1008"}
    assert changes["SKU-1008"]["match_method"] == "name"
    assert float(changes["SKU-1001"]["old_price"]) == old_1001
    # nothing applied yet
    assert price(db, "SKU-1001") == old_1001

    out = apply(db, r["import_id"])
    assert out["status"] == "applied" and out["rows_changed"] == 2 and out["rows_unchanged"] == 1
    assert price(db, "SKU-1001") == 150.0 and price(db, "SKU-1008") == 1234.5 and price(db, "SKU-1002") == old_1002
    pc = db.execute("select sku, old_price, new_price from price_changes order by sku").fetchall()
    assert [(x["sku"], float(x["old_price"]), float(x["new_price"])) for x in pc] == [
        ("SKU-1001", old_1001, 150.0), ("SKU-1008", float(changes["SKU-1008"]["old_price"]), 1234.5)]
    # applying again is a no-op
    again = apply(db, r["import_id"])
    assert again["status"] == "applied"
    assert db.execute("select count(*) n from price_changes").fetchone()["n"] == 2


def test_multiple_reasons_are_joined(db):
    r = validate(db, [{"sku": "SKU-0000", "price": "x"}])
    assert r["rejects"][0]["reason"] == "price is not a number (x); unknown SKU SKU-0000"


def test_name_rule_any_word_order_and_ambiguity(db):
    n = name(db, "SKU-1001")  # "Ceramic Mug 350ml - Sand"
    shuffled = " ".join(reversed(n.replace("-", " ").split()))
    r = validate(db, [{"name": shuffled, "price": "100"}])
    assert r["changes"][0]["sku"] == "SKU-1001"
    # a name shared by two products cannot be matched by rules
    db.execute("update products set name = %s where sku = 'SKU-1002'", (n,))
    r = validate(db, [{"name": n, "price": "100"}])
    assert r["rows_rejected"] == 1


def test_apply_is_one_transaction(db):
    """If any write fails, no price changes and no price_changes rows survive."""
    r = validate(db, [{"sku": "SKU-1001", "price": "111"}, {"sku": "SKU-1002", "price": "222"}])
    before = (price(db, "SKU-1001"), price(db, "SKU-1002"))
    db.execute("alter table price_changes add constraint test_block check (new_price <> 222)")
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            apply(db, r["import_id"])
    finally:
        db.execute("alter table price_changes drop constraint test_block")
    assert (price(db, "SKU-1001"), price(db, "SKU-1002")) == before
    assert db.execute("select count(*) n from price_changes").fetchone()["n"] == 0
    assert db.execute("select status from import_log").fetchone()["status"] == "validated"
    assert apply(db, r["import_id"])["rows_changed"] == 2


def test_ai_rows_go_to_review_never_applied(db):
    target = name(db, "SKU-1010")
    rows = [
        {"sku": "", "name": "mystery thing one", "price": "100"},   # 2 -> AI high confidence -> review
        {"sku": "", "name": "mystery thing two", "price": "100"},   # 3 -> AI low confidence -> reject
        {"sku": "", "name": "mystery thing three", "price": "100"},  # 4 -> AI invented SKU -> reject
        {"sku": "", "name": "mystery thing four", "price": "100"},  # 5 -> AI no answer -> reject
        {"sku": "", "name": "mystery thing five", "price": "x"},    # 6 bad price: never sent to AI
        {"sku": "SKU-1011", "name": "", "price": "100"},             # 7 rules -> valid
    ]
    r = validate(db, rows, ai=True)
    assert [p["row_no"] for p in r["pending_ai"]] == [2, 3, 4, 5]
    with pytest.raises(psycopg.errors.RaiseException):
        apply(db, r["import_id"])
    matches = [{"row_no": 2, "sku": "SKU-1010", "confidence": 0.93},
               {"row_no": 3, "sku": "SKU-1012", "confidence": 0.41},
               {"row_no": 4, "sku": "SKU-7777", "confidence": 0.99}]
    r = db.execute("select p8_record_ai_matches(%s, %s, 0.85) r", (r["import_id"], Jsonb(matches))).fetchone()["r"]
    assert r["rows_review"] == 1 and r["rows_valid"] == 1 and r["rows_pending_ai"] == 0
    rev = r["review"][0]
    assert (rev["row_no"], rev["suggested_sku"], rev["suggested_name"], float(rev["confidence"])) == \
        (2, "SKU-1010", target, 0.93)
    reasons = {x["row_no"]: x["reason"] for x in r["rejects"]}
    assert reasons[3] == "no SKU and the name matches no product (AI best guess SKU-1012 at 0.41, below 0.85)"
    assert reasons[4] == "no SKU and the name matches no product (AI found no match)"
    assert reasons[5] == "no SKU and the name matches no product (AI found no match)"
    assert reasons[6].startswith("price is not a number")
    before = price(db, "SKU-1010")
    out = apply(db, r["import_id"])
    assert price(db, "SKU-1010") == before  # review rows are never applied
    assert [c["sku"] for c in out["changes"]] == ["SKU-1011"]


def test_ai_failure_rejects_pending_rows_with_reason(db):
    r = validate(db, [{"name": "mystery", "price": "10"}], ai=True)
    r = db.execute("select p8_record_ai_matches(%s, '[]', 0.85, 'HTTP 503') r", (r["import_id"],)).fetchone()["r"]
    assert r["rejects"][0]["reason"] == "no SKU and the name matches no product (AI matching failed: HTTP 503)"


def test_xlsx_numbers_are_accepted(db):
    r = validate(db, [{"SKU": "SKU-1001", "Price": 199.5}, {"SKU": "SKU-1002", "Price": 0}])
    assert r["rows_valid"] == 1 and r["rejects"][0]["reason"] == "price must be greater than 0 (0)"
