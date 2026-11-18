"""scripts/secret-scan.sh catches token shapes and the literal .env secret values without echoing them."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from p8.config import ROOT

SECRET = "shpss_" + "5e" * 16          # synthetic, generated for this test only


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    for rel in ["scripts/secret-scan.sh", "scripts/check_workflows_clean.py"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    shutil.copytree(ROOT / "workflows", tmp_path / "workflows")
    (tmp_path / ".env").write_text(f"SHOPIFY_CLIENT_SECRET={SECRET}\nPOSTGRES_PASSWORD=change-me\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def scan(repo, *files):
    subprocess.run(["git", "add", *files], cwd=repo, check=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}   # no gitleaks: exercise the grep fallback
    return subprocess.run(["bash", "scripts/secret-scan.sh"], cwd=repo, capture_output=True, text=True, env=env)


def test_clean_repo_passes(repo):
    (repo / "notes.md").write_text("nothing to see\n")
    out = scan(repo, "notes.md", "scripts", "workflows")
    assert out.returncode == 0, out.stdout + out.stderr


def test_client_secret_value_is_caught_but_not_printed(repo):
    # value without the shpss_ prefix: only the literal-value check can find it
    (repo / "notes.md").write_text(f"oops {SECRET[6:]}\n")
    (repo / ".env").write_text(f"SHOPIFY_CLIENT_SECRET={SECRET[6:]}\n")
    out = scan(repo, "notes.md")
    assert out.returncode == 1
    assert "notes.md:1" in out.stdout and "value of a secret" in out.stdout
    assert SECRET[6:] not in out.stdout + out.stderr


@pytest.mark.parametrize("prefix", ["shpat_", "shpca_", "shpss_"])
def test_shopify_token_shapes_are_caught(repo, prefix):
    (repo / ".env").write_text("")
    (repo / "notes.md").write_text(prefix + "ab12" * 8 + "\n")
    out = scan(repo, "notes.md")
    assert out.returncode == 1 and "shp(at|ss|ca|pa)" in out.stdout and "notes.md:1" in out.stdout
    assert "ab12" * 8 not in out.stdout             # the token itself is never echoed
