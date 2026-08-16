#!/usr/bin/env python3
"""The OpenCode wrapper must give every sandboxed worker a PRIVATE session store.

OpenCode keeps its session state in one SQLite file under `$XDG_DATA_HOME`.
Shared between concurrent workers it deadlocks — the losers die with "database
is locked" before a model runs, and Ringer records that as an ordinary FAIL, so
the scoreboard blames a model that never executed.

These tests drive the real wrapper with a stub `opencode` on PATH, so they
assert what the worker actually receives rather than what the script appears
to say.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "engines" / "opencode-sandboxed.sh"

# The wrapper sleeps up to ~4s to spread the startup burst; the stub itself is
# instant, so this only has to clear that.
WRAPPER_TIMEOUT_S = 30


def stub_opencode(bin_dir: Path) -> None:
    """A fake `opencode` that reports the environment it was handed."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "opencode"
    stub.write_text(
        "#!/bin/bash\n"
        "data_home=\"${XDG_DATA_HOME:-}\"\n"
        "auth=\"$data_home/opencode/auth.json\"\n"
        "printf '{\"xdg_data_home\": \"%s\", \"auth_present\": %s, "
        "\"auth_is_symlink\": %s, \"db_writable\": %s}\\n' \\\n"
        "  \"$data_home\" \\\n"
        "  \"$([ -f \"$auth\" ] && echo true || echo false)\" \\\n"
        "  \"$([ -L \"$auth\" ] && echo true || echo false)\" \\\n"
        "  \"$(touch \"$data_home/opencode/opencode.db\" 2>/dev/null && echo true || echo false)\"\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


@unittest.skipUnless(sys.platform == "darwin", "wrapper's sandbox path is macOS-only")
@unittest.skipUnless(
    Path("/usr/bin/sandbox-exec").is_file(), "sandbox-exec not available"
)
class OpenCodeWrapperIsolationTests(unittest.TestCase):
    def run_wrapper(self, home: Path, taskdir: Path) -> dict[str, object]:
        bin_dir = home / "bin"
        stub_opencode(bin_dir)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        proc = subprocess.run(
            ["/bin/bash", str(WRAPPER), str(taskdir)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=WRAPPER_TIMEOUT_S,
            check=False,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def seeded_home(self, root: Path, *, with_auth: bool = True) -> Path:
        home = root / "home"
        share = home / ".local" / "share" / "opencode"
        share.mkdir(parents=True)
        if with_auth:
            auth = share / "auth.json"
            auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "x"}}))
            auth.chmod(0o600)
        return home

    def test_worker_gets_a_private_data_home_not_the_shared_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = self.seeded_home(root)
            taskdir = root / "task"
            taskdir.mkdir()

            result = self.run_wrapper(home, taskdir)

            data_home = str(result["xdg_data_home"])
            self.assertTrue(data_home, "wrapper left XDG_DATA_HOME unset")
            self.assertNotIn(
                str(home / ".local" / "share"),
                data_home,
                "worker was pointed at the SHARED opencode data dir",
            )
            # The store must be writable, or the worker dies a different way.
            self.assertTrue(result["db_writable"], "private session DB is not writable")

    def test_two_workers_never_share_a_data_home(self) -> None:
        # The whole point: concurrent workers must not touch one DB file.
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = self.seeded_home(root)
            first = root / "task-a"
            second = root / "task-b"
            first.mkdir()
            second.mkdir()

            one = self.run_wrapper(home, first)
            two = self.run_wrapper(home, second)

            self.assertNotEqual(one["xdg_data_home"], two["xdg_data_home"])

    def test_credentials_are_seeded_by_copy_not_symlink(self) -> None:
        # auth.json lives inside the directory we relocate, so an unseeded
        # worker has no provider auth at all. A symlink would defeat the fix:
        # it resolves back to the shared file the private store exists to
        # avoid, and the profile denies writes outside SCRATCH.
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = self.seeded_home(root)
            taskdir = root / "task"
            taskdir.mkdir()

            result = self.run_wrapper(home, taskdir)

            self.assertTrue(result["auth_present"], "worker got no credentials")
            self.assertFalse(
                result["auth_is_symlink"],
                "credentials were symlinked back to the shared store",
            )

    def test_missing_credentials_do_not_abort_the_worker(self) -> None:
        # A machine that authenticates some other way must still run: the seed
        # is best-effort, not a precondition. `set -euo pipefail` makes an
        # unguarded cp fatal, so this pins the guard.
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = self.seeded_home(root, with_auth=False)
            taskdir = root / "task"
            taskdir.mkdir()

            result = self.run_wrapper(home, taskdir)

            self.assertFalse(result["auth_present"])
            self.assertTrue(result["db_writable"])


if __name__ == "__main__":
    unittest.main()
