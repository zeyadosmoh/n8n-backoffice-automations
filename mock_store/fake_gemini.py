"""Fake Gemini generateContent endpoint: a TEST DOUBLE so the AI-matching branch runs in e2e tests and CI without an
API key. It is not an AI. It reads the ROWS_JSON / CATALOG_JSON blocks from the prompt and scores each name against
the catalog by word overlap (plural "s" stripped). The real workflow calls Google's API when GEMINI_API_BASE is unset.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request

router = APIRouter()


def _tokens(s: str) -> set[str]:
    out = set()
    for w in re.split(r"[^a-z0-9]+", s.lower()):
        if not w:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def _block(text: str, tag: str):
    m = re.search(rf"{tag}:\s*(\[.*?\])\s*(?:\n[A-Z_]+:|\Z)", text, re.S)
    return json.loads(m.group(1)) if m else []


def match(rows: list[dict], catalog: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        t = _tokens(r.get("name", ""))
        best, score = None, 0.0
        for c in catalog:
            ct = _tokens(c["name"])
            s = len(t & ct) / len(t | ct) if t | ct else 0
            if s > score:
                best, score = c["sku"], s
        out.append({"row_no": r["row_no"], "sku": best if score >= 0.3 else None, "confidence": round(score, 2)})
    return out


@router.post("/_fake_gemini/v1beta/models/{model}:generateContent")
async def generate(model: str, request: Request):
    body = await request.json()
    text = "\n".join(p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", []))
    result = match(_block(text, "ROWS_JSON"), _block(text, "CATALOG_JSON"))
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(result)}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": len(text) // 4, "candidatesTokenCount": len(result) * 12},
        "modelVersion": f"fake-{model}",
    }
