# einkgen

A small AWS pipeline that generates (or accepts) images, dithers and resizes
them for an **Inkplate 10** 9.7" e-paper display, and publishes them to S3
where the device pulls the latest frame on its own schedule.

**Live example:** <https://einkgen.link/> — three read-only public tabs
(Queue, History, Device) plus a password-gated Admin tab for submitting
prompts or photos from a phone or laptop. The Queue tab shows both the
pending prompts and the pre-rendered **Up next** buffer. New frames land
every 30 minutes from the cron tick, or on demand when the Inkplate's
WAKE button is pressed.

```
   CLI ──┐       ┌──────────────┐ cron / render_one ┌──────────────────┐ buffer_item
  email ─┤enqueue│ prompt queue │ ────────────────▶ │ generator Lambda │ ─────────┐
  admin ─┘       │  (queue/*)   │                   │ (concurrency=1)  │           │
  cron ─────────▶└──────────────┘                   └──────────────────┘           ▼
                                                                       ┌────────────────────┐
                                                advance / pop on  ◀── │  generated queue   │
                                                POST /wake             │  (generated/*)     │
                                                                       └────────┬───────────┘
                                                                                │ set_current
                                                                                ▼
                                                                        ┌────────────────┐
                          ┌─────────────────┐  reads via read-api      │   S3 bucket    │
                          │  web app (SPA)  │ ◀──────────────────────  │   + manifest   │
                          │  3 tabs + Admin │                          └────────┬───────┘
                          └─────────────────┘                                   │ CloudFront
                                                                                ▼ HTTPS GET
                                                                        ┌──────────────┐
                                                                        │ Inkplate 10  │ wake → POST /wake
                                                                        │   firmware   │     → fetch manifest
                                                                        └──────────────┘     → draw if changed
```

## Docs

- **[QUICKSTART.md](QUICKSTART.md)** — deploy einkgen to your own AWS
  account. Front-loaded human steps + a runbook designed for an AI agent
  to execute.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — what the system is: target
  device, data flow, queue design, image pipeline, AWS topology, and the
  threat model.
- **[PLAN.md](PLAN.md)** — why it's shaped that way: implementation order,
  locked-in decisions, open questions.
- **[TODOS.md](TODOS.md)** — open follow-up work, by priority.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.
- **[CLAUDE.md](CLAUDE.md)** — orientation for AI coding agents.
  Auto-loaded by Claude Code; safe to read as a human too.

## Deploying with an AI agent

The fastest path is to open this repo in [Claude Code](https://claude.com/claude-code)
(or any agent that can read `CLAUDE.md`), and say:

> Deploy einkgen to my AWS account. Walk me through what I need first.

The agent will read [CLAUDE.md](CLAUDE.md) + [QUICKSTART.md](QUICKSTART.md),
ask you for the things only a human can provide (AWS profile, OpenAI key,
device-status token, admin password, environment name), and then run the
deploy itself.

## Repo layout

```
src/einkgen/    Python: CLI + Lambda handlers + core image/queue/publish logic
web/            React + Vite SPA (read-only public tabs + admin tab)
firmware/       Inkplate 10 Arduino sketch
infra/          AWS CDK (one stack, four Lambdas, one bucket, one CloudFront)
shortcuts/      iPhone / Siri shortcut walkthroughs (email + HTTP paths)
tests/          pytest suite, moto-backed
```

## Status

v0.2.0.1 — first real deploy is live (see [CHANGELOG.md](CHANGELOG.md)).
The Inkplate firmware path is implemented and verified end-to-end via the
device-status API; the physical device hasn't shipped yet.
