"""Run a workflows/src/*.js Code-node script under Node with stubbed n8n globals ($json, $input, $, $env).

The unit tests exercise the exact source that ships inside the workflow JSON, with recorded fixtures and no
network. CI sets REQUIRE_NODE=1 so a missing `node` fails instead of skipping.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from p8.config import ROOT

from .conftest import skip_or_fail

HARNESS = r"""
const ctx = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const wrap = (j) => ({ json: j });
const $json = ctx.json;
const $input = { first: () => wrap(ctx.items[0]), all: () => ctx.items.map(wrap), item: wrap(ctx.json) };
const $ = (name) => {
  if (!(name in ctx.nodes)) throw new Error(`test harness: node "${name}" not stubbed`);
  const v = ctx.nodes[name];
  return { item: wrap(v), first: () => wrap(v), all: () => [wrap(v)] };
};
const $env = ctx.env;
Math.random = () => 0;   // deterministic jitter
let out;
try {
  out = { ok: true, result: new Function('$json', '$input', '$', '$env', ctx.code)($json, $input, $, $env) };
} catch (e) {
  out = { ok: false, error: e.message };
}
process.stdout.write(JSON.stringify(out));
"""


class CodeNodeError(Exception):
    pass


def run_code(src: str, json_in=None, items=None, nodes=None, env=None):
    """Return the script's output items' json (list), or raise CodeNodeError with the thrown message."""
    if not shutil.which("node"):
        skip_or_fail("REQUIRE_NODE", "node not installed")
    code = (ROOT / "workflows" / "src" / f"{src}.js").read_text()
    if items is None:
        items = [json_in] if json_in is not None else []
    ctx = {"code": code, "json": json_in if json_in is not None else (items[0] if items else {}), "items": items,
           "nodes": nodes or {}, "env": env or {}}
    proc = subprocess.run(["node", "-e", HARNESS], input=json.dumps(ctx), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    out = json.loads(proc.stdout)
    if not out["ok"]:
        raise CodeNodeError(out["error"])
    res = out["result"]
    if isinstance(res, dict):            # runOnceForEachItem returns one {json}
        res = [res]
    return [r["json"] for r in res]


def raises(src: str, **kw) -> str:
    with pytest.raises(CodeNodeError) as exc:
        run_code(src, **kw)
    return str(exc.value)
