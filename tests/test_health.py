"""Tests for the liveness/readiness health probes.

The old ``/health`` returned a static 200 even when the database was
unreachable, so Docker never restarted an unhealthy container. These tests
lock in that the probe actually pings the database and returns 503 on failure.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import routes_health
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_returns_200_when_database_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "database_ok"


def test_health_returns_503_when_database_unreachable(client, monkeypatch):
    monkeypatch.setattr(
        routes_health, "_probe_database",
        lambda: (False, "database_unreachable: simulated"),
    )
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert "simulated" in body["database"]


def test_readiness_returns_200_when_schema_present(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["database"] == "database_ok"
    assert body["schema"] == "schema_ok"


def test_readiness_returns_503_when_schema_incomplete(client, monkeypatch):
    monkeypatch.setattr(
        routes_health, "_critical_tables_present",
        lambda: (False, "schema_incomplete: found 0/2 critical tables"),
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
