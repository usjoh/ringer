#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    AppConfig,
    ArtifactConfig,
    EngineConfig,
    EvalConfig,
    Manifest,
    TaskSpec,
    Verifier,
    lint_manifest,
)


LONG_SPEC = (
    "Create the requested artifact in the current working directory, keep the change scoped, "
    "and make the check command able to explain any failure clearly."
)

GOOD_CHECK = (
    "test -s output.txt && grep -q 'ready' output.txt || "
    "{ echo 'FAIL: output.txt missing or does not contain ready'; exit 1; }"
)


class LintManifestTests(unittest.TestCase):
    def manifest(
        self,
        tasks: list[dict[str, object]],
        *,
        worktrees: bool = False,
        max_parallel: int = 1,
    ) -> Manifest:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        obj: dict[str, object] = {
            "run_name": "lint-test",
            "workdir": str(Path(temp_dir.name) / "work"),
            "max_parallel": max_parallel,
            "worktrees": worktrees,
            "tasks": tasks,
        }
        if worktrees:
            obj["repo"] = temp_dir.name
        return Manifest.from_obj(obj)

    def task(
        self,
        key: str = "one",
        *,
        spec: str = LONG_SPEC,
        check: str = GOOD_CHECK,
        expect_files: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "key": key,
            "spec": spec,
            "check": check,
            "expect_files": ["output.txt"] if expect_files is None else expect_files,
            "verified": "the output file exists and contains the expected content",
        }

    def assertHasFinding(self, findings: list[str], expected: str) -> None:
        self.assertIn(expected, findings, f"expected lint finding not found: {expected}\nfindings: {findings}")

    def test_task_fields_must_be_strings(self) -> None:
        with self.assertRaisesRegex(ValueError, r"task one: check must be a string"):
            self.manifest([self.task(check=["cmd1", "cmd2"])])  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, r"task one: spec must be a string"):
            self.manifest([self.task(spec=["write it"])])  # type: ignore[arg-type]

        task = self.task()
        task["key"] = 123
        with self.assertRaisesRegex(ValueError, r"task key must be a string"):
            self.manifest([task])

    def test_w1_unverifiable_check(self) -> None:
        manifest = self.manifest([self.task(check="echo ok && echo done")])
        self.assertHasFinding(
            lint_manifest(manifest),
            "one: check cannot fail, so the task cannot be verified.",
        )

        commented_manifest = self.manifest([self.task(check="true # worker left the placeholder check")])
        self.assertHasFinding(
            lint_manifest(commented_manifest),
            "one: check cannot fail, so the task cannot be verified.",
        )

        quoted_hash_manifest = self.manifest(
            [
                self.task(
                    check=(
                        "test -s '#artifact' || "
                        "{ echo 'FAIL: #artifact missing'; exit 1; }"
                    )
                )
            ]
        )
        self.assertNotIn(
            "one: check cannot fail, so the task cannot be verified.",
            lint_manifest(quoted_hash_manifest),
        )

    def test_w2_silent_check(self) -> None:
        manifest = self.manifest([self.task(check="test -f output.txt && [ -s report.md ]")])
        self.assertHasFinding(
            lint_manifest(manifest),
            "one: check may fail without printing why; retry prompt and eval log depend on failure output.",
        )

        diff_manifest = self.manifest([self.task(check="diff -q expected.txt actual.txt")])
        self.assertHasFinding(
            lint_manifest(diff_manifest),
            "one: check may fail without printing why; retry prompt and eval log depend on failure output.",
        )

        diff_with_output = self.manifest(
            [self.task(check="diff -q a b || { echo FAIL; diff a b; exit 1; }")]
        )
        self.assertNotIn(
            "one: check may fail without printing why; retry prompt and eval log depend on failure output.",
            lint_manifest(diff_with_output),
        )

        grep_manifest = self.manifest([self.task(check="grep -q x file")])
        self.assertHasFinding(
            lint_manifest(grep_manifest),
            "one: check may fail without printing why; retry prompt and eval log depend on failure output.",
        )

        probe_chain_manifest = self.manifest([self.task(check="grep -q x file && test -s output.txt")])
        self.assertHasFinding(
            lint_manifest(probe_chain_manifest),
            "one: check may fail without printing why; retry prompt and eval log depend on failure output.",
        )

    def test_w3_worktree_deliverable_loss(self) -> None:
        manifest = self.manifest(
            [self.task(expect_files=["report.md"])],
            worktrees=True,
        )
        self.assertHasFinding(
            lint_manifest(manifest),
            "one: deliverable would be deleted with the worktree; write it outside the worktree or export it in the check.",
        )

    def test_w4_worktree_commit_loss(self) -> None:
        spec = LONG_SPEC + " After the file is correct, run git commit with a concise message."
        manifest = self.manifest(
            [self.task(spec=spec, expect_files=[])],
            worktrees=True,
        )
        self.assertHasFinding(
            lint_manifest(manifest),
            "one: worker commits die with the worktree; have the worker leave changes uncommitted and export the diff in the check.",
        )

        negated_spec = LONG_SPEC + " Do NOT run `git commit`; leave the worktree uncommitted."
        negated_manifest = self.manifest(
            [self.task(spec=negated_spec, expect_files=[])],
            worktrees=True,
        )
        self.assertNotIn(
            "one: worker commits die with the worktree; have the worker leave changes uncommitted and export the diff in the check.",
            lint_manifest(negated_manifest),
        )

    def test_w5_serial_fan_out(self) -> None:
        manifest = self.manifest(
            [
                self.task("one", expect_files=["one.txt"]),
                self.task("two", expect_files=["two.txt"]),
                self.task("three", expect_files=["three.txt"]),
            ],
            max_parallel=1,
        )
        self.assertHasFinding(
            lint_manifest(manifest),
            "manifest: tasks will run serially; set max_parallel.",
        )

    def test_w6_write_collision(self) -> None:
        manifest = self.manifest(
            [
                self.task("one", expect_files=["/tmp/shared-deliverable.txt"]),
                self.task("two", expect_files=["/tmp/shared-deliverable.txt"]),
            ],
            worktrees=False,
        )
        self.assertHasFinding(
            lint_manifest(manifest),
            "manifest: write collision on /tmp/shared-deliverable.txt: listed by one, two.",
        )

    def test_w6_relative_paths_do_not_collide(self) -> None:
        # Relative expect_files resolve inside each task's own directory —
        # many tasks emitting report.md/extraction.json is the NORMAL swarm
        # shape, not a collision (first field use caught this false positive).
        manifest = self.manifest(
            [
                self.task("one", expect_files=["report.md"]),
                self.task("two", expect_files=["report.md"]),
                self.task("three", expect_files=["report.md"]),
            ],
            worktrees=False,
            max_parallel=3,
        )
        self.assertEqual([], lint_manifest(manifest))

    def test_verifier_expands_user_expect_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            taskdir = Path(root) / "task"
            home = Path(root) / "home"
            taskdir.mkdir()
            home.mkdir()
            (home / "report.md").write_text("done\n", encoding="utf-8")
            previous_home = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                task = TaskSpec(
                    key="one",
                    spec=LONG_SPEC,
                    check="true",
                    expect_files=("~/report.md",),
                )
                result = asyncio.run(Verifier().verify(task, taskdir))
            finally:
                if previous_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous_home
        self.assertTrue(result.ok, result.raw_output_excerpt)
        self.assertEqual((), result.missing_files)

    def test_w7_underspecified_spec(self) -> None:
        manifest = self.manifest([self.task(spec="Do it.")])
        self.assertHasFinding(
            lint_manifest(manifest),
            "one: spec is probably underspecified; workers are stateless and cannot ask questions.",
        )

    def test_w8_file_pointer_spec(self) -> None:
        findings = lint_manifest(
            self.manifest(
                [self.task(spec="Read the instructions at /tmp/brief.md and do exactly what it says in there.")]
            )
        )
        self.assertTrue(
            any("pointer to an instruction file" in item for item in findings),
            f"expected pointer-spec finding, got: {findings}",
        )

        # A long spec that references files as source material is fine.
        long_spec = (
            "You are a read-only reviewer. Study the code bundle at /tmp/bundle.txt as your "
            "source material, then write ./review.md with sections VERDICT, BLOCKERS, and "
            "EVIDENCE. For every blocker cite file and line from the bundle. Do not modify "
            "any file other than ./review.md. The review must judge correctness, security, "
            "and migration safety, and each claim needs a quoted line of code as evidence. "
            "If a concern cannot be verified from the bundle alone, list it under an "
            "UNCERTAIN heading instead of asserting it. Keep the verdict to one sentence. "
            "Write plainly; the reader is a busy maintainer deciding whether to merge today."
        )
        findings = lint_manifest(self.manifest([self.task(spec=long_spec, expect_files=["review.md"])]))
        self.assertFalse(
            any("pointer to an instruction file" in item for item in findings),
            f"long contextual spec should not be flagged: {findings}",
        )

    def test_w9_missing_expect_files(self) -> None:
        findings = lint_manifest(self.manifest([self.task(expect_files=[])]))
        self.assertTrue(
            any("no expect_files" in item for item in findings),
            f"expected missing-expect_files finding, got: {findings}",
        )

        # Worktrees mode legitimately exports deliverables outside the
        # taskdir (patch export), so the finding must not fire there.
        findings = lint_manifest(
            self.manifest([self.task(expect_files=[])], worktrees=True)
        )
        self.assertFalse(
            any("no expect_files" in item for item in findings),
            f"worktrees manifest should not be flagged for expect_files: {findings}",
        )

    def test_compliant_manifest_is_clean(self) -> None:
        manifest = self.manifest(
            [
                self.task("one", expect_files=["one.txt"]),
                self.task("two", expect_files=["two.txt"]),
                self.task("three", expect_files=["three.txt"]),
            ],
            max_parallel=2,
        )
        self.assertEqual([], lint_manifest(manifest), "compliant manifest should have no lint findings")

    def test_w10_deliverable_verified_where_nothing_writes(self) -> None:
        # The lou-call-transcript shape (2026-07-16): the spec sends output to
        # an absolute directory that is a SIBLING of the task directory, while
        # expect_files stays relative — so verification looks inside the task
        # directory, finds nothing, and records FAIL even though the check
        # passed on the real files. Four tasks, ~5.3 hours of CPU, zero output.
        spec = (
            "You are a one-task transcription runner. Run whisper on the chunk and let it "
            "finish; it is CPU transcription and may take 10-30 minutes. Execute exactly: "
            "whisper /work/chunks/chunk1.mp3 --model turbo --output_format all "
            "--output_dir /work/out/chunk1. Leave the files exactly where whisper puts them. "
            "Do not paraphrase, summarize, or fabricate a transcript."
        )
        check = (
            "python3 /work/check_chunk.py --json /work/out/chunk1/chunk1.json "
            "--txt /work/out/chunk1/chunk1.txt --dur 947"
        )
        findings = lint_manifest(
            self.manifest(
                [self.task(spec=spec, check=check, expect_files=["out/chunk1/chunk1.txt"])]
            )
        )
        errors = [item for item in findings if item.startswith("ERROR:")]
        self.assertEqual(1, len(errors), f"expected exactly one blocking finding, got: {findings}")
        self.assertIn("out/chunk1/chunk1.txt", errors[0])
        self.assertIn("/work/out/chunk1/chunk1.txt", errors[0])
        # Blocking matters: a warning would have printed into an unwatched run
        # and changed nothing.
        self.assertTrue(errors[0].startswith("ERROR:"), errors[0])

    def test_w10_reference_input_with_same_name_is_not_a_mismatch(self) -> None:
        # `diff /golden/report.md report.md` names the deliverable relatively
        # too, so the absolute path is a reference INPUT, not the output
        # location. Firing here would block a legitimate manifest.
        findings = lint_manifest(
            self.manifest(
                [
                    self.task(
                        check="diff /golden/report.md report.md || { echo 'FAIL: drift'; exit 1; }",
                        expect_files=["report.md"],
                    )
                ]
            )
        )
        self.assertEqual(
            [], [item for item in findings if item.startswith("ERROR:")], findings
        )

    def test_w10_absolute_expect_files_are_exempt(self) -> None:
        # An absolute expect_files entry is verified exactly where it is
        # declared, so it cannot disagree with the task directory.
        findings = lint_manifest(
            self.manifest(
                [
                    self.task(
                        spec=LONG_SPEC + " Write the transcript to /work/out/chunk1/chunk1.txt.",
                        check="test -s /work/out/chunk1/chunk1.txt || { echo 'FAIL'; exit 1; }",
                        expect_files=["/work/out/chunk1/chunk1.txt"],
                    )
                ]
            )
        )
        self.assertEqual(
            [], [item for item in findings if item.startswith("ERROR:")], findings
        )

    def test_w10_task_naming_the_resolved_path_is_clean(self) -> None:
        # When the task also names the path verification will actually use,
        # the two agree and there is nothing to warn about. Build the workdir
        # explicitly: self.manifest() mints a fresh temp dir per call, so a
        # taskdir borrowed from a second manifest is a genuinely different path.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workdir = Path(temp_dir.name) / "work"
        deliverable = (workdir / "one" / "report.md").resolve()
        manifest = Manifest.from_obj(
            {
                "run_name": "lint-test",
                "workdir": str(workdir),
                "max_parallel": 1,
                "tasks": [
                    self.task(
                        spec=LONG_SPEC + f" Write your report to {deliverable}.",
                        check=f"test -s {deliverable} || {{ echo 'FAIL: no report'; exit 1; }}",
                        expect_files=["report.md"],
                    )
                ],
            }
        )
        findings = lint_manifest(manifest)
        self.assertEqual(
            [], [item for item in findings if item.startswith("ERROR:")], findings
        )

    def w10_manifest(self, *, spec_tail: str, check: str, expect_files: list[str]) -> Manifest:
        """A one-task manifest whose workdir is resolved up front.

        Paths typed into a spec must be compared against the RESOLVED workdir:
        tempfile hands back /var/... which resolves to /private/var/... on
        macOS, and treating those as different paths is exactly the bug this
        helper's callers pin down.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workdir = (Path(temp_dir.name) / "work").resolve()
        return Manifest.from_obj(
            {
                "run_name": "lint-test",
                "workdir": str(workdir),
                "max_parallel": 1,
                "tasks": [
                    self.task(
                        spec=LONG_SPEC + " " + spec_tail.format(taskdir=workdir / "one"),
                        check=check.format(taskdir=workdir / "one"),
                        expect_files=expect_files,
                    )
                ],
            }
        )

    def test_w10_mismatch_inside_the_task_directory_still_fires(self) -> None:
        # Landing in the task directory is not enough — verification looks at
        # exactly <taskdir>/<expect_files entry>. Writing to a subdirectory of
        # the task directory fails the same way as writing to a sibling.
        findings = lint_manifest(
            self.w10_manifest(
                spec_tail="Write the report to {taskdir}/sub/report.md.",
                check="test -s {taskdir}/sub/report.md || {{ echo 'FAIL: no report'; exit 1; }}",
                expect_files=["report.md"],
            )
        )
        errors = [item for item in findings if item.startswith("ERROR:")]
        self.assertEqual(1, len(errors), f"in-taskdir mismatch should fire: {findings}")
        self.assertIn("sub/report.md", errors[0])

    def test_w10_same_name_reference_input_beside_the_real_path_is_clean(self) -> None:
        # The task names the path verification will actually use AND a
        # same-named file elsewhere. The latter is a reference input; firing
        # here would block a legitimate manifest.
        findings = lint_manifest(
            self.w10_manifest(
                spec_tail="Use /golden/report.md as the format reference, then write {taskdir}/report.md.",
                check="test -s {taskdir}/report.md || {{ echo 'FAIL: no report'; exit 1; }}",
                expect_files=["report.md"],
            )
        )
        self.assertEqual([], [item for item in findings if item.startswith("ERROR:")], findings)

    def test_w10_symlinked_path_is_not_a_false_mismatch(self) -> None:
        # A spec typed with the path a human uses ('/tmp/run/out/report.md')
        # against a workdir stored RESOLVED ('/private/tmp/...' on macOS)
        # describes the SAME file. Comparing the two written forms directly
        # reports agreement as a mismatch.
        #
        # Build the symlink here rather than leaning on /tmp: /tmp is a symlink
        # on macOS and a real directory on Linux, so borrowing the platform's
        # own layout makes the test pass for the wrong reason on one of them.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name).resolve()
        real = root / "real"
        real.mkdir()
        link = root / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform guard
            self.skipTest(f"symlinks unavailable on this platform: {exc}")

        as_typed = link / "work" / "one" / "report.md"
        manifest = Manifest.from_obj(
            {
                "run_name": "lint-test",
                "workdir": str(link / "work"),
                "max_parallel": 1,
                "tasks": [
                    self.task(
                        spec=LONG_SPEC + f" Write the report to {as_typed}.",
                        check=f"test -s {as_typed} || {{ echo 'FAIL: no report'; exit 1; }}",
                        expect_files=["report.md"],
                    )
                ],
            }
        )
        self.assertEqual(
            manifest.workdir,
            real / "work",
            "manifest workdir should resolve through the symlink",
        )
        self.assertNotEqual(
            str(manifest.workdir), str(link / "work"), "test needs the two written forms to differ"
        )
        findings = lint_manifest(manifest)
        self.assertEqual([], [item for item in findings if item.startswith("ERROR:")], findings)

    def test_w10_matches_on_path_shape_not_bare_filename(self) -> None:
        # The rule matches the declared path SUFFIX, not just the basename.
        # A multi-component deliverable like out/chunk1/chunk1.txt must not be
        # flagged against every unrelated chunk1.txt a spec happens to name —
        # here the input audio directory — or the guard becomes noise and gets
        # switched off.
        # Neither earlier suppression applies here: no absolute path equals the
        # resolved deliverable, and 'out/chunk1/chunk1.txt' never appears as a
        # bare token. So the suffix comparison alone decides the outcome, and a
        # basename-only rule would fire on the unrelated /inputs/chunk1.txt.
        findings = lint_manifest(
            self.w10_manifest(
                spec_tail=(
                    "Read the source audio index at /inputs/chunk1.txt for reference, then "
                    "write the transcript as chunk1.txt inside the out/chunk1 folder of your "
                    "own task directory."
                ),
                check="python3 /tools/verify_chunk.py --dir out/chunk1 || {{ echo 'FAIL: bad chunk'; exit 1; }}",
                expect_files=["out/chunk1/chunk1.txt"],
            )
        )
        self.assertEqual([], [item for item in findings if item.startswith("ERROR:")], findings)

    def test_w10_verifier_explains_a_passing_check_with_missing_deliverables(self) -> None:
        # The runtime half: when the check exits 0 but the declared
        # deliverables are absent, the old output was the bare relative list
        # jammed in front of the check's own success text, which read as "the
        # worker produced nothing". Name the resolved path instead.
        with tempfile.TemporaryDirectory() as root:
            taskdir = Path(root) / "task"
            taskdir.mkdir()
            task = TaskSpec(
                key="one",
                spec=LONG_SPEC,
                check="echo 'OK: 2906 words, coverage to 947s of 947s'",
                expect_files=("out/chunk1/chunk1.txt",),
            )
            result = asyncio.run(Verifier().verify(task, taskdir))
        self.assertFalse(result.ok)
        self.assertEqual(0, result.check_returncode)
        excerpt = result.raw_output_excerpt
        self.assertIn("the check PASSED (exit 0)", excerpt)
        self.assertIn(str(taskdir / "out/chunk1/chunk1.txt"), excerpt)
        self.assertIn("declare the deliverable as an absolute path", excerpt)
        # The check's own output must survive alongside the explanation.
        self.assertIn("2906 words", excerpt)

    def test_w10_failing_check_keeps_the_plain_missing_message(self) -> None:
        # The explainer is specifically about a PASSING check disagreeing with
        # verification. A genuinely failing check should not be told its files
        # were merely in the wrong place.
        with tempfile.TemporaryDirectory() as root:
            taskdir = Path(root) / "task"
            taskdir.mkdir()
            task = TaskSpec(
                key="one",
                spec=LONG_SPEC,
                check="echo 'FAIL: whisper died'; exit 1",
                expect_files=("out/chunk1/chunk1.txt",),
            )
            result = asyncio.run(Verifier().verify(task, taskdir))
        self.assertFalse(result.ok)
        self.assertIn("missing expected files", result.raw_output_excerpt)
        self.assertNotIn("the check PASSED", result.raw_output_excerpt)

    def w12_config(self) -> AppConfig:
        """A config with one write-confined engine and one opaque one.

        'codex' mirrors the real config's workspace-write sandbox; 'opencode'
        mirrors the wrapper-script engine whose confinement is invisible from
        config — the rule must stay silent for it rather than guess.
        """
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        return AppConfig(
            path=None,
            identity_default=None,
            state_dir=root,
            dashboard_port_base=8787,
            hud_port=8700,
            hud_app_path=None,
            allow_full_access=False,
            eval=EvalConfig(backend="jsonl", jsonl_path=root / "eval.jsonl"),
            engines={
                "codex": EngineConfig(
                    name="codex",
                    bin="codex",
                    args_template=("exec", "{spec}"),
                    full_access_args=("--dangerously-bypass-approvals-and-sandbox",),
                    sandbox_args=("--sandbox", "workspace-write"),
                ),
                "opencode": EngineConfig(
                    name="opencode",
                    bin="opencode-sandboxed.sh",
                    args_template=("run", "{spec}"),
                    full_access_args=("--no-sandbox",),
                    sandbox_args=(),
                ),
            },
            artifact=ArtifactConfig(
                enabled=False,
                out_template=str(root / "{run_id}.html"),
                report_template=str(root / "{run_id}-report.html"),
                index_out=root / "index.html",
            ),
        )

    def w12_findings(self, task_extra: dict[str, object], expect_files: list[str]) -> list[str]:
        task = self.task(expect_files=expect_files)
        task.update(task_extra)
        manifest = self.manifest([task])
        return [
            item
            for item in lint_manifest(manifest, config=self.w12_config())
            if "sandbox confines worker writes" in item
        ]

    def test_w12_sandboxed_deliverable_outside_taskdir_warns(self) -> None:
        # The cttc-idea-join-notif round-1 shape (2026-07-28): an adversarial
        # reviewer on sandboxed codex, told to deliver its report under
        # ~/.ringer/exports/, an opaque script check that never names the path
        # in the manifest. The worker wrote an 11KB review in its taskdir and
        # physically could not deliver it; two attempts, 251k tokens, both
        # failed identically.
        findings = self.w12_findings(
            {"engine": "codex", "check": "bash /work/check_review.sh"},
            ["/exports/cttc/review-r1.md"],
        )
        self.assertEqual(1, len(findings), findings)
        # A warning, not a blocker: the check-exports design is legitimate and
        # an opaque script MIGHT implement it — the author confirms, lint
        # cannot.
        self.assertFalse(findings[0].startswith("ERROR:"), findings[0])
        self.assertIn("/exports/cttc/review-r1.md", findings[0])

    def test_w12_check_naming_the_path_is_the_export_pattern(self) -> None:
        # Rounds 2/3 of the same review chain passed by having the CHECK do
        # the export — checks run unsandboxed. A check that names the path is
        # taken as that design.
        findings = self.w12_findings(
            {
                "engine": "codex",
                "check": (
                    "cp review.md /exports/cttc/review-r2.md && test -s /exports/cttc/review-r2.md "
                    "|| { echo 'FAIL: export missing'; exit 1; }"
                ),
            },
            ["/exports/cttc/review-r2.md"],
        )
        self.assertEqual([], findings)

    def test_w12_absolute_path_inside_the_taskdir_is_quiet(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workdir = (Path(temp_dir.name) / "work").resolve()
        manifest = Manifest.from_obj(
            {
                "run_name": "lint-test",
                "workdir": str(workdir),
                "max_parallel": 1,
                "tasks": [
                    {
                        **self.task(expect_files=[str(workdir / "one" / "report.md")]),
                        "engine": "codex",
                    }
                ],
            }
        )
        findings = [
            item
            for item in lint_manifest(manifest, config=self.w12_config())
            if "sandbox confines worker writes" in item
        ]
        self.assertEqual([], findings)

    def test_w12_full_access_task_is_quiet(self) -> None:
        findings = self.w12_findings(
            {"engine": "codex", "check": "bash /work/check.sh", "full_access": True},
            ["/exports/cttc/review-r1.md"],
        )
        self.assertEqual([], findings)

    def test_w12_unconfined_engine_is_quiet(self) -> None:
        # opencode's confinement lives in its wrapper script, invisible from
        # config. Warning on a guess trains authors to ignore the warning.
        findings = self.w12_findings(
            {"engine": "opencode", "check": "bash /work/check.sh"},
            ["/exports/cttc/review-r1.md"],
        )
        self.assertEqual([], findings)

    def test_w12_no_config_is_silent(self) -> None:
        # Plain `ringer.py lint` may run with no loadable config; the rule
        # degrades to silence rather than blocking lint entirely.
        task = self.task(expect_files=["/exports/cttc/review-r1.md"])
        task.update({"engine": "codex", "check": "bash /work/check_review.sh"})
        manifest = self.manifest([task])
        findings = [
            item for item in lint_manifest(manifest) if "sandbox confines worker writes" in item
        ]
        self.assertEqual([], findings)

    def test_w12_tilde_declaration_matches_expanded_form_in_check(self) -> None:
        # Verifier expands ~ in expect_files, and a check exporting the file
        # names the EXPANDED path ($HOME already substituted). The suppression
        # must compare expanded forms or this legitimate pairing false-warns.
        home_dir = tempfile.TemporaryDirectory()
        self.addCleanup(home_dir.cleanup)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = home_dir.name
        try:
            expanded = Path(home_dir.name) / "exports" / "review-r2.md"
            findings = self.w12_findings(
                {
                    "engine": "codex",
                    "check": (
                        f"cp review.md {expanded} && test -s {expanded} "
                        "|| { echo 'FAIL: export missing'; exit 1; }"
                    ),
                },
                ["~/exports/review-r2.md"],
            )
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
        self.assertEqual([], findings)

    def test_w12_tilde_deliverable_counts_as_outside(self) -> None:
        # The real incident declared the path under ~/.ringer/exports/ — a
        # tilde form must warn exactly like its expanded absolute form.
        findings = self.w12_findings(
            {"engine": "codex", "check": "bash /work/check_review.sh"},
            ["~/ringer-exports/review-r1.md"],
        )
        self.assertEqual(1, len(findings), findings)

    def test_templates_are_clean(self) -> None:
        # Every kit ships one or more manifest skeletons (manifest.json plus
        # optional manifest-round*.json for multi-round kits).
        template_paths = sorted((ROOT / "templates").glob("*/manifest*.json"))
        self.assertTrue(template_paths, "expected templates/*/manifest*.json files to exist")
        for path in template_paths:
            with self.subTest(template=path.name):
                manifest = Manifest.from_path(path)
                findings = lint_manifest(manifest)
                self.assertEqual([], findings, f"{path} should lint clean, got: {findings}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
