# LanShot P1 merge record - 2026-09-08

## Scope

- Target branch: `develop`.
- Upstream package: `LanShot_P1_实现与测试包_2026-09-07.zip`.
- Local P0 source: `04-面试au代码/screenshot-sender` (37 tests passing before merge).
- No services were started, stopped, configured, or deployed during this merge.

## Merge decisions

- Verified every upstream package file against `SHA256SUMS.txt`.
- Reconstructed and verified the uploaded baseline using `BASELINE_SHA256.json`.
- Compared the local P0 changes with both the baseline and P1 sources instead of overwriting the local tree.
- Selected P1's unified SQLite reliability implementation where it supersedes P0's pending-file and capture-ledger code.
- Preserved local-only audio source, entitlements, written prompt, and audio start/stop scripts.
- Did not import generated apps, compiled binaries, caches, local state, or credentials.
- Kept the original P1 validation artifacts for provenance; their Linux results are not treated as proof of this Mac merge.

## Verification

Run from `screenshot-sender`:

```sh
python3 smoke_p1.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
sh -n ./*.command
```

Results on this Mac:

- P1 safe smoke: passed; synthetic capture, loopback HTTP, stub model, no user state changed.
- Python tests: 136 run, 136 passed, 2 GUI tests skipped because this Python has no Tk runtime.
- Python compile and command-script syntax checks: passed.
- Existing Swift package tests: 81 run, 79 passed, 2 failed. The failures are in the pre-existing
  `ReceiverHTTPHandlerTests` timeout/thread-affinity case and `ReceiverRouterTests` control-page text assertion;
  no Swift source was changed by this merge.

GUI smoke tests are skipped when the selected Python has no Tk runtime. Real screenshot permissions,
LaunchAgent behavior, sleep/wake, external displays, model calls, and target-site compatibility remain unverified.
