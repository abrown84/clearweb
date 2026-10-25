import os, tempfile
os.environ.setdefault("CLEARWEB_DATA_DIR", tempfile.mkdtemp())
os.environ["CLEARWEB_ADMIN_KEY"] = "test-admin-key-not-real"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

remote = TestClient(app, client=("203.0.113.9", 5000))
local = TestClient(app, client=("127.0.0.1", 5000))


def test_health_minimal():
    r = remote.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_local_only():
    assert remote.get("/ready").status_code == 404


def test_search_requires_key():
    assert remote.post("/search", json={"query": "x"}).status_code == 401


def test_query_param_key_ignored():
    assert remote.post("/search?api_key=test-admin-key-not-real", json={"query": "x"}).status_code == 401


def test_admin_remote_404():
    assert remote.get("/admin/keys", headers={"Authorization": "Bearer test-admin-key-not-real"}).status_code == 404


def test_admin_local_and_extract_blocked():
    h = {"Authorization": "Bearer test-admin-key-not-real"}
    k = local.post("/admin/keys", json={"label": "t"}, headers=h).json()
    r = remote.post("/extract", json={"urls": ["http://127.0.0.1:3055/admin/keys", "http://[::ffff:169.254.169.254]/"]},
                    headers={"Authorization": f"Bearer {k['api_key']}"})
    assert r.status_code == 200 and not r.json()["results"] and len(r.json()["failed_results"]) == 2
    assert local.post(f"/admin/keys/{k['id']}/revoke", headers=h).json()["revoked"]
