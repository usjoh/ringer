#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"ringer requires Python 3.11+ (tomllib); found {sys.version.split()[0]} at {sys.executable}"
    )

import tempfile
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


TOOL_NAME = "ringer"
STATE_DIR_NAME = ".ringer"
ENV_VAR_PREFIX = "RINGER"

CONFIG_DIR_NAME = TOOL_NAME
CONFIG_FILE_NAME = "config.toml"
DEFAULT_ENGINE_NAME = "codex"
DEFAULT_TIMEOUT_S = 900
CHECK_TIMEOUT_S = 60
DEFAULT_DASHBOARD_PORT_BASE = 8787
DEFAULT_TOKEN_REGEX = r"tokens\s+used\s*:?\s*([0-9][0-9,]*)"
ACTIVITY_TAIL_BYTES = 2048
ACTIVITY_TEXT_LIMIT = 80
SHEPHERD_MODEL = f"none ({TOOL_NAME}.py)"
VERIFY_METHOD = "executed-check"
DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard" / "dashboard.html"
MINIMAL_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>ringer dashboard</title></head>
<body style="font-family: system-ui, sans-serif; background:#080a0f; color:#eef4ff;">
<main id="app">dashboard/dashboard.html is missing</main>
<script>
function update(states) {
  document.getElementById("app").textContent = JSON.stringify(states, null, 2);
}
</script>
</body>
</html>
"""


@dataclass(frozen=True)
class EngineConfig:
    name: str
    bin: str
    args_template: tuple[str, ...]
    full_access_args: tuple[str, ...]
    sandbox_args: tuple[str, ...]
    token_regex: str | None = DEFAULT_TOKEN_REGEX

    @property
    def process_name(self) -> str:
        return Path(self.bin).name or self.name


@dataclass(frozen=True)
class PostgresEvalConfig:
    env_file: Path


@dataclass(frozen=True)
class EvalConfig:
    backend: str
    jsonl_path: Path
    postgres: PostgresEvalConfig | None = None


@dataclass(frozen=True)
class AppConfig:
    path: Path | None
    identity_default: str | None
    state_dir: Path
    dashboard_port_base: int
    hud_app_path: Path | None
    allow_full_access: bool
    eval: EvalConfig
    engines: dict[str, EngineConfig]

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or env_config_path() or default_config_path()
        explicit = path is not None or env_config_path() is not None
        data: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open("rb") as fh:
                loaded = tomllib.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a TOML table")
            data = loaded
        elif explicit:
            raise ValueError(f"config file not found: {config_path}")

        state_dir = expand_path(data.get("state_dir"), default_state_dir())
        dashboard_port_base = int(data.get("dashboard_port_base", DEFAULT_DASHBOARD_PORT_BASE))
        if dashboard_port_base <= 0:
            raise ValueError("dashboard_port_base must be positive")
        identity_default = optional_string(data.get("identity_default"))
        hud_app_path = optional_path(data.get("hud_app_path"))
        allow_full_access = bool(data.get("allow_full_access", False))
        eval_config = load_eval_config(data.get("eval"), state_dir)
        engines = load_engines(data.get("engines"))
        return cls(
            path=config_path if config_path.exists() else None,
            identity_default=identity_default,
            state_dir=state_dir,
            dashboard_port_base=dashboard_port_base,
            hud_app_path=hud_app_path,
            allow_full_access=allow_full_access,
            eval=eval_config,
            engines=engines,
        )


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(base).expanduser() if base else Path.home() / ".config"
    return config_home / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def env_config_path() -> Path | None:
    value = os.environ.get(f"{ENV_VAR_PREFIX}_CONFIG")
    if not value or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def default_state_dir() -> Path:
    return Path.home() / STATE_DIR_NAME


def expand_path(value: Any, default: Path) -> Path:
    if value is None:
        return default.expanduser().resolve()
    return Path(str(value)).expanduser().resolve()


def optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_string_tuple(value: Any, *, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return tuple(str(item) for item in value)


def built_in_codex_engine() -> EngineConfig:
    resolved = shutil.which(DEFAULT_ENGINE_NAME) or DEFAULT_ENGINE_NAME
    return EngineConfig(
        name=DEFAULT_ENGINE_NAME,
        bin=resolved,
        args_template=(
            "exec",
            "--skip-git-repo-check",
            "{access_args}",
            "-C",
            "{taskdir}",
            "{spec}",
        ),
        full_access_args=("--dangerously-bypass-approvals-and-sandbox",),
        sandbox_args=("--sandbox", "workspace-write"),
        token_regex=DEFAULT_TOKEN_REGEX,
    )


def load_eval_config(raw: Any, state_dir: Path) -> EvalConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("eval must be a TOML table")
    backend = str(raw.get("backend", "jsonl")).strip().lower()
    if backend not in {"jsonl", "postgres"}:
        raise ValueError("eval.backend must be 'jsonl' or 'postgres'")
    jsonl_path = expand_path(raw.get("jsonl_path"), state_dir / "runs.jsonl")
    postgres: PostgresEvalConfig | None = None
    postgres_raw = raw.get("postgres")
    if postgres_raw is not None:
        if not isinstance(postgres_raw, dict):
            raise ValueError("eval.postgres must be a TOML table")
        env_file_raw = optional_string(postgres_raw.get("env_file"))
        if env_file_raw is None:
            raise ValueError("eval.postgres.env_file is required")
        env_file = Path(env_file_raw).expanduser().resolve()
        postgres = PostgresEvalConfig(env_file=env_file)
    if backend == "postgres" and postgres is None:
        raise ValueError("eval.backend='postgres' requires [eval.postgres].env_file")
    return EvalConfig(backend=backend, jsonl_path=jsonl_path, postgres=postgres)


def load_engines(raw: Any) -> dict[str, EngineConfig]:
    engines: dict[str, EngineConfig] = {DEFAULT_ENGINE_NAME: built_in_codex_engine()}
    if raw is None:
        return engines
    if not isinstance(raw, dict):
        raise ValueError("engines must be a TOML table")
    for name, section in raw.items():
        if not isinstance(section, dict):
            raise ValueError(f"engines.{name} must be a TOML table")
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("engine name must not be empty")
        base = engines.get(clean_name)
        default_bin = base.bin if base else clean_name
        bin_path = str(section.get("bin", default_bin)).strip()
        if not bin_path:
            raise ValueError(f"engines.{clean_name}.bin must not be empty")
        args_template = as_string_tuple(
            section.get("args_template", list(base.args_template) if base else None),
            key=f"engines.{clean_name}.args_template",
        )
        if not args_template:
            raise ValueError(f"engines.{clean_name}.args_template must not be empty")
        full_access_args = as_string_tuple(
            section.get("full_access_args", list(base.full_access_args) if base else []),
            key=f"engines.{clean_name}.full_access_args",
        )
        sandbox_args = as_string_tuple(
            section.get("sandbox_args", list(base.sandbox_args) if base else []),
            key=f"engines.{clean_name}.sandbox_args",
        )
        token_regex = optional_string(section.get("token_regex"))
        if token_regex is None and base is not None:
            token_regex = base.token_regex
        if token_regex:
            try:
                re.compile(token_regex, flags=re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"engines.{clean_name}.token_regex is invalid: {exc}") from exc
        engines[clean_name] = EngineConfig(
            name=clean_name,
            bin=bin_path,
            args_template=args_template,
            full_access_args=full_access_args,
            sandbox_args=sandbox_args,
            token_regex=token_regex,
        )
    return engines


@dataclass(frozen=True)
class TaskSpec:
    key: str
    spec: str
    check: str
    engine: str = DEFAULT_ENGINE_NAME
    expect_files: tuple[str, ...] = ()
    timeout_s: int = DEFAULT_TIMEOUT_S
    full_access: bool = False

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> "TaskSpec":
        key = str(obj.get("key", "")).strip()
        spec = str(obj.get("spec", ""))
        check = str(obj.get("check", ""))
        if not key:
            raise ValueError("task key is required")
        if not spec:
            raise ValueError(f"task {key}: spec is required")
        if not check:
            raise ValueError(f"task {key}: check is required")
        expect_files = obj.get("expect_files", [])
        if not isinstance(expect_files, list):
            raise ValueError(f"task {key}: expect_files must be a list")
        engine = str(obj.get("engine", DEFAULT_ENGINE_NAME)).strip()
        if not engine:
            raise ValueError(f"task {key}: engine must not be empty")
        timeout_s = int(obj.get("timeout_s", DEFAULT_TIMEOUT_S))
        if timeout_s <= 0:
            raise ValueError(f"task {key}: timeout_s must be positive")
        return cls(
            key=key,
            spec=spec,
            check=check,
            engine=engine,
            expect_files=tuple(str(item) for item in expect_files),
            timeout_s=timeout_s,
            full_access=bool(obj.get("full_access", False)),
        )


@dataclass(frozen=True)
class Manifest:
    run_name: str
    workdir: Path
    max_parallel: int
    worktrees: bool
    repo: Path | None
    tasks: tuple[TaskSpec, ...]
    source_path: Path | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Manifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest root must be a JSON object")
        manifest = cls.from_obj(data)
        return cls(
            run_name=manifest.run_name,
            workdir=manifest.workdir,
            max_parallel=manifest.max_parallel,
            worktrees=manifest.worktrees,
            repo=manifest.repo,
            tasks=manifest.tasks,
            source_path=path,
        )

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> "Manifest":
        run_name = str(obj.get("run_name", "")).strip()
        if not run_name:
            raise ValueError("run_name is required")
        workdir_raw = obj.get("workdir")
        if not workdir_raw:
            raise ValueError("workdir is required")
        workdir = Path(str(workdir_raw)).expanduser().resolve()
        max_parallel = int(obj.get("max_parallel", 1))
        if max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        repo_raw = obj.get("repo")
        repo = Path(str(repo_raw)).expanduser().resolve() if repo_raw else None
        tasks_raw = obj.get("tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ValueError("tasks must be a non-empty list")
        tasks = tuple(TaskSpec.from_obj(task) for task in tasks_raw)
        keys = [task.key for task in tasks]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate task keys: {', '.join(duplicates)}")
        worktrees = bool(obj.get("worktrees", False))
        if worktrees:
            reserved_logs_dir = (workdir / "logs").resolve()
            collisions = []
            for task in tasks:
                taskdir = (workdir / task.key).resolve()
                if taskdir == reserved_logs_dir or reserved_logs_dir in taskdir.parents:
                    collisions.append(task.key)
            if collisions:
                raise ValueError(
                    "task key(s) collide with reserved worktree logs directory "
                    f"'logs': {', '.join(collisions)}"
                )
        return cls(
            run_name=run_name,
            workdir=workdir,
            max_parallel=max_parallel,
            worktrees=worktrees,
            repo=repo,
            tasks=tasks,
        )

    def with_max_parallel(self, value: int | None) -> "Manifest":
        if value is None:
            return self
        if value <= 0:
            raise ValueError("--max-parallel must be positive")
        return Manifest(
            run_name=self.run_name,
            workdir=self.workdir,
            max_parallel=value,
            worktrees=self.worktrees,
            repo=self.repo,
            tasks=self.tasks,
            source_path=self.source_path,
        )


@dataclass
class TaskRuntime:
    task: TaskSpec
    taskdir: Path
    log_path: Path
    status: str = "queued"
    spec_short: str = ""
    attempts: int = 0
    started_at_monotonic: float | None = None
    ended_at_monotonic: float | None = None
    worker_pid: int | None = None
    tokens: int | None = None
    final_verdict: str | None = None

    def elapsed_s(self, now: float) -> float:
        if self.started_at_monotonic is None:
            return 0.0
        end = self.ended_at_monotonic if self.ended_at_monotonic is not None else now
        return max(0.0, end - self.started_at_monotonic)


@dataclass(frozen=True)
class WorkerResult:
    returncode: int | None
    timed_out: bool
    tokens: int | None
    error: str | None = None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    check_returncode: int | None
    check_timed_out: bool
    raw_output_excerpt: str
    missing_files: tuple[str, ...] = ()


class ProcessTree:
    @staticmethod
    def read() -> tuple[dict[int, list[int]], dict[int, str]]:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,args="],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            return {}, {}
        children: dict[int, list[int]] = {}
        commands: dict[int, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            command = parts[2] if len(parts) > 2 else ""
            children.setdefault(ppid, []).append(pid)
            commands[pid] = command
        return children, commands

    @staticmethod
    def count_named_descendants(
        root_pid: int | None,
        children: dict[int, list[int]],
        commands: dict[int, str],
        process_name: str,
    ) -> int:
        if root_pid is None:
            return 0
        needle = process_name.lower()
        count = 0
        stack = list(children.get(root_pid, []))
        while stack:
            pid = stack.pop()
            command = commands.get(pid, "")
            if command:
                executable = Path(command.split()[0]).name.lower()
                if needle and needle in executable:
                    count += 1
            stack.extend(children.get(pid, []))
        return count


class StateWriter:
    def __init__(
        self,
        run_id: str,
        run_name: str,
        identity: str,
        state_dir: Path,
        engines: dict[str, EngineConfig],
        started_at: datetime,
        runtimes: list[TaskRuntime],
        lock: threading.RLock,
        path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_name = run_name
        self.identity = identity
        self.engines = engines
        self.started_at = started_at
        self.runtimes = runtimes
        self.lock = lock
        self.path = path or (state_dir / "runs" / f"{run_id}.json")
        self.pid = os.getpid()
        self.port: int | None = None
        self.finished = False
        self.summary: dict[str, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.flush()
        self._thread = threading.Thread(target=self._loop, name="ringer-state-writer", daemon=True)
        self._thread.start()

    def set_port(self, port: int | None) -> None:
        self.port = port
        self.flush()

    def finish(self) -> None:
        self.finished = True
        self.summary = self.build_summary()
        self.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.flush()

    def flush(self) -> None:
        state = self.snapshot()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        children, commands = ProcessTree.read()
        with self.lock:
            tasks = []
            for runtime in self.runtimes:
                log_tail = tail_lines(runtime.log_path, line_count=3)
                engine = self.engines.get(runtime.task.engine)
                process_name = engine.process_name if engine else runtime.task.engine
                tasks.append(
                    {
                        "key": runtime.task.key,
                        "status": runtime.status,
                        "engine": runtime.task.engine,
                        "spec": runtime.task.spec,
                        "spec_short": runtime.spec_short,
                        "activity": worker_activity(runtime.log_path, log_tail),
                        "elapsed_s": round(runtime.elapsed_s(now), 1),
                        "tokens": runtime.tokens,
                        "attempts": runtime.attempts,
                        "children": ProcessTree.count_named_descendants(
                            runtime.worker_pid, children, commands, process_name
                        ),
                        "log_tail": log_tail,
                    }
                )
            pass_count = sum(1 for item in tasks if item["status"] == "pass")
            fail_count = sum(1 for item in tasks if item["status"] == "fail")
            running_count = sum(
                1 for item in tasks if item["status"] in {"running", "verifying", "retrying"}
            )
            totals = {
                "running": running_count,
                "done": pass_count + fail_count,
                "pass": pass_count,
                "fail": fail_count,
                "tokens": sum(int(item["tokens"] or 0) for item in tasks),
            }
            return {
                "run_id": self.run_id,
                "run_name": self.run_name,
                "identity": self.identity,
                "state": "finished" if self.finished else "live",
                "pid": self.pid,
                "port": self.port,
                "finished": self.finished,
                "summary": self.summary if self.finished else None,
                "started_at": self.started_at.isoformat(),
                "elapsed_s": max((float(item["elapsed_s"]) for item in tasks), default=0.0),
                "tasks": tasks,
                "totals": totals,
                "pass": totals["pass"],
                "fail": totals["fail"],
                "tokens": totals["tokens"],
            }

    def build_summary(self) -> dict[str, int]:
        with self.lock:
            return {
                "pass": sum(1 for runtime in self.runtimes if runtime.status == "pass"),
                "fail": sum(1 for runtime in self.runtimes if runtime.status == "fail"),
                "tokens": sum(int(runtime.tokens or 0) for runtime in self.runtimes),
            }

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.flush()
            except Exception as exc:
                print(f"state writer error: {exc}", file=sys.stderr)


class Dashboard:
    def __init__(
        self,
        state_path: Path,
        preferred_port: int,
        hud_app_path: Path | None = None,
        force_browser: bool = False,
    ) -> None:
        self.state_path = state_path
        self.preferred_port = preferred_port
        self.hud_app_path = hud_app_path
        self.force_browser = force_browser
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> int:
        state_path = self.state_path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    body = read_dashboard_html().encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/state.json":
                    try:
                        body = state_path.read_bytes()
                    except FileNotFoundError:
                        body = b'{"run_name":"ringer","identity":"unknown","started_at":"","tasks":[],"totals":{"running":0,"done":0,"pass":0,"fail":0,"tokens":0}}'
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        last_error: OSError | None = None
        for port in range(self.preferred_port, self.preferred_port + 50):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            except OSError as exc:
                last_error = exc
                continue
            self.port = port
            break
        if self.httpd is None or self.port is None:
            raise RuntimeError(f"could not start dashboard: {last_error}")
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ringer-dashboard", daemon=True)
        self.thread.start()
        url = f"http://localhost:{self.port}"
        try:
            if not self.force_browser and self.hud_app_path is not None and self.hud_app_path.exists():
                subprocess.Popen(
                    ["open", "-a", str(self.hud_app_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # flush: under a pipe this line is block-buffered and only appears at
        # process exit, making live runs look dashboard-less (MBP field report).
        print(f"Dashboard: {url}", flush=True)
        return self.port

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


class EvalLogger:
    def __init__(self, config: EvalConfig) -> None:
        self.config = config
        self._conn: Any | None = None
        self._fallback_path = config.jsonl_path
        self._fallback_reason: str | None = None
        if config.backend == "postgres":
            self._connect()

    def log_attempt(self, row: dict[str, Any]) -> None:
        db_row = dict(row)
        if self._conn is not None:
            try:
                self._conn.execute(
                    """
                    INSERT INTO swarm_runs (
                        run_id, pattern, task_key, spec, worker_engine, shepherd_model,
                        verify_method, verdict, duration_ms, worker_tokens, notes, orchestrator
                    )
                    VALUES (
                        %(run_id)s, %(pattern)s, %(task_key)s, %(spec)s, %(worker_engine)s,
                        %(shepherd_model)s, %(verify_method)s, %(verdict)s, %(duration_ms)s,
                        %(worker_tokens)s, %(notes)s, %(orchestrator)s
                    )
                    """,
                    db_row,
                )
                return
            except Exception as exc:
                self._fallback_reason = f"Supabase insert failed: {exc}"
                self._close_conn()
        self._write_jsonl(db_row)

    def close(self) -> None:
        self._close_conn()

    def _connect(self) -> None:
        try:
            import psycopg  # type: ignore[import-not-found]
        except Exception as exc:
            self._fallback_reason = f"psycopg import failed: {exc}"
            return
        if self.config.postgres is None:
            self._fallback_reason = "postgres eval config missing"
            return
        creds = parse_env_file(self.config.postgres.env_file)
        required = [
            "SUPABASE_DB_HOST",
            "SUPABASE_DB_PORT",
            "SUPABASE_DB_USER",
            "SUPABASE_DB_PASSWORD",
            "SUPABASE_DB_NAME",
        ]
        missing = [key for key in required if not creds.get(key)]
        if missing:
            self._fallback_reason = f"missing Supabase env keys: {', '.join(missing)}"
            return
        try:
            self._conn = psycopg.connect(
                host=creds["SUPABASE_DB_HOST"],
                port=int(creds["SUPABASE_DB_PORT"]),
                user=creds["SUPABASE_DB_USER"],
                password=creds["SUPABASE_DB_PASSWORD"],
                dbname=creds["SUPABASE_DB_NAME"],
                autocommit=True,
                connect_timeout=5,
            )
        except Exception as exc:
            self._fallback_reason = f"Supabase connect failed: {exc}"

    def _write_jsonl(self, row: dict[str, Any]) -> None:
        self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(row)
        payload["logged_at"] = datetime.now(timezone.utc).isoformat()
        payload["log_sink"] = "jsonl"
        payload["fallback_reason"] = self._fallback_reason
        with self._fallback_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class Verifier:
    async def verify(self, task: TaskSpec, taskdir: Path) -> VerifyResult:
        missing_files = tuple(
            rel for rel in task.expect_files if not self._is_nonempty_file(taskdir / rel)
        )
        check_returncode, check_timed_out, output = await self._run_check(task.check, taskdir)
        ok = not missing_files and not check_timed_out and check_returncode == 0
        if missing_files:
            missing_message = f"[ringer] missing expected files: {', '.join(missing_files)}"
            output = f"{missing_message}\n{output}" if output.strip() else missing_message
        elif not check_timed_out and check_returncode != 0 and not output.strip():
            # A silent failing check wastes the retry (no failure context to
            # inject) and blinds the eval row. Say so, in both places.
            output = (
                f"[ringer] check failed silently (exit {check_returncode}, no output). "
                "Prefer checks that print WHY they fail — the retry prompt and the "
                "eval log both depend on it."
            )
        return VerifyResult(
            ok=ok,
            check_returncode=check_returncode,
            check_timed_out=check_timed_out,
            raw_output_excerpt=output[:2000],
            missing_files=missing_files,
        )

    @staticmethod
    def _is_nonempty_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    async def _run_check(command: str, cwd: Path) -> tuple[int | None, bool, str]:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            timed_out = True
            terminate_process_group(proc)
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                kill_process_group(proc)
                stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        if timed_out:
            output += f"\n[ringer.py] check timed out after {CHECK_TIMEOUT_S}s\n"
        return proc.returncode, timed_out, output


class RingerRunner:
    def __init__(
        self,
        manifest: Manifest,
        config: AppConfig,
        identity: str,
        dashboard_enabled: bool = True,
        force_browser: bool = False,
    ) -> None:
        self.manifest = manifest
        self.config = config
        self.identity = identity
        self.dashboard_enabled = dashboard_enabled
        self.run_id = build_run_id(manifest.run_name)
        self.started_at = datetime.now(timezone.utc)
        self.lock = threading.RLock()
        self.runtimes = [self._task_runtime(task) for task in manifest.tasks]
        self.state_writer = StateWriter(
            self.run_id,
            manifest.run_name,
            identity,
            config.state_dir,
            config.engines,
            self.started_at,
            self.runtimes,
            self.lock,
        )
        self.dashboard = (
            Dashboard(
                state_path=self.state_writer.path,
                preferred_port=config.dashboard_port_base,
                hud_app_path=config.hud_app_path,
                force_browser=force_browser,
            )
            if dashboard_enabled
            else None
        )
        self.logger = EvalLogger(config.eval)
        self.verifier = Verifier()
        self.semaphore = asyncio.Semaphore(manifest.max_parallel)
        self.active_processes: dict[int, asyncio.subprocess.Process] = {}

    async def run(self) -> int:
        self.manifest.workdir.mkdir(parents=True, exist_ok=True)
        final_state = False
        try:
            self.state_writer.start()
            if self.dashboard is not None:
                self.state_writer.set_port(self.dashboard.start())
            await asyncio.gather(*(self._run_task(runtime) for runtime in self.runtimes))
            final_state = True
            return 0 if all(runtime.status == "pass" for runtime in self.runtimes) else 1
        except asyncio.CancelledError:
            await self.kill_all_workers()
            with self.lock:
                now = time.monotonic()
                for runtime in self.runtimes:
                    if runtime.status not in {"pass", "fail"}:
                        runtime.status = "fail"
                        runtime.final_verdict = "ERROR"
                        runtime.ended_at_monotonic = runtime.ended_at_monotonic or now
            self.state_writer.flush()
            final_state = True
            raise
        finally:
            if final_state:
                self.state_writer.finish()
            self.state_writer.stop()
            if self.dashboard is not None:
                self.dashboard.stop()
            self.logger.close()
            print_summary(self.run_id, self.runtimes)

    async def kill_all_workers(self) -> None:
        procs = list(self.active_processes.values())
        for proc in procs:
            if proc.returncode is None:
                terminate_process_group(proc)
        if procs:
            await asyncio.sleep(1)
        for proc in procs:
            if proc.returncode is None:
                kill_process_group(proc)

    async def _run_task(self, runtime: TaskRuntime) -> None:
        async with self.semaphore:
            with self.lock:
                runtime.started_at_monotonic = time.monotonic()
            prepared, prepare_error = await self._prepare_taskdir(runtime)
            if not prepared:
                await self._record_prepare_error(runtime, prepare_error or "taskdir preparation failed")
                return
            current_spec = runtime.task.spec
            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                retrying = attempt > 1
                with self.lock:
                    runtime.attempts = attempt
                    runtime.status = "retrying" if retrying else "running"
                attempt_started = time.monotonic()
                worker = await self._run_worker(runtime, current_spec, attempt)
                with self.lock:
                    runtime.worker_pid = None
                    runtime.status = "verifying"
                    if worker.tokens is not None:
                        runtime.tokens = (runtime.tokens or 0) + worker.tokens
                verify = await self.verifier.verify(runtime.task, runtime.taskdir)
                verdict = verdict_for(worker, verify)
                duration_ms = int((time.monotonic() - attempt_started) * 1000)
                self._log_attempt(runtime, current_spec, retrying, worker, verify, verdict, duration_ms)
                if verdict == "PASS":
                    with self.lock:
                        runtime.status = "pass"
                        runtime.final_verdict = verdict
                        runtime.ended_at_monotonic = time.monotonic()
                    await self._cleanup_worktree_on_pass(runtime)
                    return
                if attempt < max_attempts and verdict in {"FAIL", "TIMEOUT"}:
                    failure_context = build_failure_context(runtime.log_path, verify.raw_output_excerpt)
                    current_spec = (
                        f"{runtime.task.spec}\n\n"
                        f"Previous attempt failed: {failure_context}. Fix it."
                    )
                    continue
                with self.lock:
                    runtime.status = "fail"
                    runtime.final_verdict = verdict
                    runtime.ended_at_monotonic = time.monotonic()
                return

    async def _prepare_taskdir(self, runtime: TaskRuntime) -> tuple[bool, str | None]:
        taskdir = runtime.taskdir
        if self.manifest.worktrees and self.manifest.repo is not None:
            taskdir.parent.mkdir(parents=True, exist_ok=True)
            if taskdir.exists():
                return False, f"worktree taskdir already exists: {taskdir}"
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.manifest.repo),
                "worktree",
                "add",
                str(taskdir),
                "HEAD",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                message = stdout.decode("utf-8", errors="replace")
                append_text(runtime.log_path, f"[ringer.py] git worktree add failed:\n{message}\n")
                return False, message.strip() or "git worktree add failed"
            return True, None
        taskdir.mkdir(parents=True, exist_ok=True)
        return True, None

    async def _cleanup_worktree_on_pass(self, runtime: TaskRuntime) -> None:
        if not (self.manifest.worktrees and self.manifest.repo is not None):
            return
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.manifest.repo),
            "worktree",
            "remove",
            "--force",
            str(runtime.taskdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            message = stdout.decode("utf-8", errors="replace")
            append_text(runtime.log_path, f"[ringer.py] git worktree remove failed:\n{message}\n")

    async def _record_prepare_error(self, runtime: TaskRuntime, error: str) -> None:
        with self.lock:
            runtime.attempts = 1
            runtime.status = "fail"
            runtime.final_verdict = "ERROR"
            runtime.ended_at_monotonic = time.monotonic()
        verify = VerifyResult(
            ok=False,
            check_returncode=None,
            check_timed_out=False,
            raw_output_excerpt="",
        )
        worker = WorkerResult(returncode=None, timed_out=False, tokens=None, error=error)
        self._log_attempt(runtime, runtime.task.spec, False, worker, verify, "ERROR", 0)

    async def _run_worker(self, runtime: TaskRuntime, spec: str, attempt: int) -> WorkerResult:
        log_path = runtime.log_path
        engine = self.config.engines.get(runtime.task.engine)
        if engine is None:
            return WorkerResult(
                returncode=None,
                timed_out=False,
                tokens=None,
                error=f"unknown worker engine: {runtime.task.engine}",
            )
        if runtime.task.full_access and not self.config.allow_full_access:
            return WorkerResult(
                returncode=None,
                timed_out=False,
                tokens=None,
                error=(
                    f"task requested full_access with engine {runtime.task.engine}, "
                    "but config allow_full_access is false"
                ),
            )
        cmd = build_worker_command(
            engine,
            taskdir=runtime.taskdir,
            spec=spec,
            full_access=runtime.task.full_access,
        )
        append_text(
            log_path,
            "\n"
            f"[ringer.py] attempt {attempt} started {datetime.now(timezone.utc).isoformat()}\n"
            f"[ringer.py] engine: {runtime.task.engine}\n"
            f"[ringer.py] command: {shell_command_for_display(cmd)} < /dev/null\n",
        )
        capture = RollingBytes(max_bytes=1_000_000)
        try:
            log_fh = log_path.open("ab")
        except OSError as exc:
            return WorkerResult(returncode=None, timed_out=False, tokens=None, error=str(exc))
        async with AsyncFileCloser(log_fh):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(runtime.taskdir),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                message = f"[ringer.py] worker spawn failed: {exc}\n"
                log_fh.write(message.encode("utf-8", errors="replace"))
                log_fh.flush()
                return WorkerResult(returncode=None, timed_out=False, tokens=None, error=str(exc))
            with self.lock:
                runtime.worker_pid = proc.pid
            self.active_processes[proc.pid] = proc
            reader = asyncio.create_task(self._tee_stream(proc, log_fh, capture))
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=runtime.task.timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                terminate_process_group(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    kill_process_group(proc)
                    await proc.wait()
            try:
                await asyncio.wait_for(reader, timeout=5)
            except asyncio.TimeoutError:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            self.active_processes.pop(proc.pid, None)
        output_tail = capture.text()
        tokens = parse_token_count(output_tail, engine.token_regex)
        if timed_out:
            append_text(log_path, f"\n[ringer.py] worker timed out after {runtime.task.timeout_s}s\n")
        append_text(log_path, f"[ringer.py] attempt {attempt} exited rc={proc.returncode}\n")
        return WorkerResult(returncode=proc.returncode, timed_out=timed_out, tokens=tokens)

    async def _tee_stream(
        self,
        proc: asyncio.subprocess.Process,
        log_fh: Any,
        capture: "RollingBytes",
    ) -> None:
        if proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            log_fh.write(chunk)
            log_fh.flush()
            capture.extend(chunk)
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def _log_attempt(
        self,
        runtime: TaskRuntime,
        spec: str,
        retrying: bool,
        worker: WorkerResult,
        verify: VerifyResult,
        verdict: str,
        duration_ms: int,
    ) -> None:
        notes_parts = [
            f"retry={'true' if retrying else 'false'}",
            f"worker_returncode={worker.returncode}",
        ]
        if worker.error:
            notes_parts.append(f"worker_error={worker.error}")
        if verify.missing_files:
            notes_parts.append(f"missing_expect_files={json.dumps(list(verify.missing_files))}")
        notes_parts.append("raw_check_output_first_2000_chars:")
        notes_parts.append(verify.raw_output_excerpt)
        self.logger.log_attempt(
            {
                "run_id": self.run_id,
                "pattern": "ringer-py",
                "task_key": runtime.task.key,
                "spec": spec[:500],
                "worker_engine": runtime.task.engine,
                "shepherd_model": SHEPHERD_MODEL,
                "verify_method": VERIFY_METHOD,
                "verdict": verdict,
                "duration_ms": duration_ms,
                "worker_tokens": worker.tokens,
                "notes": "\n".join(notes_parts),
                "orchestrator": self.identity,
            }
        )

    def _task_runtime(self, task: TaskSpec) -> TaskRuntime:
        taskdir = self._taskdir(task)
        log_path = self._log_path(task, taskdir)
        with contextlib.suppress(FileNotFoundError):
            log_path.unlink()
        return TaskRuntime(
            task=task,
            taskdir=taskdir,
            log_path=log_path,
            spec_short=shorten(task.spec, 120),
        )

    def _taskdir(self, task: TaskSpec) -> Path:
        taskdir = (self.manifest.workdir / task.key).resolve()
        workdir = self.manifest.workdir.resolve()
        if taskdir != workdir and workdir not in taskdir.parents:
            raise ValueError(f"task key escapes workdir: {task.key}")
        return taskdir

    def _log_path(self, task: TaskSpec, taskdir: Path) -> Path:
        if not self.manifest.worktrees:
            return taskdir / "worker.log"
        logs_dir = (self.manifest.workdir / "logs").resolve()
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = (logs_dir / f"{task.key}.worker.log").resolve()
        if log_path != logs_dir and logs_dir not in log_path.parents:
            raise ValueError(f"task key escapes logs dir: {task.key}")
        return log_path


class RollingBytes:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.data = bytearray()

    def extend(self, chunk: bytes) -> None:
        self.data.extend(chunk)
        overflow = len(self.data) - self.max_bytes
        if overflow > 0:
            del self.data[:overflow]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class AsyncFileCloser:
    def __init__(self, fh: Any) -> None:
        self.fh = fh

    async def __aenter__(self) -> Any:
        return self.fh

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.fh.close()


def verdict_for(worker: WorkerResult, verify: VerifyResult) -> str:
    if worker.error:
        return "ERROR"
    if worker.timed_out or verify.check_timed_out:
        return "TIMEOUT"
    if verify.ok:
        return "PASS"
    return "FAIL"


def build_run_id(run_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_name.strip()).strip("-")
    # pid suffix: same-second launches of the same run_name must not collide
    # (concurrent ringer runs would otherwise share a state file and eval run_id).
    return f"{safe_name or 'ringer'}-{stamp}-p{os.getpid()}"


def find_repo_identity(start: Path | None = None) -> str | None:
    """Per-repo agent identity: nearest .fleet-agent file walking up from start.

    Jon's fleet convention (2026-07-02): each repo has its own agent name
    (projects.agent_name in the fleet DB); a .fleet-agent file in the repo
    root mirrors it so stdlib-only tools like ringer resolve it without a
    database connection.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".fleet-agent"
        try:
            if candidate.is_file():
                name = re.sub(r"[^A-Za-z0-9_-]", "", candidate.read_text(encoding="utf-8", errors="replace").strip())
                if name:
                    return name
        except OSError:
            continue
    return None


def resolve_identity(
    value: str | None,
    config: AppConfig,
    identity_start_paths: Iterable[Path] = (),
) -> str:
    repo_identities = [find_repo_identity(start) for start in identity_start_paths]
    for candidate in (
        value,
        os.environ.get("FLEET_IDENTITY"),
        os.environ.get(f"{ENV_VAR_PREFIX}_IDENTITY"),
        *repo_identities,
        find_repo_identity(),
        config.identity_default,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return socket.gethostname().split(".", 1)[0] or TOOL_NAME


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def parse_token_count(text: str, token_regex: str | None = DEFAULT_TOKEN_REGEX) -> int | None:
    if token_regex:
        matches = list(re.finditer(token_regex, text, flags=re.IGNORECASE))
        for match in reversed(matches):
            groups = [item for item in match.groups() if item]
            value = groups[0] if groups else match.group(0)
            number = re.search(r"([0-9][0-9,]*)", value)
            if number:
                return int(number.group(1).replace(",", ""))
        return None
    matches = re.findall(r"tokens\s+used\s*:?\s*([0-9][0-9,]*)", text, flags=re.IGNORECASE)
    if not matches:
        matches = re.findall(
            r"tokens\s+used\s*\r?\n\s*([0-9][0-9,]*)",
            text,
            flags=re.IGNORECASE,
        )
    if not matches:
        return None
    return int(matches[-1].replace(",", ""))


def build_worker_command(
    engine: EngineConfig,
    *,
    taskdir: Path,
    spec: str,
    full_access: bool,
) -> list[str]:
    access_args = engine.full_access_args if full_access else engine.sandbox_args
    command = [engine.bin]
    for item in engine.args_template:
        if item == "{access_args}":
            command.extend(access_args)
            continue
        if item == "{sandbox_args}":
            command.extend(engine.sandbox_args)
            continue
        if item == "{full_access_args}":
            command.extend(engine.full_access_args)
            continue
        command.append(item.replace("{taskdir}", str(taskdir)).replace("{spec}", spec))
    return command


def validate_manifest_engines(manifest: Manifest, config: AppConfig) -> None:
    missing = sorted({task.engine for task in manifest.tasks if task.engine not in config.engines})
    if missing:
        raise ValueError(f"unknown worker engine(s): {', '.join(missing)}")


def read_dashboard_html() -> str:
    try:
        return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        return MINIMAL_DASHBOARD_HTML


def tail_lines(path: Path, line_count: int) -> list[str]:
    if line_count <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-line_count:]


def tail_text(path: Path, max_bytes: int = 6000, line_count: int = 40) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-line_count:])


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CMD_JSON_DOUBLE_RE = re.compile(
    r'"(?:cmd|command)"\s*:\s*"(?P<cmd>(?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)
CMD_JSON_SINGLE_RE = re.compile(
    r"'(?:cmd|command)'\s*:\s*'(?P<cmd>(?:\\.|[^'\\])*)'",
    re.IGNORECASE,
)
CMD_LABEL_RE = re.compile(
    r"\b(?:exec(?:_command|/command)?|shell command|command)\b\s*[:=]\s*(?P<cmd>.+)$",
    re.IGNORECASE,
)
CMD_RAN_RE = re.compile(r"^\s*(?:[*>-]\s*)?(?:ran|running)\s+`?(?P<cmd>.+?)`?\s*$", re.IGNORECASE)
CMD_PROMPT_RE = re.compile(r"^\s*(?:\$|\+)\s+(?P<cmd>.+)$")
PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update)\s+File:\s+(?P<path>.+)$", re.IGNORECASE)
WRITE_QUOTED_FILE_RE = re.compile(
    r"\b(?:created|modified|updated|wrote|writing|saved|edited|patched)\b[^`'\"]{0,48}"
    r"[`'\"](?P<path>[^`'\"]+)[`'\"]",
    re.IGNORECASE,
)
WRITE_FILE_RE = re.compile(
    r"\b(?:created|modified|updated|wrote|writing|saved|edited|patched)\s+"
    r"(?:file\s+)?(?P<path>[A-Za-z0-9_./~:-]+\.[A-Za-z0-9][A-Za-z0-9_+-]*)",
    re.IGNORECASE,
)
ASSISTANT_PREFIX_RE = re.compile(
    r"^\s*(?:assistant|codex(?:-[A-Za-z0-9_-]+)?|agent)\s*(?:>|:|-)\s*(?P<text>.+)$",
    re.IGNORECASE,
)


def worker_activity(path: Path, log_tail: list[str]) -> str:
    text = tail_text(path, max_bytes=ACTIVITY_TAIL_BYTES, line_count=80)
    if text:
        for finder in (last_shell_command_activity, last_written_file_activity, last_assistant_activity):
            activity = finder(text)
            if activity:
                return activity
    return activity_fallback(log_tail)


def last_shell_command_activity(text: str) -> str:
    for line in reversed(non_empty_log_lines(text)):
        command = extract_shell_command(line)
        if command:
            return f"ran: {shorten(command, ACTIVITY_TEXT_LIMIT)}"
    return ""


def last_written_file_activity(text: str) -> str:
    for line in reversed(non_empty_log_lines(text)):
        path = extract_written_file(line)
        if path:
            return f"wrote {shorten(path, ACTIVITY_TEXT_LIMIT)}"
    return ""


def last_assistant_activity(text: str) -> str:
    lines = list(reversed(non_empty_log_lines(text)))
    for line in lines:
        match = ASSISTANT_PREFIX_RE.match(line)
        if match:
            candidate = clean_log_text(match.group("text"))
            if candidate:
                return shorten(candidate, ACTIVITY_TEXT_LIMIT)
    for line in lines:
        if looks_like_assistant_text(line):
            return shorten(clean_log_text(line), ACTIVITY_TEXT_LIMIT)
    return ""


def activity_fallback(log_tail: list[str]) -> str:
    for line in reversed(log_tail):
        candidate = clean_log_text(line)
        if candidate:
            return shorten(candidate, ACTIVITY_TEXT_LIMIT)
    return ""


def non_empty_log_lines(text: str) -> list[str]:
    return [line for line in (clean_log_text(raw) for raw in text.splitlines()) if line]


def clean_log_text(value: str) -> str:
    value = ANSI_RE.sub("", value)
    return " ".join(value.strip().split())


def extract_shell_command(line: str) -> str:
    if line.startswith("[ringer.py]"):
        return ""
    for pattern in (CMD_JSON_DOUBLE_RE, CMD_JSON_SINGLE_RE, CMD_LABEL_RE, CMD_RAN_RE, CMD_PROMPT_RE):
        match = pattern.search(line)
        if not match:
            continue
        command = clean_command(match.group("cmd"))
        if command and looks_like_shell_command(command):
            return command
    return ""


def clean_command(value: str) -> str:
    command = value.strip().strip("`")
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"'", '"'}:
        command = command[1:-1]
    command = command.replace(r"\n", " ").replace(r"\t", " ")
    command = command.replace(r"\"", '"').replace(r"\'", "'")
    command = re.split(r"\s+<\s*/dev/null\b", command, maxsplit=1)[0]
    return clean_log_text(command).strip(" ,")


def looks_like_shell_command(command: str) -> bool:
    if not command or command.startswith(("{", "[", "(", "<")):
        return False
    lower = command.lower()
    if lower.startswith(("error ", "unknown ", "none ", "failed ")):
        return False
    if lower.startswith("codex exec "):
        return False
    try:
        first = shlex.split(command)[0]
    except ValueError:
        first = command.split(maxsplit=1)[0]
    return bool(re.match(r"^(?:[A-Za-z0-9_./-]+)(?:\.[A-Za-z0-9_+-]+)?$", first))


def extract_written_file(line: str) -> str:
    if line.startswith("[ringer.py]"):
        return ""
    for pattern in (PATCH_FILE_RE, WRITE_QUOTED_FILE_RE, WRITE_FILE_RE):
        match = pattern.search(line)
        if not match:
            continue
        path = normalize_activity_path(match.group("path"))
        if path:
            return path
    return ""


def normalize_activity_path(value: str) -> str:
    path = value.strip().strip("`'\".,;:)")
    path = re.sub(r":\d+(?::\d+)?$", "", path)
    if not re.search(r"\.[A-Za-z0-9][A-Za-z0-9_+-]*(?:$|[?#])", path):
        return ""
    if path.startswith("/"):
        return Path(path).name
    return path


def looks_like_assistant_text(line: str) -> bool:
    lower = line.lower()
    if not line or line.startswith(("[", "{", "}", "```", "***", "@@", "$", "+")):
        return False
    if re.match(r"^(?:exec|command|stdout|stderr|chunk id|wall time|process exited)\b", lower):
        return False
    if re.match(r"^(?:error|warning|info|debug)[:\s]", lower):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_./-]*:\d+(?::\d+)?:", line):
        return False
    return bool(re.search(r"[A-Za-z]", line))


def build_failure_context(log_path: Path, raw_check_output: str) -> str:
    worker_tail = tail_text(log_path)
    context = f"{worker_tail}\n{raw_check_output}".strip()
    if len(context) > 6000:
        return context[-6000:]
    return context


def shorten(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def shell_command_for_display(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def dry_run(
    manifest: Manifest,
    config: AppConfig,
    identity: str,
    dashboard_enabled: bool,
    force_browser: bool,
) -> None:
    print("DRY RUN: no codex workers will be spawned.")
    print(f"Run: {manifest.run_name}")
    print(f"Identity: {identity}")
    print(f"Config: {config.path if config.path else '(safe defaults)'}")
    print(f"Workdir: {manifest.workdir}")
    print(f"Max parallel: {manifest.max_parallel}")
    print(f"Worktrees: {manifest.worktrees} repo={manifest.repo}")
    print(f"State dir: {config.state_dir}")
    print(f"Eval backend: {config.eval.backend}")
    print(f"Dashboard: {'on' if dashboard_enabled else 'off'}")
    if dashboard_enabled:
        mode = "browser"
        if not force_browser and config.hud_app_path is not None:
            mode = f"HUD app {config.hud_app_path} when available, browser fallback"
        print(f"Dashboard opener: {mode}")
        print(f"Dashboard port base: {config.dashboard_port_base}")
    print("Tasks:")
    for task in manifest.tasks:
        taskdir = (manifest.workdir / task.key).resolve()
        engine = config.engines.get(task.engine)
        full_access_allowed = task.full_access and config.allow_full_access
        cmd = (
            build_worker_command(
                engine,
                taskdir=taskdir,
                spec=task.spec,
                full_access=task.full_access,
            )
            if engine is not None
            else []
        )
        print(f"  - {task.key}")
        print(f"    engine: {task.engine}")
        print(f"    dir: {taskdir}")
        print(f"    timeout_s: {task.timeout_s}")
        if task.full_access:
            print(f"    full_access: true allowed={full_access_allowed}")
        else:
            print("    full_access: false")
        print(f"    expect_files: {list(task.expect_files)}")
        print(f"    check: {task.check}")
        if engine is None:
            print("    command: ERROR unknown engine")
        elif task.full_access and not config.allow_full_access:
            print("    command: ERROR full_access requires allow_full_access=true in config")
        else:
            print(f"    command: {shell_command_for_display(cmd)} < /dev/null")


def print_summary(run_id: str, runtimes: list[TaskRuntime]) -> None:
    print("\nSummary")
    print(f"run_id: {run_id}")
    header = f"{'task':<24} {'status':<8} {'verdict':<8} {'attempts':>8} {'tokens':>10} {'elapsed_s':>10}"
    print(header)
    print("-" * len(header))
    now = time.monotonic()
    for runtime in runtimes:
        tokens = "" if runtime.tokens is None else str(runtime.tokens)
        print(
            f"{runtime.task.key:<24} {runtime.status:<8} "
            f"{(runtime.final_verdict or ''):<8} {runtime.attempts:>8} "
            f"{tokens:>10} {runtime.elapsed_s(now):>10.1f}"
        )


def create_demo_manifest() -> Path:
    root = Path(tempfile.mkdtemp(prefix="ringer-demo-"))
    workdir = root / "work"
    manifest = {
        "run_name": "ringer-demo",
        "workdir": str(workdir),
        "max_parallel": 3,
        "worktrees": False,
        "repo": None,
        "tasks": [
            {
                "key": "alpha",
                "spec": "Create alpha.txt containing exactly: alpha ready",
                "check": "test \"$(cat alpha.txt 2>/dev/null)\" = \"alpha ready\"",
                "expect_files": ["alpha.txt"],
            },
            {
                "key": "bravo",
                "spec": "Create bravo.txt containing exactly: bravo ready",
                "check": "test \"$(cat bravo.txt 2>/dev/null)\" = \"bravo ready\"",
                "expect_files": ["bravo.txt"],
            },
            {
                "key": "charlie",
                "spec": "Create charlie.txt containing exactly: charlie ready",
                "check": "test \"$(cat charlie.txt 2>/dev/null)\" = \"charlie ready\"",
                "expect_files": ["charlie.txt"],
            },
        ],
    }
    path = root / "ringer.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


async def run_manifest(
    manifest: Manifest,
    config: AppConfig,
    identity: str,
    dashboard_enabled: bool,
    force_browser: bool,
) -> int:
    runner = RingerRunner(
        manifest,
        config=config,
        identity=identity,
        dashboard_enabled=dashboard_enabled,
        force_browser=force_browser,
    )
    return await runner.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ringer.py",
        description=(
            "Ringer: deterministic parallel AI-agent orchestrator. Runs manifest tasks in parallel, "
            "verifies artifacts with executed checks, retries failures once, logs eval rows, "
            "and serves a live dashboard."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.toml (default: XDG config path)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a ringer manifest")
    run_parser.add_argument("manifest", type=Path, help="path to ringer.json")
    run_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    run_parser.add_argument("--max-parallel", type=int, help="override manifest max_parallel")
    run_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    run_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    run_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    run_parser.add_argument("--dry-run", action="store_true", help="print the plan without spawning codex")

    demo_parser = subparsers.add_parser("demo", help="generate and run a 3-task toy manifest in /tmp")
    demo_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    demo_parser.add_argument("--max-parallel", type=int, help="override demo max_parallel")
    demo_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    demo_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    demo_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    demo_parser.add_argument("--dry-run", action="store_true", help="print the demo plan without spawning codex")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Keep progress lines live when stdout is a pipe (tee, orchestrators).
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = AppConfig.load(args.config)
        if args.command == "demo":
            manifest_path = create_demo_manifest()
            print(f"Demo manifest: {manifest_path}")
        else:
            manifest_path = args.manifest
        manifest = Manifest.from_path(manifest_path).with_max_parallel(args.max_parallel)
        validate_manifest_engines(manifest, config)
        identity_start_paths = [manifest.workdir]
        if manifest.source_path is not None:
            identity_start_paths.append(manifest.source_path.parent)
        identity = resolve_identity(args.identity, config, identity_start_paths)
        dashboard_enabled = not args.no_dashboard
        if args.dry_run:
            dry_run(
                manifest,
                config=config,
                identity=identity,
                dashboard_enabled=dashboard_enabled,
                force_browser=args.browser,
            )
            return 0
        return asyncio.run(
            run_manifest(
                manifest,
                config=config,
                identity=identity,
                dashboard_enabled=dashboard_enabled,
                force_browser=args.browser,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ringer.py: error: {exc}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(main())
