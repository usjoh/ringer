#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
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
    EvalConfig,
    artifact_library_path,
    artifact_live_path,
    model_judgment_notes,
    parse_model_notes_sections,
    run_models_command,
)


def catalog_model(model_id: str, *, prompt: str, completion: str, ctx: int = 64000) -> dict[str, object]:
    return {
        "id": model_id,
        "name": model_id,
        "context_length": ctx,
        "architecture": {"modality": "text->text"},
        "pricing": {"prompt": prompt, "completion": completion},
    }


def attempt(
    *,
    run_id: str,
    task_key: str,
    model: str,
    task_type: str,
    verdict: str,
    retry: bool,
    logged_at: str,
    tokens: int | None = 1000,
    engine: str = "opencode",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "task_key": task_key,
        "worker_engine": engine,
        "model": model,
        "task_type": task_type,
        "verdict": verdict,
        "retry": retry,
        "worker_tokens": tokens,
        "duration_ms": 100,
        "logged_at": logged_at,
    }


class ScoreboardPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_env = os.environ.copy()
        self.addCleanup(self.restore_env)
        os.environ["HOME"] = str(self.root / "home")
        os.environ["RINGER_HOME"] = str(self.root / "ringer-home")
        self.log_path = self.root / "eval.jsonl"
        self.catalog_path = self.root / "catalog.json"
        self.notes_path = self.root / "MODEL-NOTES.md"
        self.config = AppConfig(
            path=None,
            identity_default=None,
            state_dir=self.root / "state",
            dashboard_port_base=8787,
            hud_port=8700,
            hud_app_path=None,
            allow_full_access=False,
            eval=EvalConfig(backend="jsonl", jsonl_path=self.log_path),
            engines={},
            artifact=ArtifactConfig(
                enabled=True,
                out_template=str(self.root / "live.html"),
                report_template=str(self.root / "report.html"),
                index_out=self.root / "index.html",
            ),
        )
        self.write_fixtures()

    def restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)

    def write_fixtures(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(20):
            run_id = f"proven-{index:02d}"
            task_key = "task"
            if index < 18:
                rows.append(
                    attempt(
                        run_id=run_id,
                        task_key=task_key,
                        model="openrouter/proven",
                        task_type="code-feature",
                        verdict="PASS",
                        retry=False,
                        logged_at=f"2026-07-06T10:{index:02d}:00+00:00",
                        tokens=2000,
                    )
                )
            else:
                rows.append(
                    attempt(
                        run_id=run_id,
                        task_key=task_key,
                        model="openrouter/proven",
                        task_type="code-feature",
                        verdict="FAIL",
                        retry=False,
                        logged_at=f"2026-07-06T10:{index:02d}:00+00:00",
                        tokens=2000,
                    )
                )
                rows.append(
                    attempt(
                        run_id=run_id,
                        task_key=task_key,
                        model="openrouter/proven",
                        task_type="code-feature",
                        verdict="PASS",
                        retry=True,
                        logged_at=f"2026-07-06T11:{index:02d}:00+00:00",
                        tokens=2200,
                    )
                )
        rows.append(
            attempt(
                run_id="probation-1",
                task_key="task",
                model="openrouter/probation",
                task_type="code-feature",
                verdict="PASS",
                retry=False,
                logged_at="2026-07-06T12:00:00+00:00",
                tokens=500,
            )
        )
        rows.append(
            attempt(
                run_id="free-1",
                task_key="task",
                model="openrouter/free:free",
                task_type="research",
                verdict="PASS",
                retry=False,
                logged_at="2026-07-06T12:10:00+00:00",
                tokens=700,
            )
        )
        rows.append(
            attempt(
                run_id="codex-1",
                task_key="task",
                model="",
                task_type="site-build",
                verdict="PASS",
                retry=False,
                logged_at="2026-07-06T12:20:00+00:00",
                tokens=None,
                engine="codex",
            )
        )
        self.log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot-json\n", encoding="utf-8")
        self.catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        catalog_model("openrouter/proven", prompt="0.000001", completion="0.000003"),
                        catalog_model("openrouter/probation", prompt="0.0000005", completion="0.000001"),
                        catalog_model("openrouter/free:free", prompt="0.000002", completion="0.000004", ctx=128000),
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.catalog_path.with_name("catalog.changes.jsonl").write_text(
            json.dumps({"ts": "2026-07-06T12:00:00+00:00", "kind": "went_free", "id": "openrouter/free:free"})
            + "\n",
            encoding="utf-8",
        )
        self.notes_path.write_text(
            """# Notes

## Proven lane (`openrouter/proven`)

- Non-dated setup line ignored by the parser.
- 2026-07-06 - steady code-feature performance across a larger sample.
  Continuation line kept with the dated bullet.

## codex

- 2026-07-06 - flat-plan site work; no token billing in the log.
""",
            encoding="utf-8",
        )

    def args(self, *, html: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            log=self.log_path,
            task_type=None,
            model=None,
            engine=None,
            since=None,
            explore=False,
            catalog_file=self.catalog_path,
            notes_file=self.notes_path,
            html=html,
            open=False,
            json=False,
        )

    def render_to(self, path: Path) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, run_models_command(self.config, self.args(html=str(path))))
        self.assertEqual(str(path.resolve()) + "\n", out.getvalue())
        return path.read_text(encoding="utf-8")

    def test_html_render_contains_models_tiers_counts_free_badge_and_notes(self) -> None:
        html = self.render_to(self.root / "scoreboard.html")

        self.assertIn("openrouter/proven", html)
        self.assertIn("openrouter/probation", html)
        self.assertIn("openrouter/free:free", html)
        self.assertIn("codex", html)
        self.assertIn("proven", html)
        self.assertIn("probation", html)
        self.assertIn("n=20", html)
        self.assertIn("FREE", html)
        self.assertIn("steady code-feature performance across a larger sample", html)
        self.assertIn("Continuation line kept with the dated bullet.", html)
        self.assertIn("no judgment notes yet", html)
        self.assertIn("included in plan", html)

    def test_evidence_floor_orders_probation_after_proven_model(self) -> None:
        html = self.render_to(self.root / "scoreboard.html")

        self.assertLess(html.index("openrouter/proven"), html.index("openrouter/probation"))
        self.assertIn("#1", html[: html.index("openrouter/proven")])

    def test_reused_task_identity_does_not_collapse_proven_evidence(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(20):
            rows.append(
                attempt(
                    run_id="shared-run",
                    task_key="shared-task",
                    model="openrouter/proven",
                    task_type="code-feature",
                    verdict="PASS" if index < 18 else "FAIL",
                    retry=False,
                    logged_at=f"2026-07-06T13:{index:02d}:00+00:00",
                    tokens=2000,
                )
            )
        rows.append(
            attempt(
                run_id="shared-run",
                task_key="shared-task",
                model="openrouter/probation",
                task_type="code-feature",
                verdict="PASS",
                retry=False,
                logged_at="2026-07-06T14:00:00+00:00",
                tokens=500,
            )
        )
        self.log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        html = self.render_to(self.root / "scoreboard.html")

        self.assertLess(html.index("openrouter/proven"), html.index("openrouter/probation"))
        self.assertIn("n=20", html)
        self.assertIn("proven", html[: html.index("openrouter/proven")])

    def test_html_path_does_not_write_artifact_library(self) -> None:
        html_path = self.root / "custom.html"
        self.render_to(html_path)

        self.assertTrue(html_path.exists())
        self.assertFalse(artifact_library_path(self.config.state_dir).exists())

    def test_html_without_path_writes_live_artifact_library_entry(self) -> None:
        out = io.StringIO()
        args = self.args(html="")
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, run_models_command(self.config, args))

        live_path = artifact_live_path(self.config.state_dir, "model-scoreboard")
        self.assertEqual(str(live_path.resolve()) + "\n", out.getvalue())
        self.assertTrue(live_path.exists())
        library = json.loads(artifact_library_path(self.config.state_dir).read_text(encoding="utf-8"))
        self.assertIn("model-scoreboard", library["artifacts"])

    def test_html_is_self_contained_with_no_external_resource_loads(self) -> None:
        html = self.render_to(self.root / "scoreboard.html")

        self.assertNotRegex(html, r"""(?:src|href)=["']https?://""")
        self.assertNotRegex(html, r"""@import\s+["']https?://""")
        self.assertNotIn("<script", html.lower())

    def test_notes_section_parser_matches_heading_and_missing_section_falls_back(self) -> None:
        sections = parse_model_notes_sections(self.notes_path)

        proven = model_judgment_notes("openrouter/proven", sections)
        self.assertEqual(1, len(proven))
        self.assertIn("steady code-feature performance", proven[0])
        self.assertIn("Continuation line kept", proven[0])
        self.assertEqual([], model_judgment_notes("openrouter/missing", sections))


if __name__ == "__main__":
    unittest.main(verbosity=2)
