"""Synthetic product catalog shared by the mock store and the Postgres seed.

Deterministic for a given seed. Every name is invented; no real brand or supplier.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

PRODUCT_TYPES = [
    # (type, base price EGP, variants)
    ("Ceramic Mug 350ml", 180, ["Sand", "Olive", "Charcoal", "Terracotta"]),
    ("Linen Tea Towel", 140, ["Natural", "Indigo", "Rust"]),
    ("Olive Wood Cutting Board", 520, ["Small", "Medium", "Large"]),
    ("Glass Storage Jar 1L", 210, ["Bamboo Lid", "Cork Lid"]),
    ("Cotton Throw Blanket", 950, ["Cream", "Grey", "Mustard"]),
    ("Scented Soy Candle", 260, ["Jasmine", "Cedar", "Orange Blossom", "Mint"]),
    ("Stoneware Dinner Plate", 230, ["White", "Speckled", "Blue Rim"]),
    ("Woven Palm Basket", 380, ["Small", "Large"]),
    ("Brass Table Lamp", 2400, ["Matte", "Polished"]),
    ("Handmade Soap Bar", 75, ["Olive Oil", "Rose", "Charcoal", "Oat"]),
    ("Copper Coffee Pot", 640, ["Small", "Large"]),
    ("Embroidered Cushion Cover", 330, ["Teal", "Ivory", "Coral"]),
    ("Bamboo Serving Tray", 410, ["Round", "Rectangle"]),
    ("Cotton Bath Towel", 290, ["White", "Sage", "Sand"]),
    ("Clay Plant Pot", 160, ["12cm", "18cm", "24cm"]),
    ("Glass Water Carafe", 270, ["Clear", "Smoke"]),
    ("Kilim Floor Rug", 1850, ["Small", "Medium"]),
    ("Wooden Spoon Set", 150, ["3 pcs", "5 pcs"]),
    ("Enamel Pasta Bowl", 200, ["Red", "Black", "Cream"]),
]


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Decimal
    initial_stock: int
    reorder_point: int
    popularity: float  # relative weight in generated orders
    inventory_item_id: int


def build_catalog(seed: int = 42) -> list[Product]:
    rng = random.Random(seed)
    products: list[Product] = []
    n = 0
    for type_name, base, variants in PRODUCT_TYPES:
        for variant in variants:
            n += 1
            price = Decimal(base) * Decimal(str(round(rng.uniform(0.9, 1.15), 2)))
            price = price.quantize(Decimal("1")) - Decimal("0.01")  # e.g. 179.99
            reorder_point = rng.choice([10, 12, 15, 20, 25, 30])
            popularity = round(rng.uniform(0.3, 2.5), 2)
            # ~1 in 9 SKUs starts below its reorder point; the rest have healthy stock.
            if rng.random() < 0.11:
                stock = rng.randint(0, reorder_point - 1)
            else:
                stock = rng.randint(reorder_point + 15, reorder_point + 140)
            products.append(
                Product(
                    sku=f"SKU-{1000 + n}",
                    name=f"{type_name} - {variant}",
                    price=price,
                    initial_stock=stock,
                    reorder_point=reorder_point,
                    popularity=popularity,
                    inventory_item_id=43_000_000_000 + n,
                )
            )
    return products
