# Tour — start here

This is a private fork of [NateBJones-Projects/ringer](https://github.com/NateBJones-Projects/ringer):
a verified-swarm orchestrator (one frontier model directs; cheap workers type; every task is
verified by an EXECUTED check, never by the worker's own claim). The fork adds local hardening
and — the part a public repo can't have — the operational record of running it for real.

## Run it on your machine

Everything the tool needs is in this repo. You supply the per-user layer:

1. **Python 3.12+** and git. `ringer.py` is stdlib-only — no pip install.
2. **At least one worker engine, authenticated on your accounts.** The simplest first lane is
   the Codex CLI (`npm install -g @openai/codex`, then `codex login` on a ChatGPT plan).
   Alternatives, each with its own auth: OpenCode + an OpenRouter API key, or the Grok CLI on
   a SuperGrok plan. Ringer orchestrates and verifies; the engines bill separately.
3. **Your config:** `cp config.sample.toml ~/.config/ringer/config.toml` and edit — the sample
   is heavily commented. Pin `model_default` for any engine you use: unpinned models leave
   unattributable rows in the eval log (the lint will warn you).
4. Smoke-test without any engine: `./ringer.py demo` runs a mock swarm end-to-end.
   Full suite: `python3 -m unittest discover -s tests -t tests` (~330 tests, ~30s).
5. **Claude Code integration:** `./ringer.py install-agent` installs the orchestrator skill and
   nudge hooks so your sessions know the playbook. The live dashboard is `./ringer.py hud`.

## The blueprint (read in this order)

| where | what it gives you |
|---|---|
| [`.claude/skills/ringer/SKILL.md`](.claude/skills/ringer/SKILL.md) | The orchestrator playbook — routing rules, swarm patterns, when to delegate |
| [`docs/MODEL-NOTES.md`](docs/MODEL-NOTES.md) | The judgment layer: dated lessons from every real run — which models earn which lanes, check-craft traps, scoreboard corrections. The behind-the-scenes of the family-board project lives here |
| [`templates/`](templates/) | Portable manifest kits (review swarm, fix swarm, bakeoff) — start from these |
| [`config.sample.toml`](config.sample.toml) | Every engine lane, documented |
| [`docs/`](docs/) | STEERING (per-model steering profiles), TAXONOMY (model identity discipline), integrations/ (how this plugs into a wider agentic OS) |
| root `*.json` manifests | Real production manifests — read as worked examples; their absolute paths are machine-specific, so don't run them verbatim |
| PRs #20, #22, #29, #30 | The verification-integrity arc: why a FAIL isn't always the model's fault, failure attribution, and the head-capture + worktree_ref hardening |

## What is NOT here

Eval logs, worker transcripts, and run state live in `~/.ringer/` (never committed).
The projects the manifests orchestrate (the family board, the knowledge-OS repos) live in
their own repositories — this repo tells their orchestration story, not their source.
