# Ringer Live Artifacts — Design Plan

**Author:** aicred (Claude, MBP) — 2026-07-04, written during the aicred baseline-repair double swarm
**For:** Jon → Ringer repo (github.com/NateBJones-Projects/ringer, Lex maintains)
**Goal:** live, shareable visual status/review pages from Ringer runs with ZERO Anthropic tokens.
Reserve Fable/Claude for what it's uniquely good at (triage judgment, adversarial verification,
boss-level integration) and stop spending it on dashboard upkeep.

## Why

Tonight's data point: keeping a live status artifact updated through a swarm run cost a dedicated
Claude subagent ~90k+ tokens for what is, structurally, a template render of state Ringer already
has on disk. Meanwhile the compiled 211-recommendation review HTML from yesterday cost one Codex
writer worker ~284k OpenAI tokens. Both jobs are automatable inside Ringer at the right tier:

| Tier | Job | Engine | Marginal cost |
|------|-----|--------|---------------|
| 0 | Live swarm progress page | Pure Python (no LLM) | $0, forever |
| 1 | Synthesized review/report page (judgment content) | `codex exec`, cheap model | OpenAI tokens only |
| 2 | Hosting/sharing beyond the local machine | pluggable publish hook | whatever the hook costs |

## Tier 0 — zero-LLM live status artifact (the big win)

Ringer is zero-LLM orchestration; the status page should be too. Every fact a progress dashboard
shows already lives in the run state (`~/.ringer/runs/<run>.json`): task keys, status
(queued/running/pass/fail/retry), timings, check commands, retry counts, eval rows.

**Change:** add a `render_status_html(state) -> str` function (stdlib only, one big template
string) and call it at every point ringer.py flushes run state. Write the result next to the state
file AND to an optional configured path.

```toml
# config.toml
[artifact]
enabled = true
out = "~/.ringer/artifacts/{run_name}.html"   # {run_name}, {ts} substitutions
open_on_start = false                          # Ringside already opens; this is for extra displays
```

Page requirements:
- Self-contained single file (inline CSS, no CDN), dark/light aware, big status colors — built to
  look good on a screen recording.
- `<meta http-equiv="refresh" content="5">` — live-enough over file://, no server, no JS needed.
- Header: run name, identity, started, elapsed, N/M pass, parallel slots.
- One row per task: key, status chip, attempt count, duration, last check exit code; failed tasks
  expand to show the tail of the check output (Ringer already captures it for retry injection).
- Multi-run index: also render `~/.ringer/artifacts/index.html` listing active + recent runs, so
  two concurrent swarms (like tonight) get one pane of glass.

Acceptance: run `ringer.py demo`, open the artifact, watch it tick through pass/fail with zero
LLM calls (verify by network/token logs). Kill -9 the orchestrator mid-run; page must show last
known state, not corrupt.

## Tier 1 — cheap-LLM report artifact (judgment content)

Progress is deterministic; *synthesis* ("what did 23 scouts actually find, ranked") needs a model.
Keep it off Anthropic by making it a first-class Ringer feature instead of a hand-rolled final
task:

```json
"reporter": {
  "spec": "Read every tasks/*/report.md under {workdir}. Produce ./report.html: single-file, ...",
  "model": "gpt-5.1-codex-mini",
  "when": "on_complete"        // or "rolling" — rerun after every task completion
}
```

**Changes:**
1. Optional `reporter` block in the manifest. Ringer appends it as a synthetic final task whose
   taskdir can read all sibling taskdirs; check = `test -s report.html`.
2. Per-task model override while you're in there: `"engine_args": ["-m", "gpt-5.1-codex-mini"]`
   (or a `model` key mapped to codex profiles). Scouts/writers rarely need xhigh; this is the
   other big cost lever. Default stays the config-level profile.
3. `"when": "rolling"` mode: rerun the reporter after each task completes (cheap model, capped to
   one in flight, debounced ~60s). Gives a *live* narrative page for long runs.

Cost note: mini-tier reporter rerun 10x over a run ≈ still cheaper than one Sonnet dashboard
session, and $0 Anthropic either way.

## Tier 2 — publish hook (sharing)

Local file covers Ringside + screen recording. For sharing (phone, another box, a teammate),
don't build hosting into Ringer — add one hook:

```toml
[artifact]
publish_cmd = "rsync -q {path} jon@fleetbox:/srv/ringer/"   # or scp, or a Convex upload script
```

Run it (best-effort, non-blocking, log-don't-fail) after each artifact write. Jon's existing
options slot in without Ringer knowing about them: LEJ/Convex hosting, the NAS, or nothing.
Claude-hosted Artifacts stay available for boss-narrated sessions, but are never *required*.

## What stays with Claude/Fable

- Triage and accept/reject judgment on findings (tonight: 211 recs → 150/20/15 with two
  scout claims overturned by boss verification — that's the layer worth Fable tokens).
- Adversarial review of worker diffs before commit.
- Anything user-facing that needs Jon's voice.

Boss-Claude's dashboard duty shrinks to: send milestone facts to nobody — the Tier 0 page reads
state directly; Tier 1 reads reports. No LLM in the loop for "keeping Jon visually up to date."

## Suggested build order (each independently shippable)

1. Tier 0 renderer + config block (pure win, ~150 lines, no new deps).
2. Multi-run index page.
3. Per-task `engine_args`/model override (cost lever beyond artifacts).
4. `reporter` block, `on_complete` mode.
5. `rolling` mode + debounce.
6. `publish_cmd` hook.

## Gotchas for the implementer

- Never render secrets: task specs can contain paths/keys; the status page should show spec
  *previews* truncated at ~200 chars and full check output only for FAILED tasks.
- Atomic writes (`tmp` + `os.replace`) so the meta-refresh browser never reads a half-written file.
- Keep the renderer synchronous and cheap; it runs inside the state-flush path.
- The reporter task must be excluded from pass/fail retry accounting for the *run verdict* of the
  real tasks (a pretty report failing shouldn't mark a green run red — log it, don't gate on it).
