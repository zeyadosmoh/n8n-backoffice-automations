"""e2e fixtures. RUN_E2E=1 starts a throwaway stack; E2E_EXTERNAL=1 uses running services (CI).
Without either the tests are skipped; REQUIRE_N8N=1 turns every skip into a failure."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.conftest import skip_or_fail

from .stack import external_stack, local_stack, teardown


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/e2e/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def stack():
    if os.environ.get("E2E_EXTERNAL") == "1":
        s = external_stack()
        yield s
        return
    if os.environ.get("RUN_E2E") != "1":
        skip_or_fail("REQUIRE_N8N", "e2e needs n8n: set RUN_E2E=1 (throwaway docker stack) or E2E_EXTERNAL=1")
    if not shutil.which("docker"):
        skip_or_fail("REQUIRE_N8N", "docker is not available for the e2e stack")
    try:
        s = local_stack()
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"RUN_E2E=1 but the e2e stack did not start: {exc.stderr[-800:]}")
    yield s
    teardown(s)
