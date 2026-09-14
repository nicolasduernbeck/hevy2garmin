"""The dashboard "Sync Now" job must survive the browser tab closing.

It used to be a client-side loop: the browser called /api/sync-one repeatedly
and closing the tab (or losing the connection) silently stopped the sync
mid-way. These endpoints move the loop into a server-side background task
(/api/sync-job/start, /api/sync-job/status, /api/sync-job/stop) so a sync keeps running
independent of any single HTTP request.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_state():
    from hevy2garmin import server

    server._manual_sync_state.clear()
    server._manual_sync_state["active"] = False
    yield
    server._manual_sync_state.clear()
    server._manual_sync_state["active"] = False


@pytest.fixture
def client(monkeypatch):
    os.environ.pop("HEVY2GARMIN_SECRET", None)
    os.environ.pop("DEMO_MODE", None)
    from hevy2garmin import server

    monkeypatch.setattr(server, "_is_configured_cache", True)
    monkeypatch.setattr(server, "_record_sync_log", lambda *a, **kw: None)
    yield TestClient(server.app, follow_redirects=False)


def _stub_steps(monkeypatch, payloads: list[dict]):
    """Make each call to _do_sync_one return the next canned payload."""
    from hevy2garmin import server

    it = iter(payloads)

    async def _fake(*, respect_grace=False, **kw):
        return JSONResponse(next(it))

    monkeypatch.setattr(server, "_do_sync_one", _fake)


class TestRunManualSyncJob:
    def test_processes_workouts_until_done(self, monkeypatch):
        from hevy2garmin import server

        _stub_steps(monkeypatch, [
            {"synced": 1, "title": "Push Day", "remaining": 1, "done": False},
            {"synced": 1, "title": "Leg Day", "remaining": 0, "done": True},
        ])
        server._reset_manual_sync_state()

        asyncio.run(server._run_manual_sync_job())

        state = server._manual_sync_state
        assert state["active"] is False
        assert state["done"] is True
        assert state["synced"] == 2
        assert state["current_title"] == "Leg Day"
        assert state.get("error") is None

    def test_stops_after_current_step_when_requested(self, monkeypatch):
        from hevy2garmin import server

        calls = {"n": 0}

        async def _fake(*, respect_grace=False, **kw):
            calls["n"] += 1
            # Ask to stop as soon as the first step lands, before the loop
            # would otherwise take a second step.
            server._manual_sync_state["stop_requested"] = True
            return JSONResponse({"synced": 1, "title": "Push Day", "remaining": 5, "done": False})

        monkeypatch.setattr(server, "_do_sync_one", _fake)
        server._reset_manual_sync_state()

        asyncio.run(server._run_manual_sync_job())

        assert calls["n"] == 1
        assert server._manual_sync_state["active"] is False
        assert server._manual_sync_state["synced"] == 1

    def test_error_stops_the_job_and_is_reported(self, monkeypatch):
        from hevy2garmin import server

        _stub_steps(monkeypatch, [{"error": "Garmin upload failed"}])
        server._reset_manual_sync_state()

        asyncio.run(server._run_manual_sync_job())

        state = server._manual_sync_state
        assert state["active"] is False
        assert state["done"] is True
        assert state["error"] == "Garmin upload failed"

    def test_nothing_to_sync_finishes_immediately(self, monkeypatch):
        from hevy2garmin import server

        _stub_steps(monkeypatch, [{"synced": 0, "remaining": 0, "done": True}])
        server._reset_manual_sync_state()

        asyncio.run(server._run_manual_sync_job())

        state = server._manual_sync_state
        assert state["active"] is False
        assert state["synced"] == 0
        assert state["done"] is True


class TestSyncEndpoints:
    def test_start_reports_busy_without_double_starting(self, client):
        from hevy2garmin import server

        server._manual_sync_state.update(active=True, synced=3)
        resp = client.post("/api/sync-job/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["started"] is False
        assert data["busy"] is True
        assert data["synced"] == 3

    def test_status_reflects_current_state(self, client):
        from hevy2garmin import server

        server._manual_sync_state.update(active=True, synced=2, total=5, current_title="Pull Day")
        resp = client.get("/api/sync-job/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["synced"] == 2
        assert data["current_title"] == "Pull Day"

    def test_status_before_any_sync_reports_inactive(self, client):
        resp = client.get("/api/sync-job/status")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_stop_sets_flag_only_when_a_job_is_active(self, client):
        from hevy2garmin import server

        server._reset_manual_sync_state()
        resp = client.post("/api/sync-job/stop")
        assert resp.status_code == 200
        assert server._manual_sync_state["stop_requested"] is True

    def test_stop_is_a_no_op_when_nothing_is_running(self, client):
        resp = client.post("/api/sync-job/stop")
        assert resp.status_code == 200
        assert resp.json() == {"stopping": True}
