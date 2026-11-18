"""Synthetic supplier price lists for demos and tests (deterministic). Every supplier and row is invented.

- pricelist_good.csv           24 SKUs, supplier headers ("Item Code", "Product Name", "Unit Price (EGP)"), all valid
- pricelist_bad.csv            every reject reason once, plus valid rows matched by SKU and by name
- pricelist_names_only.xlsx    no SKU column: names exactly as in the catalog, words reordered, plural/typo variants
                               (only AI can suggest those), and names nobody can match
"""

from __future__ import annotations

import csv
import random
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from mock_store.catalog import build_catalog

SUPPLIER = "Nile Home Supply (synthetic)"


def _new_price(rng: random.Random, old: Decimal) -> Decimal:
    if rng.random() < 0.2:
        return old  # unchanged
    pct = Decimal(str(round(rng.uniform(-0.08, 0.14), 3)))
    return (old * (1 + pct)).quantize(Decimal("1")) - Decimal("0.01")


def good_rows(seed: int = 42) -> list[dict]:
    rng = random.Random(seed + 1)
    cat = build_catalog(seed)
    rows = []
    for p in rng.sample(cat, 24):
        rows.append({"Item Code": p.sku, "Product Name": p.name, "Unit Price (EGP)": f"{_new_price(rng, p.price):.2f}"})
    return sorted(rows, key=lambda r: r["Item Code"])


def bad_rows(seed: int = 42) -> list[dict]:
    cat = build_catalog(seed)
    by = {p.sku: p for p in cat}
    p = lambda sku: by[sku]  # noqa: E731
    return [
        {"Item Code": "SKU-1001", "Product Name": p("SKU-1001").name, "Unit Price (EGP)": "189.99"},   # valid
        {"Item Code": "SKU-1002", "Product Name": p("SKU-1002").name, "Unit Price (EGP)": "1,250.00"},  # valid
        {"Item Code": "SKU-1003", "Product Name": p("SKU-1003").name, "Unit Price (EGP)": "call us"},   # not a number
        {"Item Code": "SKU-1004", "Product Name": p("SKU-1004").name, "Unit Price (EGP)": "0"},         # not > 0
        {"Item Code": "SKU-1005", "Product Name": p("SKU-1005").name, "Unit Price (EGP)": "-12.50"},    # not > 0
        {"Item Code": "SKU-1006", "Product Name": p("SKU-1006").name, "Unit Price (EGP)": ""},          # missing
        {"Item Code": "SKU-2999", "Product Name": "Velvet Footstool - Rose", "Unit Price (EGP)": "799.00"},  # unknown
        {"Item Code": "SKU-1007", "Product Name": p("SKU-1007").name, "Unit Price (EGP)": "150.00"},    # duplicate
        {"Item Code": "SKU-1007", "Product Name": p("SKU-1007").name, "Unit Price (EGP)": "155.00"},    # duplicate
        {"Item Code": "", "Product Name": "", "Unit Price (EGP)": "99.00"},                              # no sku/name
        {"Item Code": "", "Product Name": p("SKU-1012").name.upper(), "Unit Price (EGP)": "560.00"},    # by name
        {"Item Code": "SKU-1013", "Product Name": p("SKU-1013").name, "Unit Price (EGP)": "12,5"},      # comma decimal
    ]


def names_only_rows(seed: int = 42) -> list[dict]:
    by = {p.sku: p for p in build_catalog(seed)}
    n = lambda sku: by[sku].name  # noqa: E731
    return [
        {"Product": n("SKU-1020"), "Price": 241.0},                               # exact name -> rules
        {"Product": "Sand - 350ml Mug Ceramic", "Price": 185.0},                  # same words reordered -> rules
        {"Product": "Ceramic Mugs 350ml Olive", "Price": 187.5},                  # plural -> AI (review)
        {"Product": "Scented Soy Candles - Jasmine", "Price": 279.0},             # plural -> AI (review)
        {"Product": "Linen Towel Natural", "Price": 150.0},                       # partial -> AI low confidence
        {"Product": "Marble Coaster Set", "Price": 210.0},                        # not in catalog
        {"Product": n("SKU-1031"), "Price": 0},                                   # exact name, bad price
    ]


def write_samples(out: str | Path = "samples", seed: int = 42) -> list[Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in (("pricelist_good.csv", good_rows(seed)), ("pricelist_bad.csv", bad_rows(seed))):
        path = out / name
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        paths.append(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Price list"
    rows = names_only_rows(seed)
    ws.append(list(rows[0]))
    for r in rows:
        ws.append(list(r.values()))
    wb.properties.creator = SUPPLIER
    wb.properties.title = "Supplier price list (synthetic data)"
    path = out / "pricelist_names_only.xlsx"
    wb.save(path)
    paths.append(path)
    return paths
