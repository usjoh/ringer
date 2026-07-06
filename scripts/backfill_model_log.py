#!/usr/bin/env python3
"""Backfill ``model`` and ``task_type`` into a legacy Ringer eval log.

Older Ringer versions wrote eval-log rows without the ``model`` and
``task_type`` keys that the new ``./ringer.py models`` scoreboard depends on.
This tool enriches an existing JSONL log in place by:

  * looking up each row's ``model`` from the Ringer run-state file
    ``<runs-dir>/<run_id>.json`` (matching ``task_key`` against the run-state's
    ``tasks[].key``), and
  * looking up each row's ``task_type`` from a flat mapping JSON using the
    precedence ``<run_id>:<task_key>`` > ``<run_id>`` > ``name:<prefix>``.

Existing non-empty values are never overwritten; malformed lines are preserved
byte-for-byte in their original position. The script is idempotent and uses
only the Python standard library.

Usage:
    python3 scripts/backfill_model_log.py --log PATH --runs-dir PATH \
        --mapping PATH [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone


def _split_line(raw: str) -> tuple[str, str]:
    """Split a raw file line into ``(body, newline)`` preserving its terminator.

    ``body`` is the line content without the trailing line terminator; ``newline``
    is the terminator that was stripped (``""``, ``"\\n"`` or ``"\\r\\n"``) so
    that re-joining ``body + newline`` reproduces the original bytes exactly.
    """
    body = raw
    newline = ""
    if body.endswith("\n"):
        if body.endswith("\r\n"):
            newline = "\r\n"
            body = body[:-2]
        else:
            newline = "\n"
            body = body[:-1]
    return body, newline


def load_mapping(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("mapping file must contain a JSON object")
    return data


def load_run_state(runs_dir: str, run_id) -> dict | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    candidate = os.path.join(runs_dir, f"{run_id}.json")
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def model_from_run_state(run_state: dict | None, task_key) -> str | None:
    if run_state is None or not isinstance(task_key, str) or not task_key:
        return None
    tasks = run_state.get("tasks")
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("key") == task_key:
            model = task.get("model")
            if isinstance(model, str) and model:
                return model
            return None
    return None


def task_type_from_mapping(mapping: dict, run_id, task_key) -> str | None:
    if not isinstance(run_id, str) or not run_id:
        return None

    # 1. most specific: "<run_id>:<task_key>"
    if isinstance(task_key, str) and task_key:
        exact = mapping.get(f"{run_id}:{task_key}")
        if isinstance(exact, str) and exact:
            return exact

    # 2. exact "<run_id>"
    run_match = mapping.get(run_id)
    if isinstance(run_match, str) and run_match:
        return run_match

    # 3. any "name:<prefix>" where run_id startswith prefix; longest wins
    best: str | None = None
    best_len = -1
    for key, value in mapping.items():
        if not isinstance(key, str) or not key.startswith("name:"):
            continue
        prefix = key[len("name:"):]
        if not prefix:
            continue
        if run_id.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best = value if isinstance(value, str) else None
    if isinstance(best, str) and best:
        return best
    return None


def process_lines(raw_lines: list[str], runs_dir: str, mapping: dict):
    """Return ``(out_lines, summary)`` enriching each JSON-object line in place."""
    summary = {
        "rows_scanned": 0,
        "malformed_preserved": 0,
        "models_filled": 0,
        "task_types_filled": 0,
        "rows_untouched": 0,
    }
    out_lines: list[str] = []
    run_state_cache: dict[str, dict | None] = {}

    for raw in raw_lines:
        summary["rows_scanned"] += 1
        body, newline = _split_line(raw)
        out_lines.append(raw)  # default: preserve verbatim

        stripped = body.strip()
        if not stripped:
            summary["malformed_preserved"] += 1
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            summary["malformed_preserved"] += 1
            continue
        if not isinstance(obj, dict):
            summary["malformed_preserved"] += 1
            continue

        run_id = obj.get("run_id")
        task_key = obj.get("task_key")
        touched = False

        current_model = obj.get("model")
        if not (isinstance(current_model, str) and current_model):
            cache_key = run_id if isinstance(run_id, str) else ""
            if cache_key not in run_state_cache:
                run_state_cache[cache_key] = load_run_state(runs_dir, cache_key)
            model = model_from_run_state(run_state_cache[cache_key], task_key)
            if model:
                obj["model"] = model
                touched = True
                summary["models_filled"] += 1

        current_task_type = obj.get("task_type")
        if not (isinstance(current_task_type, str) and current_task_type):
            task_type = task_type_from_mapping(mapping, run_id, task_key)
            if task_type:
                obj["task_type"] = task_type
                touched = True
                summary["task_types_filled"] += 1

        if touched:
            out_lines[-1] = json.dumps(obj, ensure_ascii=False) + newline
        else:
            summary["rows_untouched"] += 1

    return out_lines, summary


def print_summary(summary: dict) -> None:
    print(
        "rows scanned:            {rows_scanned}\n"
        "malformed preserved:     {malformed_preserved}\n"
        "models filled:           {models_filled}\n"
        "task_types filled:       {task_types_filled}\n"
        "rows untouched:          {rows_untouched}".format(**summary)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill `model` and `task_type` into a Ringer eval log.",
    )
    parser.add_argument("--log", required=True, help="path to the JSONL eval log")
    parser.add_argument(
        "--runs-dir", required=True, help="directory of <run_id>.json run-state files"
    )
    parser.add_argument(
        "--mapping", required=True, help="path to the task_type mapping JSON object"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the summary but do not rewrite the log or create a backup",
    )
    args = parser.parse_args(argv)

    log_path = args.log
    runs_dir = args.runs_dir
    mapping_path = args.mapping
    dry_run = args.dry_run

    if not os.path.isfile(log_path):
        print(f"error: log file not found: {log_path}", file=sys.stderr)
        return 1
    if not os.path.isdir(runs_dir):
        print(f"error: runs-dir not found: {runs_dir}", file=sys.stderr)
        return 1
    if not os.path.isfile(mapping_path):
        print(f"error: mapping file not found: {mapping_path}", file=sys.stderr)
        return 1

    try:
        mapping = load_mapping(mapping_path)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"error: could not load mapping: {exc}", file=sys.stderr)
        return 1

    with open(log_path, "r", encoding="utf-8") as fh:
        raw_lines = fh.readlines()

    out_lines, summary = process_lines(raw_lines, runs_dir, mapping)

    if not dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = f"{log_path}.bak-{stamp}"
        shutil.copy2(log_path, backup_path)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.writelines(out_lines)

    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
