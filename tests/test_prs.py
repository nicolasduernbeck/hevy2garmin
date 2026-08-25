"""Tests for personal record (PR) tracking."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import hevy2garmin.server as srv
from hevy2garmin.db_sqlite import SQLiteDatabase
from hevy2garmin.prs import (
    detect_new_prs,
    exercise_key,
    group_pr_history,
    rebuild_pr_history,
    record_workout_prs,
    workout_max_weights,
)

BENCH = "79D0BB3A"
OHP = "878CD1D0"


def _set(weight, reps=5, set_type="normal"):
    return {"type": set_type, "weight_kg": weight, "reps": reps}


def _ex(title, sets, template_id=None):
    ex = {"title": title, "sets": sets}
    if template_id:
        ex["exercise_template_id"] = template_id
    return ex


def _workout(wid, start, exercises, title=None):
    return {
        "id": wid,
        "title": title or f"Workout {wid}",
        "start_time": start,
        "end_time": start,
        "exercises": exercises,
    }


def _store(tmp_path: Path) -> SQLiteDatabase:
    return SQLiteDatabase(tmp_path / "sync.db")


class TestWorkoutMaxWeights:
    def test_highest_weight_wins_regardless_of_reps(self) -> None:
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(80, 10), _set(90, 8), _set(100, 5)], BENCH),
        ])
        result = workout_max_weights(w)
        assert result[f"template:{BENCH}"]["weight_kg"] == 100.0

    def test_warmup_sets_ignored(self) -> None:
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [
                _set(40, 10, "warmup"), _set(60, 8, "warmup"),
                _set(80, 8), _set(100, 5),
            ], BENCH),
        ])
        result = workout_max_weights(w)
        assert result[f"template:{BENCH}"]["weight_kg"] == 100.0

    def test_warmup_only_exercise_has_no_candidate(self) -> None:
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Band Pull Apart", [_set(120, 10, "warmup")]),
        ])
        assert workout_max_weights(w) == {}

    def test_bodyweight_sets_ignored(self) -> None:
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Pull Up", [{"type": "normal", "weight_kg": None, "reps": 10},
                            {"type": "normal", "weight_kg": 0, "reps": 8}]),
        ])
        assert workout_max_weights(w) == {}

    def test_failure_and_dropset_count_as_working_sets(self) -> None:
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(90, 5), _set(100, 1, "failure")], BENCH),
        ])
        assert workout_max_weights(w)[f"template:{BENCH}"]["weight_kg"] == 100.0


class TestExerciseIdentity:
    def test_template_id_preferred_over_title(self) -> None:
        a = _ex("Bench Press (Barbell)", [], BENCH)
        b = _ex("bench press (BARBELL) ", [], BENCH)
        assert exercise_key(a) == exercise_key(b)

    def test_title_fallback_normalized(self) -> None:
        a = _ex("My Custom Lift", [])
        b = _ex("  my custom lift ", [])
        assert exercise_key(a) == exercise_key(b)
        assert exercise_key(a) != exercise_key(_ex("Other Lift", []))


class TestDetectAndRecord:
    def test_first_weight_becomes_pr(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(80, 8)], BENCH),
        ])
        events = record_workout_prs(store, w)
        assert len(events) == 1
        assert events[0]["weight_kg"] == 80.0
        assert events[0]["hevy_workout_id"] == "w1"
        assert store.get_pr_maxima()[f"template:{BENCH}"]["weight_kg"] == 80.0

    def test_higher_weight_new_pr(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record_workout_prs(store, _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(95, 5)], BENCH)]))
        events = record_workout_prs(store, _workout("w2", "2026-08-08T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)]))
        assert len(events) == 1
        assert events[0]["weight_kg"] == 100.0
        assert len(store.get_pr_history()) == 2

    def test_lower_weight_no_pr(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record_workout_prs(store, _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)]))
        events = record_workout_prs(store, _workout("w2", "2026-08-08T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(95, 10)], BENCH)]))
        assert events == []
        assert len(store.get_pr_history()) == 1

    def test_equal_weight_no_pr(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record_workout_prs(store, _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)]))
        events = record_workout_prs(store, _workout("w2", "2026-08-08T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 8)], BENCH)]))
        assert events == []
        assert len(store.get_pr_history()) == 1

    def test_multiple_exercises_multiple_prs_in_one_workout(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH),
            _ex("Shoulder Press (Dumbbell)", [_set(34, 8)], OHP),
        ])
        events = record_workout_prs(store, w)
        assert len(events) == 2
        assert {e["weight_kg"] for e in events} == {100.0, 34.0}

    def test_no_pr_workout_produces_no_records(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record_workout_prs(store, _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)]))
        record_workout_prs(store, _workout("w2", "2026-08-08T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(90, 5)], BENCH),
            _ex("Pull Up", [{"type": "normal", "weight_kg": None, "reps": 10}]),
        ]))
        assert len(store.get_pr_history()) == 1

    def test_reprocessing_same_workout_no_duplicates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)])
        first = record_workout_prs(store, w)
        second = record_workout_prs(store, w)
        assert len(store.get_pr_history()) == 1
        # Re-sync still reports this workout's PR so descriptions stay stable.
        assert first == second

    def test_detect_uses_actual_weight_not_reps(self) -> None:
        maxima = {f"template:{BENCH}": {"weight_kg": 95.0, "hevy_workout_id": "old"}}
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 1)], BENCH)])
        events = detect_new_prs(w, maxima)
        assert len(events) == 1 and events[0]["weight_kg"] == 100.0


class TestRebuild:
    HISTORY = [
        ("w1", "2026-08-01T10:00:00+00:00", 80),
        ("w2", "2026-08-08T10:00:00+00:00", 90),
        ("w3", "2026-08-15T10:00:00+00:00", 85),
        ("w4", "2026-08-22T10:00:00+00:00", 100),
    ]

    def _workouts(self):
        return [
            _workout(wid, start, [_ex("Bench Press (Barbell)", [_set(weight, 5)], BENCH)])
            for wid, start, weight in self.HISTORY
        ]

    def test_history_records_only_improvements(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = rebuild_pr_history(store, self._workouts())
        assert result == {"workouts": 4, "events": 3, "exercises": 1}
        history = store.get_pr_history()
        assert [(e["hevy_workout_id"], e["weight_kg"]) for e in sorted(history, key=lambda e: e["achieved_at"])] == [
            ("w1", 80.0), ("w2", 90.0), ("w4", 100.0),
        ]

    def test_processes_chronologically_regardless_of_input_order(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        rebuild_pr_history(store, list(reversed(self._workouts())))
        history = sorted(store.get_pr_history(), key=lambda e: e["achieved_at"])
        assert [e["weight_kg"] for e in history] == [80.0, 90.0, 100.0]

    def test_rebuild_twice_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        rebuild_pr_history(store, self._workouts())
        once = store.get_pr_history()
        rebuild_pr_history(store, self._workouts())
        twice = store.get_pr_history()
        strip = lambda evs: [{k: v for k, v in e.items() if k != "created_at"} for e in evs]
        assert strip(once) == strip(twice)

    def test_rebuild_replaces_stale_events(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.upsert_pr_event({
            "exercise_key": "title:ghost", "exercise_title": "Ghost", "weight_kg": 50.0,
            "reps": 5, "achieved_at": "2026-01-01T10:00:00+00:00",
            "hevy_workout_id": "deleted", "workout_title": "Old",
        })
        rebuild_pr_history(store, self._workouts())
        assert all(e["exercise_key"] != "title:ghost" for e in store.get_pr_history())


class TestGroupPrHistory:
    def test_groups_current_and_previous(self) -> None:
        events = [
            {"exercise_key": "k1", "exercise_title": "Bench Press", "weight_kg": 100.0,
             "achieved_at": "2026-08-25", "hevy_workout_id": "w2"},
            {"exercise_key": "k1", "exercise_title": "Bench Press", "weight_kg": 95.0,
             "achieved_at": "2026-08-20", "hevy_workout_id": "w1"},
            {"exercise_key": "k2", "exercise_title": "Squat", "weight_kg": 140.0,
             "achieved_at": "2026-08-22", "hevy_workout_id": "w3"},
        ]
        groups = group_pr_history(events)
        assert [g["title"] for g in groups] == ["Bench Press", "Squat"]
        bench = groups[0]
        assert bench["current"]["weight_kg"] == 100.0
        assert [p["weight_kg"] for p in bench["previous"]] == [95.0]


class TestLegacyStoreDoubles:
    def test_store_without_pr_methods_is_skipped(self) -> None:
        class Legacy:
            pass

        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)])
        assert record_workout_prs(Legacy(), w) == []


def _client(store: SQLiteDatabase):
    srv._is_configured_cache = True
    return patch.object(srv.db, "get_db", return_value=store), TestClient(srv.app)


class TestSyncIntegration:
    CFG = {"merge_mode": False, "hr_fusion": {"enabled": False}, "sync": {"grace_period_minutes": 0}}

    def _sync(self, store, workout):
        from hevy2garmin.sync import sync_one_workout

        with patch("hevy2garmin.sync.find_activity_by_start_time", return_value=123), \
                patch("hevy2garmin.sync.rename_activity"), \
                patch("hevy2garmin.sync.set_description") as set_desc:
            result = sync_one_workout(workout, cfg=self.CFG, garmin_client=MagicMock(), database=store)
        return result, set_desc

    def test_new_pr_lands_in_garmin_description(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(40, 10, "warmup"), _set(100, 5)], BENCH)])
        w["end_time"] = "2026-08-01T11:00:00+00:00"
        result, set_desc = self._sync(store, w)
        assert result.status == "synced"
        desc = set_desc.call_args[0][2]
        assert "🏆 NEW PRs" in desc
        assert "• Bench Press (Barbell): 100 kg" in desc
        assert len(store.get_pr_history()) == 1

    def test_resync_keeps_description_and_no_duplicates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        w = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)])
        w["end_time"] = "2026-08-01T11:00:00+00:00"
        _, first = self._sync(store, w)
        _, second = self._sync(store, w)
        assert first.call_args[0][2] == second.call_args[0][2]
        assert len(store.get_pr_history()) == 1

    def test_no_pr_workout_keeps_existing_description(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        pr = _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)])
        pr["end_time"] = "2026-08-01T11:00:00+00:00"
        self._sync(store, pr)
        weaker = _workout("w2", "2026-08-08T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(90, 8)], BENCH)])
        weaker["end_time"] = "2026-08-08T11:00:00+00:00"
        result, set_desc = self._sync(store, weaker)
        assert result.status == "synced"
        assert "🏆" not in set_desc.call_args[0][2]
        assert len(store.get_pr_history()) == 1


class TestPrEndpoints:
    def test_prs_page_renders(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record_workout_prs(store, _workout("w1", "2026-08-01T10:00:00+00:00", [
            _ex("Bench Press (Barbell)", [_set(100, 5)], BENCH)], title="Push Day"))
        db_patch, client = _client(store)
        with db_patch, client:
            response = client.get("/prs")
        assert response.status_code == 200
        assert "Bench Press (Barbell)" in response.text
        assert "100 kg" in response.text
        assert "Sync PRs" in response.text

    def test_prs_page_empty_state(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        db_patch, client = _client(store)
        with db_patch, client:
            response = client.get("/prs")
        assert response.status_code == 200
        assert "No personal records yet" in response.text

    def test_api_prs_sync_rebuilds_history(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workouts = [
            _workout("w1", "2026-08-01T10:00:00+00:00", [
                _ex("Bench Press (Barbell)", [_set(80, 8)], BENCH)]),
            _workout("w2", "2026-08-22T10:00:00+00:00", [
                _ex("Bench Press (Barbell)", [_set(100, 3)], BENCH)]),
        ]
        hevy = MagicMock()
        hevy.get_all_workouts.return_value = workouts
        db_patch, client = _client(store)
        with db_patch, client, \
                patch.object(srv, "load_config", return_value={"hevy_api_key": "k"}), \
                patch("hevy2garmin.hevy.HevyClient", return_value=hevy):
            response = client.post("/api/prs/sync")
        assert response.status_code == 200
        assert "PR history rebuilt" in response.text
        assert "100 kg" in response.text
        assert len(store.get_pr_history()) == 2

    def test_api_prs_sync_error_shows_toast(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        db_patch, client = _client(store)
        with db_patch, client, \
                patch.object(srv, "load_config", return_value={"hevy_api_key": "k"}), \
                patch("hevy2garmin.hevy.HevyClient", side_effect=RuntimeError("hevy down")):
            response = client.post("/api/prs/sync")
        assert response.status_code == 200
        assert "toast-error" in response.text
        assert "hevy down" in response.text
