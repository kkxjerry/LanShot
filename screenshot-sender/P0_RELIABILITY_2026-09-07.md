# LanShot P0 reliability changes — 2026-09-07

## Scope

1. End-to-end trace events using the same capture/task ID.
2. Persist captured images until receiver acknowledgement.
3. Retry pending uploads after transient failure and process restart.
4. Receiver-side capture-ID deduplication with a persistent ledger.
5. Receiver `/api/health` plus sender/startup readiness checks.

## Failure modes covered

- Receiver connection refused after a successful screenshot no longer deletes the screenshot.
- A restarted sender scans `spool_dir` for `*.pending.json` and retries the same capture ID.
- Duplicate `X-LanShot-Capture-ID` submissions do not start a second analysis.
- Receiver processing state and staged capture are persisted so an interrupted processing item can be recovered on receiver restart.
- Startup scripts no longer print success when receiver readiness or sender startup fails.
- Busy hotkey triggers are explicitly logged instead of being silently dropped.

## Trace chain

`HOTKEY_RECEIVED -> MANUAL_CAPTURE_START -> CAPTURE_START -> CAPTURE_OK -> QUEUE_PERSISTED -> UPLOAD_START -> HTTP_REQUEST_* -> UPLOAD_RECEIVED -> ANALYSIS_START -> ANALYSIS_DONE -> TASK_DONE`

Failure events include stage, exception type, retry status, HTTP status when available, screenshot return code/stderr, and elapsed time.

## Verification

- Python compile checks: `sender_service.py`, `receiver_service.py`, tests.
- Full unittest suite: 37 tests passing after the final P0 implementation.
- `zsh -n start_written.command start_assessment.command` passing.

## Known boundaries

- Readiness verifies the screenshot executable exists and is executable; it does not take a real screenshot at startup, so macOS Screen Recording permission still has to be proven by a real capture.
- The project directory currently contains no `.git` metadata, so these changes cannot be committed to a `develop` branch from this checkout.
- Automatic retry is intentionally focused on preserving work and retrying the existing upload path; alternate screenshot backends and platform-specific compatibility fallbacks are not part of P0.
