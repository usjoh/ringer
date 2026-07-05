#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import ArtifactRenderer, render_final_report_html, render_status_html  # noqa: E402


class PlainEnglishArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.renderer = ArtifactRenderer(Path(self.tmp.name) / "artifacts" / "run.html")

    def task(
        self,
        key: str,
        status: str,
        *,
        attempts: int = 0,
        elapsed_s: float = 0,
        check_timed_out: bool = False,
    ) -> dict[str, object]:
        return {
            "key": key,
            "status": status,
            "attempts": attempts,
            "elapsed_s": elapsed_s,
            "check": "echo ok",
            "spec_short": "do the work",
            "check_timed_out": check_timed_out,
        }

    def state(self, tasks: list[dict[str, object]], *, finished: bool = False, elapsed_s: float = 360) -> dict[str, object]:
        pass_n = sum(1 for task in tasks if task["status"] == "pass")
        fail_n = sum(1 for task in tasks if task["status"] == "fail")
        running_n = sum(1 for task in tasks if task["status"] in {"running", "verifying", "retrying"})
        return {
            "run_id": "run-123",
            "run_name": "Plain English Run",
            "identity": "claude-code-mbp",
            "state": "finished" if finished else "live",
            "started_at": "2026-07-05T00:00:00+00:00",
            "elapsed_s": elapsed_s,
            "max_parallel": 2,
            "finished": finished,
            "report_ready": False,
            "report_path": None,
            "totals": {
                "pass": pass_n,
                "fail": fail_n,
                "done": pass_n + fail_n,
                "running": running_n,
                "tokens": 0,
            },
            "tasks": tasks,
        }

    def plain_sections(self, html: str, first_heading_id: str = "right-now-heading") -> str:
        match = re.search(
            rf'<section aria-labelledby="{first_heading_id}">(?P<first>.*?)</section>\s*'
            r'<section aria-labelledby="status-updates-heading">(?P<updates>.*?)</section>',
            html,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        assert match is not None
        return match.group("first") + match.group("updates")

    def test_live_briefing_counts_mixed_state(self) -> None:
        html = render_status_html(
            self.state(
                [
                    self.task("A-pass", "pass", attempts=1),
                    self.task("B-fail", "fail", attempts=2),
                    self.task("C-running", "running", attempts=1),
                    self.task("D-waiting", "queued"),
                ]
            ),
            renderer=self.renderer,
        )

        self.assertIn("Right now", html)
        self.assertIn("Ringer is working on 4 tasks for claude-code-mbp.", html)
        self.assertIn(
            "1 passed its check, 1 failed its check, 1 is running, 1 is waiting",
            html,
        )

    def test_status_change_adds_timestamped_update(self) -> None:
        render_status_html(self.state([self.task("A-mock-engine", "queued")]), renderer=self.renderer)

        html = render_status_html(
            self.state([self.task("A-mock-engine", "running", attempts=1)]),
            renderer=self.renderer,
        )

        self.assertRegex(html, r"<time>\d{2}:\d{2}:\d{2}</time>A-mock-engine started")

    def test_retry_and_second_try_updates(self) -> None:
        render_status_html(
            self.state([self.task("C-nudge-hooks", "running", attempts=1, elapsed_s=10)]),
            renderer=self.renderer,
        )
        retry_html = render_status_html(
            self.state([self.task("C-nudge-hooks", "retrying", attempts=1, elapsed_s=20)]),
            renderer=self.renderer,
        )

        self.assertIn(
            "C-nudge-hooks failed its check — sending it back to try again",
            retry_html,
        )

        pass_html = render_status_html(
            self.state([self.task("C-nudge-hooks", "pass", attempts=2, elapsed_s=35)]),
            renderer=self.renderer,
        )

        self.assertIn("C-nudge-hooks passed on the second try", pass_html)
        self.assertIn(
            "C-nudge-hooks failed its check — sending it back to try again",
            pass_html,
        )

    def test_briefing_and_updates_do_not_use_banned_jargon(self) -> None:
        html = render_status_html(
            self.state(
                [
                    self.task("B-mock-engine", "pass", attempts=1, elapsed_s=330),
                    self.task("C-nudge-hooks", "retrying", attempts=1, elapsed_s=22),
                ]
            ),
            renderer=self.renderer,
        )
        plain_html = self.plain_sections(html).lower()

        for banned in ("manifest", "worktree", "orchestrator", "rc=", "exit code"):
            self.assertNotIn(banned, plain_html)

    def test_technical_table_is_inside_details(self) -> None:
        html = render_status_html(
            self.state([self.task("B-mock-engine", "pass", attempts=1, elapsed_s=330)]),
            renderer=self.renderer,
        )

        self.assertIn('<details class="technical-detail">', html)
        self.assertIn("<summary>Technical detail</summary>", html)
        details_start = html.index('<details class="technical-detail">')
        table_start = html.index("<table>")
        details_end = html.index("</details>", details_start)
        self.assertGreater(table_start, details_start)
        self.assertLess(table_start, details_end)
        self.assertIn("rc=", html[details_start:details_end])

    def test_final_report_all_pass_briefing(self) -> None:
        html = render_final_report_html(
            self.state(
                [
                    self.task("A", "pass", attempts=1),
                    self.task("B", "pass", attempts=1),
                    self.task("C", "pass", attempts=1),
                    self.task("D", "pass", attempts=1),
                ],
                finished=True,
                elapsed_s=330,
            ),
            renderer=self.renderer,
        )

        self.assertIn("What happened", html)
        self.assertIn(
            "Ringer finished 4 tasks in 5m 30s. All 4 passed their checks.",
            html,
        )
        self.assertIn('<details class="technical-detail" open>', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
