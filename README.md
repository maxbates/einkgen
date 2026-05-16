# einkgen

A small AWS pipeline that generates (or accepts) images, dithers and resizes
them for an **Inkplate 10** 9.7" e-paper display, and publishes them to S3
where the device pulls the latest frame on its own schedule.

**Live example:** <https://einkgen.link/> — three read-only
tabs (Queue, History, Device) over a real dev deployment. History fills in
every 2 hours from the cron tick.

```
   CLI ──┐            ┌────────────────────┐                     ┌──────────────────┐
  cron ──┤  enqueue ─▶│  queue (S3 prefix) │── S3 ObjectCreated ▶│ generator Lambda │──┐
 future ─┘            └────────────────────┘    (concurrency=1)  └──────────────────┘  │
                              ▲                                                        │
                              │   read-only                                            ▼
                       ┌─────────────────┐    ◀── public reads ──            ┌────────────────┐
                       │  web app (SPA)  │    via read-api Lambda            │   S3 bucket    │
                       │  3 tabs, public │                                   │   + manifest   │
                       └─────────────────┘                                   └────────┬───────┘
                                                                                      │ CloudFront
                                                                                      ▼ HTTPS GET
                                                                              ┌──────────────┐
                                                                              │ Inkplate 10  │
                                                                              │   firmware   │
                                                                              └──────────────┘
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
ask you for the four things only a human can provide (AWS profile, OpenAI
key, device-status token, environment name), and then run the deploy
itself.

## Repo layout

```
src/einkgen/    Python: CLI + Lambda handlers + core image/queue/publish logic
web/            React + Vite SPA (the three-tab read-only dashboard)
firmware/       Inkplate 10 Arduino sketch
infra/          AWS CDK (one stack, three Lambdas, one bucket, one CloudFront)
tests/          pytest suite, moto-backed
```

## Status

v0.2.0.1 — first real deploy is live (see [CHANGELOG.md](CHANGELOG.md)).
The Inkplate firmware path is implemented and verified end-to-end via the
device-status API; the physical device hasn't shipped yet.
