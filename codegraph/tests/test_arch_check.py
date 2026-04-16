"""Tests for :mod:`codegraph.arch_check`.

Each policy function takes a ``neo4j.Driver`` and runs two queries: a count
query and a sample query. We mock both with a fake session that dispatches
on a substring of the Cypher string. That gives us policy-level coverage
without needing a running Neo4j — the Cypher itself is smoke-tested by
``codegraph arch-check`` against the live graph in dev.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

import pytest

from codegraph import arch_check
from codegraph.arch_check import (
    ArchReport,
    PolicyResult,
    _check_cross_package,
    _check_import_cycles,
    _check_layer_bypass,
    run_arch_check,
)


# ── Fake Neo4j driver ───────────────────────────────────────────────


class _FakeResult:
    """Stand-in for a Neo4j result object; supports iteration + ``.single()``."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Routes ``run(cypher, **params)`` to a caller-supplied resolver."""

    def __init__(self, resolver):
        self._resolver = resolver

    def run(self, cypher: str, **params: Any) -> _FakeResult:
        return _FakeResult(self._resolver(cypher, **params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, resolver):
        self._resolver = resolver
        self.closed = False

    def session(self):
        return _FakeSession(self._resolver)

    def close(self):
        self.closed = True


def _constant_driver(answers: dict[str, list[dict]]) -> _FakeDriver:
    """Build a driver whose ``session.run`` returns the row list whose key appears in the query."""

    def resolver(cypher: str, **_params):
        for key, rows in answers.items():
            if key in cypher:
                return rows
        return []

    return _FakeDriver(resolver)


# ── Dataclass shape ─────────────────────────────────────────────────


def test_policy_result_defaults():
    p = PolicyResult(name="x", passed=True, violation_count=0)
    assert p.sample == []
    assert p.detail == ""


def test_arch_report_ok_is_true_when_all_pass():
    report = ArchReport(policies=[
        PolicyResult(name="a", passed=True, violation_count=0),
        PolicyResult(name="b", passed=True, violation_count=0),
    ])
    assert report.ok is True


def test_arch_report_ok_is_false_when_any_fails():
    report = ArchReport(policies=[
        PolicyResult(name="a", passed=True, violation_count=0),
        PolicyResult(name="b", passed=False, violation_count=3),
    ])
    assert report.ok is False


def test_arch_report_to_json_is_valid_and_contains_ok_flag():
    report = ArchReport(policies=[
        PolicyResult(name="a", passed=False, violation_count=2, sample=[{"x": 1}]),
    ])
    blob = json.loads(report.to_json())
    assert blob["ok"] is False
    assert blob["policies"][0]["violation_count"] == 2
    assert blob["policies"][0]["sample"] == [{"x": 1}]


# ── import_cycles ───────────────────────────────────────────────────


def test_import_cycles_clean():
    driver = _constant_driver({
        "count(DISTINCT path) AS v": [{"v": 0}],
        "DISTINCT cycle, hops": [],
    })
    result = _check_import_cycles(driver)
    assert result.name == "import_cycles"
    assert result.passed is True
    assert result.violation_count == 0
    assert result.sample == []


def test_import_cycles_detected():
    sample = [
        {"cycle": ["a.py", "b.py", "a.py"], "hops": 2},
        {"cycle": ["x.py", "y.py", "z.py", "x.py"], "hops": 3},
    ]
    driver = _constant_driver({
        "count(DISTINCT path) AS v": [{"v": 2}],
        "DISTINCT cycle, hops": sample,
    })
    result = _check_import_cycles(driver)
    assert result.passed is False
    assert result.violation_count == 2
    assert result.sample == sample


# ── cross_package ───────────────────────────────────────────────────


def test_cross_package_clean():
    # All pair queries return zero count.
    driver = _constant_driver({
        "count(*) AS v": [{"v": 0}],
    })
    result = _check_cross_package(driver)
    assert result.name == "cross_package"
    assert result.passed is True
    assert result.violation_count == 0
    assert "twenty-front" in result.detail
    assert "twenty-server" in result.detail


def test_cross_package_detected():
    # Violations on the first configured pair; sample rows returned.
    call_log: list[tuple[str, dict]] = []

    def resolver(cypher: str, **params):
        call_log.append((cypher, params))
        if "count(*) AS v" in cypher:
            return [{"v": 2}]
        if "RETURN a.path AS importer" in cypher:
            return [
                {"importer": "packages/twenty-front/src/a.ts",
                 "importee": "packages/twenty-server/src/b.ts"},
                {"importer": "packages/twenty-front/src/c.ts",
                 "importee": "packages/twenty-server/src/d.ts"},
            ]
        return []

    driver = _FakeDriver(resolver)
    result = _check_cross_package(driver)
    assert result.passed is False
    assert result.violation_count == 2
    assert len(result.sample) == 2
    assert result.sample[0]["importer_package"] == "twenty-front"
    assert result.sample[0]["importee_package"] == "twenty-server"


# ── layer_bypass ────────────────────────────────────────────────────


def test_layer_bypass_clean():
    driver = _constant_driver({
        "count(DISTINCT ctrl) AS v": [{"v": 0}],
        "DISTINCT ctrl.name AS controller": [],
    })
    result = _check_layer_bypass(driver)
    assert result.name == "layer_bypass"
    assert result.passed is True
    assert result.violation_count == 0


def test_layer_bypass_detected():
    sample = [
        {"controller": "UserController", "repository": "UserRepository", "method": "find"},
    ]
    driver = _constant_driver({
        "count(DISTINCT ctrl) AS v": [{"v": 1}],
        "DISTINCT ctrl.name AS controller": sample,
    })
    result = _check_layer_bypass(driver)
    assert result.passed is False
    assert result.violation_count == 1
    assert result.sample == sample


# ── Orchestrator ────────────────────────────────────────────────────


def test_run_arch_check_aggregates_three_policies(monkeypatch):
    """``run_arch_check`` opens a driver, runs all 3 policies, closes it."""
    fake_driver = _constant_driver({
        # Every count query in every policy returns 0 → all pass.
        "count(DISTINCT path) AS v": [{"v": 0}],
        "count(*) AS v": [{"v": 0}],
        "count(DISTINCT ctrl) AS v": [{"v": 0}],
    })

    def _fake_driver_factory(uri, auth):
        assert uri == "bolt://fake:7687"
        assert auth == ("neo4j", "pw")
        return fake_driver

    monkeypatch.setattr(arch_check.GraphDatabase, "driver", _fake_driver_factory)

    report = run_arch_check("bolt://fake:7687", "neo4j", "pw", console=None)
    assert fake_driver.closed is True
    assert report.ok is True
    policy_names = [p.name for p in report.policies]
    assert policy_names == ["import_cycles", "cross_package", "layer_bypass"]


def test_run_arch_check_closes_driver_even_on_failure(monkeypatch):
    """Driver lifecycle must be finally-protected."""
    fake_driver = _constant_driver({})
    fake_driver._resolver = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))

    monkeypatch.setattr(arch_check.GraphDatabase, "driver", lambda uri, auth: fake_driver)

    with pytest.raises(RuntimeError):
        run_arch_check("bolt://fake:7687", "neo4j", "pw", console=None)
    assert fake_driver.closed is True
