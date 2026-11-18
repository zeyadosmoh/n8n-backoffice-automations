"""Mock Shopify-style Admin REST API (synthetic data).

- GET /admin/api/2025-10/orders.json            orders with line items, filtered by updated_at_min
- GET /admin/api/2025-10/inventory_levels.json  inventory levels, filtered by updated_at_min
  Both sort by (updated_at, id) ascending and paginate with an opaque `page_info` cursor in the Link header.
- Leaky-bucket rate limit (429 + Retry-After) and configurable random 5xx faults.
- /_admin/* (X-Admin-Token): fault settings, simulated orders/restocks, counters. Never rate limited.
- /_fake_gemini/...: a word-overlap test double of Gemini generateContent for the AI-matching e2e tests.
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .fake_gemini import router as fake_gemini_router
from .store import SHOP_TZ, Store, default_anchor

API = "/admin/api/2025-10"
MAX_LIMIT = 250


@dataclass
class Faults:
    fail_rate: float = 0.0  # probability of a random 5xx per API request
    fail_next: int = 0  # the next N API requests fail with 500 (deterministic, for tests)
    bucket_size: int = 40
    leak_rps: float = 2.0
    level: float = 0.0
    last: float = field(default_factory=time.monotonic)
    counters: dict = field(default_factory=lambda: {"requests": 0, "ok": 0, "rate_limited": 0, "server_errors": 0})


class FaultsIn(BaseModel):
    fail_rate: float | None = Field(None, ge=0, le=1)
    fail_next: int | None = Field(None, ge=0)
    bucket_size: int | None = Field(None, ge=1)
    leak_rps: float | None = Field(None, gt=0)
    reset_counters: bool = False


class SimLine(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class SimulateIn(BaseModel):
    orders: list[list[SimLine]] = []
    random_orders: int = Field(0, ge=0, le=500)
    updated_orders: int = Field(0, ge=0, le=500)
    restock: dict[str, int] = {}


def _encode(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(token: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, {"errors": "Invalid page_info"}) from exc


def _parse_ts(value: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, {"errors": f"Invalid timestamp: {value}"}) from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=SHOP_TZ)


def create_app(
    *,
    seed: int | None = None,
    api_token: str | None = None,
    admin_token: str | None = None,
    fail_rate: float | None = None,
    bucket_size: int | None = None,
    leak_rps: float | None = None,
    anchor: datetime | None = None,
    fault_seed: int | None = None,
) -> FastAPI:
    env = os.environ
    seed = seed if seed is not None else int(env.get("MOCK_SEED", "42"))
    api_token = api_token or env.get("STORE_API_TOKEN") or ""
    admin_token = admin_token or env.get("MOCK_ADMIN_TOKEN") or ""
    if not api_token or not admin_token:
        raise RuntimeError("STORE_API_TOKEN and MOCK_ADMIN_TOKEN must be set")

    store = Store(seed=seed, anchor=anchor or default_anchor())
    faults = Faults(
        fail_rate=fail_rate if fail_rate is not None else float(env.get("MOCK_FAIL_RATE", "0")),
        bucket_size=bucket_size or int(env.get("MOCK_BUCKET_SIZE", "40")),
        leak_rps=leak_rps or float(env.get("MOCK_LEAK_RPS", "2")),
    )
    fault_rng = random.Random(fault_seed)
    flock = threading.Lock()

    app = FastAPI(title="P8 mock store (synthetic data)", docs_url="/docs", redoc_url=None)
    app.state.store = store
    app.state.faults = faults
    app.include_router(fake_gemini_router)  # test double for the optional AI matching (no key needed)

    def api_guard(request: Request, x_shopify_access_token: str = Header("")) -> None:
        if not secrets.compare_digest(x_shopify_access_token, api_token):
            raise HTTPException(401, {"errors": "[API] Invalid API key or access token"})
        with flock:
            c = faults.counters
            c["requests"] += 1
            now = time.monotonic()
            faults.level = max(0.0, faults.level - (now - faults.last) * faults.leak_rps)
            faults.last = now
            if faults.level + 1 > faults.bucket_size:
                c["rate_limited"] += 1
                wait = max(1.0, math.ceil((faults.level + 1 - faults.bucket_size) / faults.leak_rps))
                raise HTTPException(
                    429, {"errors": "Exceeded 2 calls per second for api client. Reduce request rates."},
                    headers={"Retry-After": f"{wait:.1f}"},
                )
            faults.level += 1
            request.state.call_limit = f"{math.ceil(faults.level)}/{faults.bucket_size}"
            if faults.fail_next > 0:
                faults.fail_next -= 1
                c["server_errors"] += 1
                raise HTTPException(500, {"errors": "Internal Server Error (injected)"})
            if faults.fail_rate and fault_rng.random() < faults.fail_rate:
                c["server_errors"] += 1
                code = fault_rng.choice([500, 502, 503])
                raise HTTPException(code, {"errors": f"Injected {code}"})
            c["ok"] += 1

    def admin_guard(x_admin_token: str = Header("")) -> None:
        if not secrets.compare_digest(x_admin_token, admin_token):
            raise HTTPException(401, {"errors": "admin token required"})

    def paginate(request: Request, resource: str, rows_fn, key_fn, render_fn, updated_at_min, limit, page_info):
        if page_info:
            cur = _decode(page_info)
            m = _parse_ts(cur["m"]) if cur.get("m") else None
            after = (_parse_ts(cur["a"][0]), int(cur["a"][1]))
            limit = int(cur.get("l", limit))
        else:
            m = _parse_ts(updated_at_min) if updated_at_min else None
            after = None
        with store.lock:
            rows = rows_fn(m, after)
            page = rows[:limit]
            body = {resource: [render_fn(r) for r in page]}
        headers = {"X-Shopify-Shop-Api-Call-Limit": getattr(request.state, "call_limit", "")}
        if len(rows) > limit:
            last_ts, last_id = key_fn(page[-1])
            token = _encode({"m": m.isoformat() if m else None, "a": [last_ts.isoformat(), last_id], "l": limit})
            url = request.url.replace(query=f"limit={limit}&page_info={token}")
            headers["Link"] = f'<{url}>; rel="next"'
        return JSONResponse(body, headers=headers)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "orders": len(store.orders), "skus": len(store.products), "synthetic": True}

    @app.get(f"{API}/orders.json", dependencies=[Depends(api_guard)])
    def orders(
        request: Request,
        updated_at_min: str | None = None,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        page_info: str | None = None,
        status: str = "any",
    ):
        return paginate(
            request, "orders", store.orders_since, lambda o: (o.updated_at, o.id), lambda o: o.to_json(),
            updated_at_min, limit, page_info,
        )

    @app.get(f"{API}/inventory_levels.json", dependencies=[Depends(api_guard)])
    def inventory_levels(
        request: Request,
        updated_at_min: str | None = None,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        page_info: str | None = None,
        location_ids: str | None = None,
    ):
        return paginate(
            request, "inventory_levels", store.inventory_since,
            lambda lv: (lv.updated_at, lv.product.inventory_item_id), lambda lv: lv.to_json(),
            updated_at_min, limit, page_info,
        )

    @app.get("/_admin/state", dependencies=[Depends(admin_guard)])
    def state():
        return {
            "orders": len(store.orders),
            "anchor": store.anchor.isoformat(),
            "faults": {k: getattr(faults, k) for k in ("fail_rate", "fail_next", "bucket_size", "leak_rps")},
            "counters": dict(faults.counters),
            "inventory": {sku: lv.available for sku, lv in store.inventory.items()},
        }

    @app.post("/_admin/faults", dependencies=[Depends(admin_guard)])
    def set_faults(body: FaultsIn):
        with flock:
            for k in ("fail_rate", "fail_next", "bucket_size", "leak_rps"):
                v = getattr(body, k)
                if v is not None:
                    setattr(faults, k, v)
            if body.reset_counters:
                faults.counters = {k: 0 for k in faults.counters}
            faults.level = 0.0
        return state()

    @app.post("/_admin/simulate", dependencies=[Depends(admin_guard)])
    def simulate(body: SimulateIn):
        unknown = {li.sku for o in body.orders for li in o} | set(body.restock)
        unknown -= set(store.by_sku)
        if unknown:
            raise HTTPException(422, {"errors": f"unknown SKUs: {sorted(unknown)}"})
        return store.simulate(
            datetime.now(SHOP_TZ).replace(microsecond=0),
            orders=[[li.model_dump() for li in o] for o in body.orders],
            random_orders=body.random_orders,
            updated_orders=body.updated_orders,
            restock=body.restock,
        )

    return app


def app_from_env() -> FastAPI:  # uvicorn --factory mock_store.app:app_from_env
    return create_app()
