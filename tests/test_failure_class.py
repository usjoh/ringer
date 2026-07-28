#!/usr/bin/env python3
"""Failure attribution: which layer produced a non-PASS attempt.

Only FAILURE_CLASS_MODEL belongs in a per-model pass rate. Everything else is
the harness, the manifest, or the machine, and counting it against a model
corrupts the routing signal the scoreboard exists to provide.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    FAILURE_CLASS_CHECK_TIMEOUT,
    FAILURE_CLASS_DELIVERABLE_PATH,
    FAILURE_CLASS_ENGINE,
    FAILURE_CLASS_MODEL,
    FAILURE_CLASS_UNKNOWN,
    FAILURE_CLASS_WORKER_TIMEOUT,
    VerifyResult,
    WorkerResult,
    classify_attempt_failure,
    model_log_row_failure_class,
    model_log_row_is_model_attributable,
)


def worker(returncode=0, *, timed_out=False, tokens=1000, error=None) -> WorkerResult:
    return WorkerResult(returncode=returncode, timed_out=timed_out, tokens=tokens, error=error)


def verify(check_returncode=1, *, check_timed_out=False, missing=()) -> VerifyResult:
    return VerifyResult(
        ok=False,
        check_returncode=check_returncode,
        check_timed_out=check_timed_out,
        raw_output_excerpt="",
        missing_files=tuple(missing),
    )


class ClassifyAttemptFailureTests(unittest.TestCase):
    def test_pass_has_no_failure_class(self) -> None:
        self.assertEqual("", classify_attempt_failure("PASS", worker(), verify(0)))

    def test_worker_error_is_an_engine_error(self) -> None:
        # Ringer's own signal that the worker never ran — e.g. the worktree
        # taskdir collision seen on 2026-07-15.
        result = classify_attempt_failure(
            "FAIL", worker(1, error="worktree taskdir already exists: /tmp/x"), verify()
        )
        self.assertEqual(FAILURE_CLASS_ENGINE, result)

    def test_worker_timeout_is_not_a_model_failure(self) -> None:
        self.assertEqual(
            FAILURE_CLASS_WORKER_TIMEOUT,
            classify_attempt_failure("FAIL", worker(None, timed_out=True), verify()),
        )

    def test_check_timeout_is_not_a_model_failure(self) -> None:
        self.assertEqual(
            FAILURE_CLASS_CHECK_TIMEOUT,
            classify_attempt_failure("FAIL", worker(0), verify(None, check_timed_out=True)),
        )

    def test_passing_check_with_absent_deliverables_is_a_path_defect(self) -> None:
        # lou-call-transcript, 2026-07-16: whisper wrote the transcripts, every
        # check exited 0 reporting full coverage, and all four tasks were
        # recorded FAIL because expect_files resolved to a sibling directory.
        result = classify_attempt_failure(
            "FAIL", worker(0), verify(0, missing=("out/chunk1/chunk1.txt",))
        )
        self.assertEqual(FAILURE_CLASS_DELIVERABLE_PATH, result)

    def test_clean_engine_exit_with_failing_check_is_the_model(self) -> None:
        # The engine did its job and the check rejected what came back. This is
        # the only shape that is evidence about the model.
        self.assertEqual(
            FAILURE_CLASS_MODEL,
            classify_attempt_failure("FAIL", worker(0), verify(1, missing=("report.md",))),
        )

    def test_nonzero_exit_without_a_recorded_error_is_unknown(self) -> None:
        self.assertEqual(
            FAILURE_CLASS_UNKNOWN, classify_attempt_failure("FAIL", worker(1), verify(1))
        )

    def test_no_tokens_is_not_treated_as_proof_no_model_ran(self) -> None:
        # The tempting shortcut. Checked against the surviving worker logs of
        # the 18 rows it matches in this repo's own eval log: WRONG for 5 of
        # them, where opencode drove the model through 20-30 tool calls and
        # then exited without emitting a parseable token count. Calling that an
        # engine error would excuse a real model failure as infrastructure.
        result = classify_attempt_failure("FAIL", worker(1, tokens=None), verify(1))
        self.assertEqual(FAILURE_CLASS_UNKNOWN, result)
        self.assertNotEqual(FAILURE_CLASS_ENGINE, result)


class RowAttributionTests(unittest.TestCase):
    def row(self, verdict="FAIL", *, notes="", failure_class=None) -> dict[str, object]:
        row: dict[str, object] = {"verdict": verdict, "notes": notes}
        if failure_class is not None:
            row["failure_class"] = failure_class
        return row

    def test_explicit_class_wins(self) -> None:
        row = self.row(failure_class=FAILURE_CLASS_DELIVERABLE_PATH, notes="worker_returncode=0")
        self.assertEqual(FAILURE_CLASS_DELIVERABLE_PATH, model_log_row_failure_class(row))
        self.assertFalse(model_log_row_is_model_attributable(row))

    def test_class_is_read_back_from_notes(self) -> None:
        row = self.row(notes="worker_returncode=0\nfailure_class=check-timeout\n")
        self.assertEqual(FAILURE_CLASS_CHECK_TIMEOUT, model_log_row_failure_class(row))

    def test_pass_rows_are_always_attributable(self) -> None:
        self.assertTrue(model_log_row_is_model_attributable(self.row("PASS")))

    def test_historical_clean_exit_is_attributed_to_the_model(self) -> None:
        row = self.row(notes="worker_returncode=0\ntask_type=code-review\n")
        self.assertEqual(FAILURE_CLASS_MODEL, model_log_row_failure_class(row))
        self.assertTrue(model_log_row_is_model_attributable(row))

    def test_historical_missing_deliverables_without_check_rc_is_unknown(self) -> None:
        # Pre-check_returncode rows cannot distinguish "the model wrote
        # nothing" from "verification looked in the wrong place", and the
        # second really happened. Charging the ambiguity to the model is the
        # bias being removed.
        row = self.row(
            notes='worker_returncode=0\nmissing_expect_files=["out/chunk1/chunk1.txt"]\n'
        )
        self.assertEqual(FAILURE_CLASS_UNKNOWN, model_log_row_failure_class(row))
        self.assertFalse(model_log_row_is_model_attributable(row))

    def test_new_row_with_check_rc_is_classified_not_guessed(self) -> None:
        # Once check_returncode is present the same shape is decidable, so it
        # must NOT fall into the historical unknown bucket.
        row = self.row(
            notes=(
                "worker_returncode=0\ncheck_returncode=1\n"
                'missing_expect_files=["report.md"]\n'
            )
        )
        self.assertEqual(FAILURE_CLASS_MODEL, model_log_row_failure_class(row))

    def test_unexplained_failure_is_not_charged_to_the_model(self) -> None:
        self.assertFalse(model_log_row_is_model_attributable(self.row(notes="")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
