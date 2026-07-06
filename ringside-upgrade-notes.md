# Ringside Upgrade Notes

Working log for the four-feature Ringside/Ringer upgrade (2026-07-04). Written while two
production swarms (`aicred-docs-repair`, `aicred-site-fixes`) were live — all edits happened in
an isolated git worktree, never in `~/fleet/swarm` directly (its `dashboard/dashboard.html` is
re-read from disk on every live-dashboard HTTP request by the running orchestrators, so editing
it in place would have visibly corrupted Jon's screen recording).

## Stack discovered

- **Repo:** `~/fleet/swarm` (git, `main` branch, Jon works here on the Mac alongside Lex; push feature branches + PRs).
- **Ringer** (`ringer.py`, stdlib-only Python 3.11+): orchestrator + per-run state writer
  (`~/.ringer/runs/<run_id>.json`, flushed every 1s) + optional embedded HTTP dashboard
  (`Dashboard` class, serves `dashboard/dashboard.html` + `/state.json`, live-read from disk
  every request — no caching).
- **Ringside** (`hud/`): a **Tauri v2** app (Rust backend `hud/src/main.rs` + vanilla-JS/HTML
  frontend, NOT SwiftUI/React). `productName: Ringside`, `identifier: com.jonedwards.ringside`.
  - Rust side polls `~/.ringer/runs/*.json` every 1s (`start_state_poller`), computes
    live/finished/died state + sort order, emits a `ringer-runs` Tauri event with the full JSON
    array (fields are passed through mostly verbatim — new task/run JSON fields show up in the
    frontend for free, no Rust changes needed unless you need new file I/O or window behavior).
  - Frontend: `hud/dist/index.html` (webview HTML+CSS+JS) is a **generated copy** of the repo's
    canonical `dashboard/dashboard.html` — synced by `hud/scripts/sync-dist.sh` at build time.
    **Always edit `dashboard/dashboard.html`, never `hud/dist/index.html` directly**, then run
    the sync script (or let `tauri build`'s `beforeBuildCommand` do it).
  - `hud/frontend/hud.js` (synced to `hud/dist/hud.js`) is the Tauri-only bridge: builds the
    custom title bar, window drag, close button, HUD ticker text. It listens for `ringer-runs`
    and also calls the shared `update()` function defined inline in `dashboard.html`.
  - `dashboard.html`'s inline script also does `pollLocalState()` — a `fetch('/state.json')`
    poll. That's for when the SAME html is served by ringer.py's own embedded HTTP dashboard
    (browser tab, no Tauri). Under Tauri it 404s harmlessly (try/catch swallows it) since there's
    no HTTP server backing the webview.
  - Window: 360x420 default, transparent, decorations:false, alwaysOnTop, resizable (min
    280x220). `toggle_collapse` Tauri command shrinks it to a 34px title-bar-only strip.
  - Capabilities: `hud/capabilities/default.json` lists allowed commands
    (`core:default` + window permissions). New `#[tauri::command]` fns must be added to both
    `invoke_handler!` in `main.rs` AND (if not covered by `core:default`) get a permission there.

## Branch / worktree

- Canonical repo: `~/fleet/swarm` (do not edit while swarms are live).
- Working worktree: `~/fleet/swarm-ringside-upgrades`, branch `feat/ringside-upgrades`.
- Local only — never pushed, never touched `main`.

## What changed (ringer.py — Tier 0 + Tier 4 "final report", the plan doc's build order #1)

File: `~/fleet/swarm-ringside-upgrades/ringer.py`

- Added `ArtifactConfig` dataclass + `load_artifact_config()`; new `[artifact]` config block
  (`enabled`, `out` template, `report_out` template, `index_out`) documented in
  `config.sample.toml`. Defaults write under `~/.ringer/artifacts/`.
- `TaskRuntime` gained `last_check_output`, `last_check_returncode`, `last_check_timed_out` —
  previously the verify/check output was logged to the eval sink and then thrown away; nothing
  else in the process remembered it, so neither a live page nor a final report could show *why*
  a task failed without grepping `worker.log`. Set in `RingerRunner._run_task` right after
  `verify = await self.verifier.verify(...)`.
- `StateWriter` now takes `max_parallel` + `artifact: ArtifactConfig`. `snapshot()` gained, per
  task: `check` (the check command), `timeout_s`, `taskdir` (absolute path, for report links),
  `verdict` (PASS/FAIL/TIMEOUT/ERROR — more specific than `status`), `check_returncode`,
  `check_output_tail` (capped), `log_tail_full` (40 lines vs the existing 3-line `log_tail`) —
  this is the data Ringside's detail view (feature 1) needed and didn't have.
- Run-level snapshot gained `max_parallel`, `artifact_path`, `report_path`, `report_ready`.
- `flush()` now also renders+atomically-writes the Tier 0 live status HTML
  (`render_status_html`) to `artifact_path` on every 1s tick, best-effort (wrapped so a render
  bug can never fail the run) — this is the "live artifact" feature 2 needs; Ringside just has
  to display the file, not build the renderer.
- `finish()` now also renders the final report (`render_final_report_html`) to `report_path`
  once, after the summary is built — this is feature 4. Self-contained single HTML file: full
  task table, per-task attempts/timings/verdict/check command/check output (fuller than the live
  page, capped ~4000 chars with a truncation note), `file://` links to each task's `taskdir`,
  `worker.log`, and `report.md`/`report.html` if present.
  Reporter failures never gate run exit code (matches the plan's "pretty report failing
  shouldn't mark a green run red" rule).
- Multi-run index: `render_artifact_index_html()` + best-effort write of
  `~/.ringer/artifacts/index.html` on every flush, scanning `state_dir/runs/*.json` — gives one
  pane of glass across concurrent runs (tonight's exact scenario: 2 concurrent swarms).
- New CLI flag `--no-artifact` (both `run` and `demo`), symmetric with `--no-dashboard`.

## Scope update (mid-task, from Jon via coordinator)

Added **Feature 5: settings panel with persistent customization** — accent/theme color, layout,
density (compact/comfortable), which columns/fields show, default view mode. Must persist across
Ringside restarts. Guidance: plain JSON file (not UserDefaults — this is Tauri, not Swift),
ideally at `~/.ringer/ringside-settings.json` so the Tier 0 HTML artifact can read the same theme
later. Explicitly told to design this together with Feature 3 (view modes) since "default view"
becomes one persisted setting rather than a separate mechanism. Folded into priority after 4 and 1.

Settings schema (JSON, `~/.ringer/ringside-settings.json`):
```json
{
  "version": 1,
  "theme": {"accent": "#28d7ff"},
  "density": "comfortable",
  "columns": {"showTokens": true, "showActivity": true, "showChildren": true},
  "layout": {"defaultView": "grid", "hiddenRunIds": []}
}
```

## What changed (Ringside — hud/)

**`hud/src/main.rs`** — 4 new `#[tauri::command]` fns, registered in `invoke_handler!` alongside
the existing `hide_window`/`toggle_collapse` (no `capabilities/default.json` change needed —
custom app-binary commands aren't gated by the ACL system the way plugin commands are; verified
empirically since `hide_window`/`toggle_collapse` already worked with only `core:default` +
window permissions listed):
- `resize_main_window(width, height)` — feature 3 window-size presets (Medium/Large), separate
  from `toggle_collapse`'s mini-strip bookkeeping so the two don't fight.
- `read_artifact_html(path)` — feature 2; reads a ringer.py-rendered HTML artifact off disk,
  canonicalized and restricted to be under the resolved ringer `state_dir` (path-traversal
  safe). Frontend injects the result into an `<iframe srcdoc>`.
- `load_settings()` / `save_settings(settings)` — feature 5; plain JSON file at
  `<state_dir>/ringside-settings.json` (atomic tmp+rename write), not UserDefaults, so a future
  Tier 0 HTML artifact could read the same theme.

**`dashboard/dashboard.html`** (canonical source — `hud/dist/index.html` and `hud/build.rs` both
regenerate from this file; never edit `dist/index.html` directly):
- Feature 1: click a task card to expand a detail panel (engine, attempts+verdict, check
  command, taskdir, full spec, check output, 40-line live worker.log tail) built from the new
  ringer.py state fields. Per-task progress bar (`elapsed_s / timeout_s`), hidden when no
  timeout is known.
- Feature 2: new "Artifact" view mode embeds the run's live Tier 0 status page via
  `read_artifact_html` + `iframe.srcdoc`; degrades to a friendly empty-state message outside
  Tauri.
- Feature 3: toolbar with Grid/Artifact toggle, Collapse-all (per-run), a substring filter over
  run+task names ("show only some agents"), and Mini/Medium/Large window-size buttons.
- Feature 5: gear icon opens a settings panel — accent color (drives a new `--accent` CSS
  variable; only the neutral "live" highlight is themeable, pass/fail keep fixed semantic
  colors), density (comfortable/compact), default view mode (feeds directly into feature 3's
  view-mode switch, not a separate mechanism), and per-column show/hide (activity line, child
  count, run token totals). Persisted via the new Tauri commands, debounced ~350ms; gracefully
  in-memory-only outside Tauri.

## Build / run

```bash
cd ~/fleet/swarm-ringside-upgrades
/opt/homebrew/bin/python3.11 ringer.py demo --no-dashboard --no-artifact   # sanity: unchanged behavior
/opt/homebrew/bin/python3.11 ringer.py demo --no-dashboard                # exercise new artifact code path
# artifacts land in ~/.ringer/artifacts/ (shared dir, but distinctly named per run_id — does not
# collide with or touch the two production runs' state files or dashboards)
```

Rust: the Homebrew `cargo`/`rustc` on PATH (`/opt/homebrew/bin`, comes before `~/.cargo/bin`) is
pinned to 1.87.0 and can't build current deps (darling/time/image need 1.88+). Use the
rustup-managed toolchain explicitly:
```bash
cd ~/fleet/swarm-ringside-upgrades/hud
PATH="$HOME/.cargo/bin:$PATH" cargo check   # verified clean, 1.92.0
```

Tauri build (do NOT run `tauri build` in a way that overwrites `/Applications/Ringside.app` —
stage output only):
```bash
cd ~/fleet/swarm-ringside-upgrades/hud
bash scripts/sync-dist.sh   # regenerate dist/ from dashboard.html + frontend/hud.js
# tauri build/dev commands, if run, must not touch /Applications/Ringside.app
```

## Tested

- `ringer.py` (Python 3.11, `/opt/homebrew/bin/python3.11`): syntax check, a real `demo
  --no-dashboard` run (3 real codex tasks, all pass) producing a correct live-status HTML,
  final-report HTML, and multi-run index (the index correctly picked up the two live production
  runs read-only, proving the "one pane of glass across concurrent swarms" goal). Also
  unit-tested `render_status_html`/`render_final_report_html`/`render_artifact_index_html`
  directly with synthetic fail/retry/long-spec-truncation state to confirm the fail-block,
  "(retried)" marker, and check-output rendering. All test artifacts cleaned up from
  `~/.ringer/runs/` and `~/.ringer/artifacts/` afterward; the two production run state files
  were never touched (verified: `finished:true`, correct pass counts, all after the fact — they
  completed naturally, unrelated to this work).
- Rust: `cargo check` clean (via rustup's 1.92.0 toolchain) after both the command additions and
  the dashboard.html changes (which `build.rs` also re-syncs from source).
- Frontend: JS syntax (`node --check`) and HTML parse both clean. Live smoke test via
  chrome-devtools MCP: served `hud/dist/index.html` statically with a synthetic `state.json`
  (three tasks: running/pass/fail) and drove it in real Chrome —
  - task-card click expand/collapse (both a passing and the failing task, verifying check output
    + live-tail rendering and the "2 (FAIL)" attempts/verdict line),
  - progress bars rendered and color-matched to status,
  - Collapse-all toggled the whole run's task grid off/on,
  - agent filter correctly hid non-matching tasks (verified via direct DOM inspection — the MCP
    `fill`/`click` tools don't always dispatch a real `input`/`click` event on native form
    controls; confirmed by dispatching the event manually and reading `task.hidden` back),
  - settings panel opened, "Activity line" checkbox correctly hid the activity row across all
    task cards, density/accent controls present and wired,
  - Artifact view mode correctly hid the grid and showed the designed fallback message ("could
    not read the live artifact (Ringside desktop app only)") since no Tauri bridge exists in a
    plain browser — proving the code path is safe outside Ringside,
  - zero new console errors through all of the above (one pre-existing unrelated 404, likely
    favicon).
  - CSS bug found+fixed during this pass: `.toolbar-group[hidden]` needed an explicit rule — the
    plain `[hidden]` UA-stylesheet rule was losing to the author `.toolbar-group{display:flex}`
    rule at equal specificity (author origin always wins over UA origin), so the
    non-Tauri window-size buttons weren't actually hiding. Fixed and reverified.
- **Not tested (needs Jon or a follow-up session):** the three Tauri-specific invoke commands
  (`resize_main_window`, `read_artifact_html`, `load_settings`/`save_settings`) were verified by
  code review only — I did not launch a live Ringside/Tauri GUI instance during this session
  because Jon was actively screen-recording the installed `/Applications/Ringside.app` and a
  second always-on-top Ringside-style window appearing mid-recording would have been disruptive.
  All three commands compile, are registered, and their JS `invoke()` argument names match the
  Rust parameter names exactly (Tauri does no case conversion needed here since all args are
  already single lowercase words). Recommend a quick `cargo tauri dev` (or a staged debug build,
  never overwriting the installed app) after the recording session to confirm the real
  window-resize/read-artifact/settings-persist round trip.

## Remaining / TODO

- Live Tauri runtime verification of the 3 new commands (see above) — lowest-risk next step,
  just needs a moment when Jon isn't mid-recording.
- Multi-run index page (`~/.ringer/artifacts/index.html`) has no UI entry point in Ringside yet
  — it's generated and correct, but nothing in the app links to it. Small follow-up: a toolbar
  button or menu item that opens it (e.g. via the existing `open` shell pattern Dashboard.start()
  already uses, or another `read_artifact_html`-style embed) — worth it if Jon wants deeper
  history across concurrent/past runs than the live/report pair gives per-run.
- Per-run manual collapse (vs. only "collapse all") wasn't built — the agent filter already
  covers "show only some agents" reasonably well, but a per-run collapse chevron in `run-head`
  would be a small, cheap addition if wanted.
- Settings currently only cover accent/density/columns/default-view, per Jon's explicit list;
  no window position/size persistence was added beyond the existing `tauri-plugin-window-state`
  (already handles that generically).
- Not done: hooking the final-report/live-artifact paths into a "Reveal in Finder" / "Open"
  action from Ringside chrome (e.g. right-click a run) — currently the only access path is the
  new Artifact view mode (iframe) for the live page; the final report has no Ringside UI at all
  yet (by design the safest/lowest-risk piece was ringer.py-side rendering; a Ringside "open
  final report" button is a small, obvious follow-up once the 3 new commands are live-verified).
