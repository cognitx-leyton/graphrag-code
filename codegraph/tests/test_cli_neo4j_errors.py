"""Tests for Neo4j connection error handling in query, validate, and arch-check commands.

All tests use monkeypatched drivers — no live Neo4j instance required.
"""
from __future__ import annotations

import json

from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable
from typer.testing import CliRunner

from codegraph.cli import app

runner = CliRunner()


# ── Helpers ──────────────────────────────────────────────────────────


class _FakeSession:
    def run(self, cypher, **params):
        raise self._error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, error):
        self._error = error
        self.closed = False

    def verify_connectivity(self):
        raise self._error

    def session(self):
        s = _FakeSession()
        s._error = self._error
        return s

    def close(self):
        self.closed = True


# ── query command ────────────────────────────────────────────────────


def test_query_service_unavailable_json(monkeypatch):
    """query --json emits clean error on ServiceUnavailable."""
    err = ServiceUnavailable("Connection refused")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["query", "RETURN 1", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"
    assert "Connection refused" in data["message"]


def test_query_auth_error_json(monkeypatch):
    """query --json emits clean error on AuthError."""
    err = AuthError("Unauthorized")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["query", "RETURN 1", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"
    assert "Unauthorized" in data["message"]


def test_query_service_unavailable_rich(monkeypatch):
    """query (Rich mode) prints connection error, not a traceback."""
    err = ServiceUnavailable("Connection refused")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["query", "RETURN 1"])
    assert result.exit_code == 2
    assert "connection error" in result.output.lower()
    assert "Traceback" not in result.output


# ── validate command ─────────────────────────────────────────────────


def test_validate_service_unavailable(monkeypatch, tmp_path):
    """validate emits clean error on ServiceUnavailable."""
    err = ServiceUnavailable("Connection refused")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["validate", str(tmp_path), "--json"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"


def test_validate_auth_error(monkeypatch, tmp_path):
    """validate emits clean error on AuthError."""
    err = AuthError("Unauthorized")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["validate", str(tmp_path), "--json"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"


# ── arch-check command ───────────────────────────────────────────────


def test_arch_check_service_unavailable(monkeypatch):
    """arch-check emits clean error on ServiceUnavailable."""
    err = ServiceUnavailable("Connection refused")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["arch-check", "--json", "--no-scope"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"


def test_arch_check_auth_error(monkeypatch):
    """arch-check emits clean error on AuthError."""
    err = AuthError("Unauthorized")
    monkeypatch.setattr(GraphDatabase, "driver", lambda *a, **kw: _FakeDriver(err))

    result = runner.invoke(app, ["arch-check", "--json", "--no-scope"])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "connection"
