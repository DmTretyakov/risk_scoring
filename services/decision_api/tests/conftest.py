"""Fixtures. Runs inside the `tests` compose service (layout: /srv/{app,tests,db,data,reference,scripts})."""
import csv
import importlib.util
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def replay():
    return _load("replay_csv", ROOT / "scripts" / "replay_csv.py")


@pytest.fixture(scope="session")
def simulate():
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load("simulate_traffic", ROOT / "scripts" / "simulate_traffic.py")


@pytest.fixture(scope="session")
def prototype():
    return _load("risk_scoring_prototype", ROOT / "reference" / "risk_scoring_prototype.py")


@pytest.fixture(scope="session")
def csv_rows():
    with open(ROOT / "data" / "raw_applications.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="session")
def database_url():
    """Fresh throwaway database with the real migration applied."""
    url = os.environ["DATABASE_URL"]
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(url, autocommit=True) as conn:
        for migration in sorted((ROOT / "db").glob("*.sql")):
            conn.execute(migration.read_text())
    return url


@pytest.fixture
def db(database_url):
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE drift_runs, drift_reference, loans, evaluations, applications, companies")
        yield conn


@pytest.fixture
def redis_client():
    import redis
    client = redis.Redis.from_url(os.environ["REDIS_URL"])
    client.flushdb()
    yield client
    client.close()


@pytest.fixture
def make_client(db, redis_client, monkeypatch):
    """Starts the app with extra environment, e.g. make_client(RATE_LIMIT_CAPACITY="3")."""
    from fastapi.testclient import TestClient
    from app.main import app
    started = []

    def start(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        client = TestClient(app)
        client.__enter__()
        started.append(client)
        return client

    yield start
    for client in started:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()
