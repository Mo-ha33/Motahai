"""
The merchant portal is mounted at /app only when portal/dist exists at startup.

The check runs in a subprocess so the import-time mount decision can be made
with dist reported as absent, without touching the test process or the real
portal/dist directory.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = r"""
import json
import pathlib

_real_is_dir = pathlib.Path.is_dir

def _no_portal_dist(self):
    if self.name == "dist" and self.parent.name == "portal":
        return False
    return _real_is_dir(self)

pathlib.Path.is_dir = _no_portal_dist

from fastapi.testclient import TestClient
from src.ameen_workforce.service import app

client = TestClient(app)
print(json.dumps({
    "health": client.get("/health").status_code,
    "app": client.get("/app/").status_code,
}))
"""


def test_app_starts_and_portal_is_404_without_dist():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout.strip().splitlines()[-1])
    assert status == {"health": 200, "app": 404}
