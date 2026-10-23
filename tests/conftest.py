import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config, db, export
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "web.sqlite3")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(export, "SUMMARY_DIR", tmp_path / "summaries")
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, follow_redirects=True)
