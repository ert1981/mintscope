import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tests never use a real key or the developer's local database.
os.environ["OPENSEA_API_KEY"] = ""
os.environ["MINTSCOPE_MODE"] = "auto"
# Legacy tests check the full report; pricing tests turn pricing on explicitly.
os.environ["MINTSCOPE_PRICING"] = "off"
for _k in ("MINTSCOPE_LOCAL", "MINTSCOPE_ADMIN_TOKEN", "MINTSCOPE_SPONSORED_ENABLED", "MINTSCOPE_FEATURED_FEED_URL",
           "MINTSCOPE_PAYEE_ADDRESS", "MINTSCOPE_X402_NETWORK", "MINTSCOPE_X402_FACILITATOR_URL",
           "CDP_API_KEY_ID", "CDP_API_KEY_SECRET"):
    os.environ.pop(_k, None)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(**env):
        monkeypatch.setenv("MINTSCOPE_DB_PATH", str(tmp_path / "test.db"))
        # Never touch the developer's real keyring or config dir.
        monkeypatch.setenv("MINTSCOPE_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setenv("MINTSCOPE_KEYSTORE", "file")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from app import create_app
        from config import get_settings
        base = "http://127.0.0.1:8765" if env.get("MINTSCOPE_LOCAL") == "1" else "http://testserver"
        return TestClient(create_app(get_settings()), base_url=base)
    return _make


@pytest.fixture
def client(make_client):
    return make_client()
