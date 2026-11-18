"""In-memory synthetic store: orders with line items and inventory levels.

Seeded deterministically from (seed, anchor). The anchor is the moment the seeded history ends
(default: today 00:00 in the shop's time zone), so the last 14 days always contain sales.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .catalog import Product, build_catalog

SHOP_TZ = ZoneInfo("Africa/Cairo")
HISTORY_DAYS = 90
FIRST_ORDER_ID = 5_100_000_001
FIRST_LINE_ID = 13_200_000_001
LOCATION_ID = 71_000_000_001


def default_anchor() -> datetime:
    now = datetime.now(SHOP_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def iso(ts: datetime) -> str:
    return ts.astimezone(SHOP_TZ).isoformat(timespec="seconds")


@dataclass
class LineItem:
    id: int
    sku: str
    title: str
    quantity: int
    price: Decimal


@dataclass
class Order:
    id: int
    number: int
    created_at: datetime
    updated_at: datetime
    financial_status: str
    fulfillment_status: str | None
    cancelled_at: datetime | None
    line_items: list[LineItem]

    def to_json(self) -> dict:
        subtotal = sum((li.price * li.quantity for li in self.line_items), Decimal("0"))
        return {
            "id": self.id,
            "name": f"#{self.number}",
            "order_number": self.number,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "cancelled_at": iso(self.cancelled_at) if self.cancelled_at else None,
            "financial_status": self.financial_status,
            "fulfillment_status": self.fulfillment_status,
            "currency": "EGP",
            "subtotal_price": f"{subtotal:.2f}",
            "total_price": f"{subtotal:.2f}",
            "line_items": [
                {
                    "id": li.id,
                    "sku": li.sku,
                    "title": li.title,
                    "quantity": li.quantity,
                    "price": f"{li.price:.2f}",
                }
                for li in self.line_items
            ],
        }


@dataclass
class InventoryLevel:
    product: Product
    available: int
    updated_at: datetime

    def to_json(self) -> dict:
        return {
            "inventory_item_id": self.product.inventory_item_id,
            "location_id": LOCATION_ID,
            "available": self.available,
            "updated_at": iso(self.updated_at),
            # Mock convenience: real Shopify needs a variant lookup to map an inventory item to its SKU.
            "sku": self.product.sku,
        }


@dataclass
class Store:
    seed: int = 42
    anchor: datetime = field(default_factory=default_anchor)
    products: list[Product] = field(init=False)
    orders: dict[int, Order] = field(init=False)
    inventory: dict[str, InventoryLevel] = field(init=False)
    lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.products = build_catalog(self.seed)
        self.by_sku = {p.sku: p for p in self.products}
        self.rng = random.Random(self.seed * 7919)
        self.orders = {}
        self._next_order_id = FIRST_ORDER_ID
        self._next_line_id = FIRST_LINE_ID
        self._generate_history()
        start = self.anchor - timedelta(days=HISTORY_DAYS)
        self.inventory = {
            p.sku: InventoryLevel(p, p.initial_stock, start + timedelta(hours=1)) for p in self.products
        }

    # -- generation ---------------------------------------------------------------------------

    def _new_order(self, created_at: datetime, items: list[tuple[Product, int]]) -> Order:
        lines = []
        for product, qty in items:
            lines.append(LineItem(self._next_line_id, product.sku, product.name, qty, product.price))
            self._next_line_id += 1
        order = Order(
            id=self._next_order_id,
            number=1000 + (self._next_order_id - FIRST_ORDER_ID) + 1,
            created_at=created_at,
            updated_at=created_at,
            financial_status="paid",
            fulfillment_status=None,
            cancelled_at=None,
            line_items=lines,
        )
        self._next_order_id += 1
        self.orders[order.id] = order
        return order

    def _random_items(self) -> list[tuple[Product, int]]:
        rng = self.rng
        weights = [p.popularity for p in self.products]
        n_lines = rng.choices([1, 2, 3, 4], weights=[55, 28, 12, 5])[0]
        chosen: dict[str, Product] = {}
        while len(chosen) < n_lines:
            p = rng.choices(self.products, weights=weights)[0]
            chosen[p.sku] = p
        return [(p, rng.choices([1, 2, 3], weights=[80, 15, 5])[0]) for p in chosen.values()]

    def _generate_history(self) -> None:
        rng = self.rng
        start = self.anchor - timedelta(days=HISTORY_DAYS)
        for day in range(HISTORY_DAYS):
            day_start = start + timedelta(days=day)
            weekend = day_start.weekday() in (4, 5)  # Fri/Sat busier in Egypt
            for _ in range(max(0, int(rng.gauss(19 if weekend else 15, 4)))):
                created = day_start + timedelta(seconds=rng.randint(8 * 3600, 23 * 3600 + 3599))
                order = self._new_order(created, self._random_items())
                roll = rng.random()
                if roll < 0.04:
                    order.cancelled_at = created + timedelta(hours=rng.randint(1, 20))
                    order.financial_status = "refunded"
                    order.updated_at = order.cancelled_at
                elif roll < 0.85:
                    order.fulfillment_status = "fulfilled"
                    order.updated_at = created + timedelta(hours=rng.randint(4, 60))
                order.updated_at = min(order.updated_at, self.anchor - timedelta(seconds=1))
                if order.cancelled_at:
                    order.cancelled_at = min(order.cancelled_at, order.updated_at)

    # -- mutations (admin) --------------------------------------------------------------------

    def simulate(
        self,
        now: datetime,
        orders: list[list[dict]] | None = None,
        random_orders: int = 0,
        updated_orders: int = 0,
        restock: dict[str, int] | None = None,
    ) -> dict:
        """Create orders (decrementing inventory), touch existing orders, or restock SKUs."""
        created, updated, inventory_changed = [], [], set()
        with self.lock:
            batches = [[(self.by_sku[i["sku"]], int(i["quantity"])) for i in o] for o in (orders or [])]
            batches += [self._random_items() for _ in range(random_orders)]
            for items in batches:
                order = self._new_order(now, items)
                created.append(order.id)
                for product, qty in items:
                    level = self.inventory[product.sku]
                    level.available = max(0, level.available - qty)
                    level.updated_at = now
                    inventory_changed.add(product.sku)
            candidates = sorted(
                (o for o in self.orders.values() if o.fulfillment_status is None and o.cancelled_at is None
                 and o.id not in created),
                key=lambda o: o.id,
            )
            for order in candidates[:updated_orders]:
                order.fulfillment_status = "fulfilled"
                order.updated_at = now
                updated.append(order.id)
            for sku, qty in (restock or {}).items():
                level = self.inventory[sku]
                level.available += int(qty)
                level.updated_at = now
                inventory_changed.add(sku)
        return {"created": created, "updated": updated, "inventory_changed": sorted(inventory_changed)}

    # -- queries ------------------------------------------------------------------------------

    def orders_since(self, updated_at_min: datetime | None, after: tuple[datetime, int] | None) -> list[Order]:
        rows = sorted(self.orders.values(), key=lambda o: (o.updated_at, o.id))
        if updated_at_min:
            rows = [o for o in rows if o.updated_at >= updated_at_min]
        if after:
            rows = [o for o in rows if (o.updated_at, o.id) > after]
        return rows

    def inventory_since(self, updated_at_min: datetime | None, after: tuple[datetime, int] | None):
        rows = sorted(self.inventory.values(), key=lambda lv: (lv.updated_at, lv.product.inventory_item_id))
        if updated_at_min:
            rows = [lv for lv in rows if lv.updated_at >= updated_at_min]
        if after:
            rows = [lv for lv in rows if (lv.updated_at, lv.product.inventory_item_id) > after]
        return rows
