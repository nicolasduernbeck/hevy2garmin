# AI Handover — hevy2garmin

## What is this?

A Python package that syncs gym workouts from Hevy to Garmin Connect. The core value is the exercise mapping — 438 Hevy exercise names mapped to Garmin's FIT SDK category/subcategory IDs, so exercises display correctly in Garmin Connect instead of showing as "Other."

## Why does it exist?

Hevy doesn't sync to Garmin natively. When you log a workout in Hevy, it stays in Hevy. This tool bridges that gap by:

1. Pulling workouts via Hevy's API
2. Generating proper Garmin FIT files with exercise structure
3. Uploading to Garmin Connect with correct names and descriptions

## Architecture

```
Hevy API → HevyClient → workout JSON
                            ↓
                     ExerciseMapper (438 mappings)
                            ↓
                     FIT Generator (fit-tool SDK)
                            ↓
                     Garmin Upload (garmin-auth + garminconnect)
                            ↓
                     SQLite (track what's synced)
```

## Key files

- `src/hevy2garmin/mapper.py` — 438-entry Hevy→Garmin exercise lookup table. Pure data, no dependencies.
- `src/hevy2garmin/fit.py` — Generates FIT files from Hevy workout JSON. Uses fit-tool SDK. Handles timing (set duration, rest periods), calorie estimation (Keytel formula), and HR overlay.
- `src/hevy2garmin/hevy.py` — Hevy API v1 client with retry/rate limiting.
- `src/hevy2garmin/garmin.py` — Garmin upload (FIT), rename, description. Uses garmin-auth for authentication.
- `src/hevy2garmin/sync.py` — Orchestrator: pull from Hevy → generate FIT → upload to Garmin → track in SQLite.
- `src/hevy2garmin/prs.py` — Personal-record detection: highest actual `weight_kg` per exercise, warm-ups excluded, strictly-greater comparison, no 1RM formulas. Pure logic, isolated from Garmin and UI concerns.
- `src/hevy2garmin/db.py` — SQLite storage for tracking synced workouts.
- `src/hevy2garmin/cli.py` — CLI: sync, status, list commands.

## Personal records

```
Hevy workout → prs.record_workout_prs() during sync_one_workout()
                   ↓ (strictly higher weight than stored max?)
              pr_events table (SQLite/Postgres, UNIQUE(exercise_key, hevy_workout_id))
                   ↓
              🏆 NEW PRs block in generate_description() → Garmin activity
                   ↓
              /prs dashboard page (current + history), POST /api/prs/sync
              rebuilds the whole history chronologically from the Hevy API
              (idempotent replace-all)
```

Exercises are keyed by Hevy `exercise_template_id` (title fallback for custom
exercises). PR failures never fail a sync. Garmin's native Personal Records are
not written — there is no API for that.

## Dependencies

- `garmin-auth` — Our own package for Garmin OAuth (self-healing auth)
- `garminconnect` — Garmin Connect API client
- `fit-tool` — FIT file SDK for generating Garmin-compatible files
- `requests` — HTTP client for Hevy API

## FIT file details

The FIT generator creates strength-training activities with:

- Workout message (title, timestamp)
- Exercise messages (category, subcategory from mapper)
- Set messages (reps, weight, duration)
- HR records (if available from Garmin daily monitoring)
- Calorie estimation via Keytel formula

Timing is estimated: 40s per working set, 25s per warmup, 75s rest between sets, 120s between exercises.

## Parent project

Extracted from the Soma fitness platform (github.com/drkostas/soma). Soma imports hevy2garmin via PyPI.
