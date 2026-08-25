"""Personal record (PR) tracking — highest actual weight per exercise.

A PR is only the highest actual ``weight_kg`` successfully recorded for an
exercise. No estimated 1RM, no formulas, no volume or rep records. Warm-up
sets never count; sets without a positive actual weight (bodyweight, cardio)
never create a PR.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hevy2garmin")

_PR_STORE_METHODS = ("get_pr_maxima", "upsert_pr_event", "replace_pr_events")


def exercise_key(exercise: dict) -> str:
    """Stable identity for an exercise across workouts.

    Prefers Hevy's ``exercise_template_id``; falls back to the normalized
    title for custom exercises without one. Prefixes prevent collisions
    between the two namespaces.
    """
    template_id = exercise.get("exercise_template_id")
    if template_id:
        return f"template:{template_id}"
    title = (exercise.get("title") or exercise.get("name") or "").strip()
    return f"title:{title.lower()}"


def workout_max_weights(workout: dict) -> dict[str, dict]:
    """Highest working-set weight per exercise in one workout.

    Returns ``{exercise_key: {"weight_kg", "reps", "title"}}``. Warm-up sets
    (``type == "warmup"``, matching the FIT generator's working-set rule) and
    sets without a positive actual weight are ignored.
    """
    result: dict[str, dict] = {}
    for ex in workout.get("exercises", []):
        title = ex.get("title") or ex.get("name") or "Unknown"
        key = exercise_key(ex)
        for s in ex.get("sets", []):
            if s.get("type", "normal") == "warmup":
                continue
            raw = s.get("weight_kg")
            if raw is None:
                raw = s.get("weight")
            try:
                weight = float(raw)
            except (TypeError, ValueError):
                continue
            if weight <= 0:
                continue
            best = result.get(key)
            if best is None or weight > best["weight_kg"]:
                result[key] = {"weight_kg": weight, "reps": s.get("reps"), "title": title}
    return result


def _event(workout: dict, key: str, best: dict) -> dict:
    return {
        "exercise_key": key,
        "exercise_title": best["title"],
        "weight_kg": best["weight_kg"],
        "reps": best["reps"],
        "achieved_at": workout.get("start_time") or workout.get("startTime", ""),
        "hevy_workout_id": workout.get("id", ""),
        "workout_title": workout.get("title", ""),
    }


def detect_new_prs(workout: dict, current_maxima: dict[str, dict]) -> list[dict]:
    """PR events this workout creates versus the known maxima.

    ``current_maxima`` maps exercise_key to ``{"weight_kg", "hevy_workout_id"}``.
    A strictly greater actual weight is a new PR; equal weight only counts
    when the stored PR came from this same workout (re-sync stability).
    """
    events: list[dict] = []
    wid = workout.get("id", "")
    for key, best in workout_max_weights(workout).items():
        prev = current_maxima.get(key)
        if (
            prev is None
            or best["weight_kg"] > prev["weight_kg"]
            or (best["weight_kg"] == prev["weight_kg"] and prev.get("hevy_workout_id") == wid)
        ):
            events.append(_event(workout, key, best))
    return events


def record_workout_prs(store: Any, workout: dict) -> list[dict]:
    """Detect and persist new PRs for one workout. Idempotent.

    Returns the PR events for this workout (also on re-sync of the same
    workout, so the Garmin description stays stable across retries). Stores
    without the PR methods (legacy test doubles) are skipped silently.
    """
    if not all(callable(getattr(store, m, None)) for m in _PR_STORE_METHODS):
        return []
    maxima = store.get_pr_maxima()
    events = detect_new_prs(workout, maxima)
    for event in events:
        store.upsert_pr_event(event)
    return events


def group_pr_history(events: list[dict]) -> list[dict]:
    """Group PR events for display: ``[{title, current, previous}]`` by exercise.

    ``current`` is the highest-weight event; ``previous`` holds older records,
    highest first. Sorted alphabetically by exercise title.
    """
    groups: dict[str, list[dict]] = {}
    for event in events:
        groups.setdefault(event.get("exercise_key", ""), []).append(event)
    result = []
    for group in groups.values():
        ordered = sorted(group, key=lambda e: e.get("weight_kg") or 0, reverse=True)
        result.append({
            "title": ordered[0].get("exercise_title") or "Unknown",
            "current": ordered[0],
            "previous": ordered[1:],
        })
    result.sort(key=lambda g: g["title"].lower())
    return result


def rebuild_pr_history(store: Any, workouts: list[dict]) -> dict:
    """Replay the full workout history chronologically and replace all PR events.

    Deterministic and idempotent: running it twice yields identical rows.
    Returns ``{"workouts", "events", "exercises"}`` counts.
    """
    ordered = sorted(workouts, key=lambda w: w.get("start_time") or w.get("startTime", ""))
    maxima: dict[str, float] = {}
    events: list[dict] = []
    for workout in ordered:
        for key, best in workout_max_weights(workout).items():
            prev = maxima.get(key)
            if prev is None or best["weight_kg"] > prev:
                maxima[key] = best["weight_kg"]
                events.append(_event(workout, key, best))
    count = store.replace_pr_events(events)
    return {"workouts": len(workouts), "events": count, "exercises": len(maxima)}
