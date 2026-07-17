# LanShot Design

## Goal

Build one native macOS 13+ app that runs in either **Capture** or **Receiver** mode. One Mac captures its main display on a schedule or on demand; another Mac on the same trusted LAN receives and stores the JPEG files. A phone or Mac browser can request an immediate screenshot.

## Scope

- One capture Mac and one receiver Mac.
- Main display only.
- Configurable scheduled capture plus remote immediate capture.
- Receiver stores files by date and deletes files older than seven days.
- A minimal browser page with one capture button and current request status.
- No account, password, history gallery, cloud service, TLS, multi-display capture, or public internet access.

## App Experience

The same app bundle is installed on both Macs. On first launch, the user chooses **Capture mode** or **Receiver mode**. The role can later be changed in Settings; changing it stops the current mode before starting the other one.

The app lives in the menu bar and exposes a compact status panel.

Capture mode shows:

- Receiver discovery and connection status.
- Screen Recording permission status.
- Schedule toggle and interval choices: 1, 5, 10, 30, or 60 minutes. The default interval is 5 minutes, but scheduling is off until explicitly started.
- A **Capture Now** command and the latest upload result.

Receiver mode shows:

- Server running status.
- The LAN URL to open on a phone or Mac.
- The storage folder and a command to reveal it in Finder.
- The latest received-file time, without image previews or history browsing.

Both modes offer an optional **Launch at Login** toggle.

## Architecture

The app is written in Swift and SwiftUI. Screen capture uses ScreenCaptureKit. The receiver embeds SwiftNIO with `NIOHTTP1`, so HTTP framing and parsing are handled by a maintained library rather than custom string parsing. It listens on port `8787`. Bonjour advertises and discovers a `_lanshot._tcp` service so the capture Mac does not require a fixed IP address.

Capture mode has four isolated components:

1. `ScreenCapturer` checks permission and session/display state, then captures the main display.
2. `CaptureScheduler` owns the local interval and never performs capture itself.
3. `ReceiverClient` discovers the receiver, long-polls for remote tasks, and uploads JPEG data.
4. `CaptureCoordinator` serializes scheduled, local, and remote requests through one capture/upload pipeline.

Receiver mode has four isolated components:

1. `ReceiverServer` serves the control page and JSON/upload endpoints.
2. `CaptureTaskStore` owns one-at-a-time remote tasks, UUIDs, expiry, and status.
3. `ImageStore` validates uploads, writes them atomically, and performs retention cleanup.
4. `BonjourService` publishes the receiver endpoint.

## Data Flow

For a scheduled screenshot, the local scheduler asks `CaptureCoordinator` to capture, encode, and upload.

For a remote screenshot:

1. The browser sends `POST /api/v1/tasks` to the receiver.
2. If no task is active, the receiver creates a UUID task with a 60-second pending deadline. A second click while a task is pending or running returns the existing task ID instead of creating a queue.
3. The capture app receives it through a 25-second HTTP long poll.
4. The coordinator checks permission, active session, and display state.
5. ScreenCaptureKit captures the main display and encodes it as JPEG at 0.8 quality.
6. The capture app uploads the JPEG with the task UUID. Scheduled and local captures generate a capture UUID and use the direct-image endpoint without creating a remote task.
7. The receiver atomically stores the file and marks the task complete.
8. The browser polls task status and shows success or a concise failure reason.

Only one capture runs at a time. The receiver may redeliver the same active task after a dropped long poll; the capture coordinator deduplicates it by UUID. Uploading an already completed UUID returns the existing result and cannot save a second file.

## HTTP Contract

All API responses are JSON except JPEG uploads and empty long-poll responses.

- `POST /api/v1/tasks` creates a remote task and returns `201` with `{id, status, expiresAt}`. If a task is already pending or running, it returns `200` with that task instead of creating another one.
- `GET /api/v1/tasks/<id>` returns `200` with a status of `pending`, `running`, `completed`, `failed`, or `expired`; an unknown ID returns `404`.
- `GET /api/v1/agent/next?timeout=25` long-polls for work. It returns `200` with `{id, expiresAt}` or `204` when there is no task. First delivery changes `pending` to `running` and sets one 60-second result deadline; redelivery never extends it.
- `POST /api/v1/tasks/<id>/image` accepts a remote task JPEG, stores it, and returns `201`; a duplicate completed task returns `200` with the original result.
- `POST /api/v1/tasks/<id>/failure` accepts `{code, message}` and marks the task failed. Stable codes are `permission_required`, `session_locked`, `display_asleep`, `capture_failed`, and `encoding_failed`.
- `POST /api/v1/images` accepts a scheduled or local JPEG with an `X-LanShot-Capture-ID` UUID header and returns `201`; duplicate UUIDs return `200` without creating another file.

Invalid JSON, UUIDs, content types, or request sizes return `400`, `415`, or `413` as appropriate. Unexpected server failures return `500` without exposing local paths.

Task states only move forward: `pending -> running -> completed|failed|expired`, or `pending -> expired`. When a deadline passes, the receiver marks the task `expired`. Any image or failure received afterward returns `410 Gone`, is not stored, and cannot change the terminal state.

## Capture Compatibility

- On macOS 14+, use `SCScreenshotManager` for one-shot capture.
- On macOS 13, use `SCStream` and stop after the first complete frame.
- Match `CGMainDisplayID()` to the corresponding ScreenCaptureKit display.
- Screen Recording permission must be granted locally and cannot be bypassed remotely.
- When the user session is locked or the display is asleep, capture does not run and the app reports a stable failure code while its process and network remain active. During system sleep, the 60-second task expires, the browser shows "Capture Mac offline or unavailable," and the task is never executed after wake.
- The app never prevents system sleep.

## Network and Storage

The receiver listens only for LAN use and is advertised with Bonjour. The browser page uses same-origin JSON requests. There is deliberately no authentication, so documentation must state that the app is for a trusted private network only and the receiver port must not be forwarded to the internet.

Files are stored under:

`~/Pictures/LanShot/YYYY-MM-DD/YYYY-MM-DD_HH-mm-ss_<capture-id>.jpg`

The server generates filenames and accepts only `image/jpeg` uploads up to 25 MB. A remote task ID is also its capture ID; scheduled and local captures provide their generated capture ID. Dates and filenames use the receiver Mac's local time zone. The server writes to a temporary file and atomically renames it after success. Cleanup runs at receiver startup and every 24 hours while active, deleting files whose modification time is earlier than exactly seven days before cleanup, then removing empty date directories.

Uploads retry up to three times with short exponential backoff. Version 1 has no persistent offline upload queue; a failed scheduled capture is reported and the next interval proceeds normally.

## Error Handling

User-visible states are limited to: ready, capturing, uploading, completed, receiver offline, permission required, Mac unavailable, and upload failed. Errors are logged locally without screenshot contents. Remote tasks expire instead of executing unexpectedly after a long sleep or outage.

## Testing

- Unit tests for interval scheduling, request serialization, task expiry/idempotency, upload validation, filename generation, and seven-day cleanup.
- Integration tests with a fake capturer covering browser request through atomic file storage.
- Real-device checks on two macOS 13+ Macs and mobile Safari for permission denial, receiver restart, Wi-Fi interruption, sleep/wake, lock/unlock, repeated clicks, and firewall prompts.

## Delivery

The project will include a Swift Package executable target that opens directly in Xcode, test targets, a script that assembles and signs `LanShot.app`, a concise setup guide, and a development build suitable for installing on the two private Macs. The first release favors direct installation and simple LAN operation over App Store distribution.
