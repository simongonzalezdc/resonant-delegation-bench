# Delegation Bench — ResonantOS add-on

Which of your real jobs can you safely hand to a local model — and walk away?
This add-on wraps [delegation-bench](https://github.com/KyaniteLabs/delegation-bench)
(MIT), the pre-registered delegation benchmark: 9 job classes, certified
confidence floors printed on every result (15/15 = at least 82%; 30/30 = at
least 90%), a blinded dual-judge protocol, hidden-test oracles, a sabotage
safety cell, and a walk-away autonomy tier. No leaderboards, no cloud — the
benchmark runs against YOUR local model endpoint and writes plain files you
own.

The engine is vendored byte-identical under `vendor/` (hash-pinned by tests);
the wrapper is Python 3.10+ standard library only.

## What it does

- `delegationbench.status` — version, busy state, and the fixture cells available.
- `delegationbench.run` — start a benchmark job: a named cell, a trial count
  (1..30), and either `mode: mock` (deterministic harness self-test, no
  network) or `mode: http` with your local endpoint URL (loopback only unless
  you opt in with `DELEGATIONBENCH_ALLOW_REMOTE=1`). Returns a run id
  immediately; runs take minutes.
- `delegationbench.results` — the decision verdict (RED / YELLOW / ESCALATE /
  CAPABILITY-GREEN / AUTONOMY-GREEN per the pre-registered table), the
  certified card path, and the trial rows.

One run at a time. Everything lands under `var/<run_id>/` (`decision.json`,
`rows.json`, `card.md`) with workspaces redacted and home paths replaced by `~`.

The mock mode exercises the full grading pipeline against a deterministic
stub product — expect honest RED verdicts there; it proves the harness judges,
not that the model works.

## Running it

    python3 server.py          # listens on http://127.0.0.1:4890 (the manifest entrypoint)

    curl -s http://127.0.0.1:4890/health
    curl -s -X POST http://127.0.0.1:4890/ -H 'Content-Type: application/json' \
      -d '{"method":"delegationbench.run","params":{"cell":"C1-S","mode":"mock","trials":5}}'
    curl -s -X POST http://127.0.0.1:4890/ -H 'Content-Type: application/json' \
      -d '{"method":"delegationbench.results"}'

Environment: `DELEGATIONBENCH_ALLOW_REMOTE=1` (opt-in for non-loopback
endpoints), `DELEGATIONBENCH_CALL_TIMEOUT` (per-request seconds, default 600),
`DELEGATIONBENCH_JOB_CAP` (wall-clock seconds, default 3600), and
`DELEGATIONBENCH_PORT` (dev only — the manifest declares 4890).

## Grading execution note

The service layer spawns nothing and accepts no adapter configs. The vendored
hidden-test oracle does execute submitted code under python as its grading
function — the code it runs is the model's output for the task, against
fixture-defined tests, inside per-trial workspaces under `var/`.

## Tests

    python3 -m unittest discover -s tests            # wrapper suite (17 tests)
    python3 vendor/tests/test_core.py                # upstream suite (30 checks)
    python3 vendor/tests/test_adversarial.py         # upstream adversarial (7 checks)
    sh run-validator-check.sh <path-to-2.0.0-alpha-clone>   # manifest vs the real validator

`vendor/` is hash-pinned to upstream; a wrapper test fails loudly on drift.
The sealed holdout set is NOT and will never be in this repo (that is what
keeps green honest — see the upstream DESIGN.md).

## License

MIT — see LICENSE. The vendored delegation-bench engine is MIT, KyaniteLabs.
