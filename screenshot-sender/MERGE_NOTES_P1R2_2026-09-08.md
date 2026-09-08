# LanShot P1R2 merge record - 2026-09-08

## Scope

- Target branch: `develop`.
- Upstream package: `LanShot_P1_修订_原生显示与多接收服务_2026-09-08.zip`.
- Previous repository state: P1 at `770843a`, with local audio and written-mode sources retained.
- The running P1 services were stopped before replacing runtime modules.

## Merge decisions

- Verified every packaged file against the root `SHA256SUMS.txt`.
- Accepted R2 replacements for the P1 runtime, diagnostics, tests, scripts, and validation evidence.
- Restored the native `capture-exclusion-demo` source and removed the obsolete Tk launcher and Tk tests.
- Preserved repository-only audio source, audio scripts, `written_prompt.txt`, and the prior merge records.
- Did not copy generated apps, compiled binaries, caches, screenshots, runtime databases, or credentials.
- Kept the existing disabled P1 state directory unchanged; R2 must use an explicit compatible configuration.

## Local verification

- Isolated upstream Python suite: 197 passed.
- Native AppKit source: Swift parse, compile, link, and ad-hoc signing passed in an isolated directory.
- Merged working tree: 197 Python tests passed; `smoke_p1.py` and `smoke_multi.py` passed without
  desktop capture, real model calls, or user-state modification.
- Merged Python compile, command-script syntax, AppKit compile/link/sign, and package checksums passed.
- A temporary redundant-mode configuration produced the expected four-role dry run: primary receiver,
  backup receiver, sender, and display bridge.
- The first real startup exposed a trailing-newline prompt signature mismatch: embedded mode ran, but
  both HTTP receivers correctly refused the shared store. Prompt canonicalization and redundant-node
  readiness checks were added, with regression coverage. The local suite now runs 199 passing tests.
- Real screenshots, model calls, Input Monitoring, Screen Recording, launchd failover, and target-site behavior were not exercised during merge.
