# LanShot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and package one native macOS 13+ menu bar app that can run as either a main-display capture client or a LAN receiver with a one-button mobile web page.

**Architecture:** A root Swift Package contains a testable `LanShotCore` library and a thin SwiftUI `LanShot` executable. Capture and receiver runtimes depend on small protocols; the receiver uses SwiftNIO/NIOHTTP1 over Network.framework, while the client uses ScreenCaptureKit, Bonjour discovery, and structured HTTP requests. A release script assembles the SwiftPM executable into a signed `.app` bundle.

**Tech Stack:** Swift 6.3 toolchain in Swift 5 language mode, SwiftUI, AppKit, ScreenCaptureKit, Network.framework, ServiceManagement, SwiftNIO 2.101.3, NIO Transport Services 1.28.0, XCTest, SwiftPM, codesign.

---

## File Map

```text
Package.swift                                  SwiftPM products, targets, pinned packages
.gitignore                                     Build and release output exclusions
Packaging/Info.plist                           Final app bundle metadata and privacy strings
Sources/LanShot/LanShotApp.swift               SwiftUI menu bar entry point
Sources/LanShot/AppModel.swift                 Main-actor dependency assembly and view state
Sources/LanShot/Views/RoleSelectionView.swift  First-run role choice
Sources/LanShot/Views/MenuBarContentView.swift Role-specific compact UI
Sources/LanShot/Views/CaptureModeView.swift    Capture controls and status
Sources/LanShot/Views/ReceiverModeView.swift   Receiver URL, folder, and status
Sources/LanShot/LaunchAtLoginController.swift  SMAppService adapter
Sources/LanShotCore/Shared/AppRole.swift        Persisted capture/receiver role
Sources/LanShotCore/Shared/APIModels.swift      HTTP DTOs, states, and stable error codes
Sources/LanShotCore/Shared/RuntimeStatus.swift  User-visible runtime state
Sources/LanShotCore/Shared/Clock.swift          Injectable wall clock and sleeper
Sources/LanShotCore/Capture/CaptureContracts.swift Protocol boundaries and capture errors
Sources/LanShotCore/Capture/CaptureScheduler.swift Local interval scheduling
Sources/LanShotCore/Capture/CaptureCoordinator.swift Serialized capture/encode/upload pipeline
Sources/LanShotCore/Capture/ScreenCapturer.swift ScreenCaptureKit implementation
Sources/LanShotCore/Capture/JPEGEncoder.swift   ImageIO JPEG encoder at requested quality
Sources/LanShotCore/Capture/MacSessionMonitor.swift Lock, sleep, and display-state tracking
Sources/LanShotCore/Capture/ReceiverClient.swift Long poll, upload, failure report, retry
Sources/LanShotCore/Capture/BonjourReceiverDiscovery.swift NWBrowser receiver discovery
Sources/LanShotCore/Capture/CaptureModeService.swift Capture-mode lifecycle
Sources/LanShotCore/Receiver/CaptureTaskStore.swift Remote task state machine
Sources/LanShotCore/Receiver/ImageStore.swift Atomic storage, deduplication, retention
Sources/LanShotCore/Receiver/RetentionScheduler.swift Startup and 24-hour cleanup
Sources/LanShotCore/Receiver/ControlPage.swift Embedded responsive control page
Sources/LanShotCore/Receiver/ReceiverRouter.swift Typed route validation and responses
Sources/LanShotCore/Receiver/ReceiverHTTPHandler.swift NIO HTTP framing and body limits
Sources/LanShotCore/Receiver/ReceiverServer.swift NIOTS listener on port 8787
Sources/LanShotCore/Receiver/ReceiverModeService.swift Receiver-mode lifecycle
Tests/LanShotCoreTests/TestSupport/*            Manual clock, temporary directory, fakes
Tests/LanShotCoreTests/*Tests.swift             Focused unit and integration tests
scripts/build-app.sh                            Release build, bundle assembly, signing
scripts/verify.sh                               Deterministic local verification
README.md                                       Two-Mac setup and trusted-LAN warning
docs/manual-test-checklist.md                   Real-device acceptance checklist
```

## Dependency Order

```text
Task 1 -> Tasks 2, 3, 4 in parallel
Tasks 2 + 3 -> Task 5
Task 4 -> Task 6
Tasks 5 + 6 -> Task 7 -> Task 8
```

Agents must use the single existing worktree and `develop` branch. Parallel tasks may edit only their listed files. Commits are serialized and must stage explicit paths, never `git add -A`.

### Task 1: Package Skeleton and Shared Contracts

**Files:**
- Create: `Package.swift`
- Create: `.gitignore`
- Create: `Sources/LanShot/main.swift`
- Create: `Sources/LanShotCore/Shared/AppRole.swift`
- Create: `Sources/LanShotCore/Shared/APIModels.swift`
- Create: `Sources/LanShotCore/Shared/RuntimeStatus.swift`
- Create: `Sources/LanShotCore/Shared/Clock.swift`
- Create: `Tests/LanShotCoreTests/SharedContractsTests.swift`

- [ ] **Step 1: Write shared-contract tests**

```swift
import XCTest
@testable import LanShotCore

final class SharedContractsTests: XCTestCase {
    func testRolesRoundTripThroughRawValues() {
        XCTAssertEqual(AppRole(rawValue: "capture"), .capture)
        XCTAssertEqual(AppRole(rawValue: "receiver"), .receiver)
    }

    func testTaskResponseUsesStableWireValues() throws {
        let value = CaptureTaskResponse(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            status: .pending,
            expiresAt: Date(timeIntervalSince1970: 1_700_000_000),
            error: nil
        )
        let data = try APIJSON.encoder.encode(value)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["status"] as? String, "pending")
        XCTAssertEqual(json["id"] as? String, "00000000-0000-0000-0000-000000000001")
    }

    func testFailureCodesMatchHTTPContract() {
        XCTAssertEqual(Set(CaptureFailureCode.allCases.map(\.rawValue)), [
            "permission_required", "session_locked", "display_asleep",
            "capture_failed", "encoding_failed"
        ])
    }
}
```

- [ ] **Step 2: Create the package and verify the test fails because contracts do not exist**

```swift
// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "LanShot",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "LanShotCore", targets: ["LanShotCore"]),
        .executable(name: "LanShot", targets: ["LanShot"])
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", exact: "2.101.3"),
        .package(url: "https://github.com/apple/swift-nio-transport-services.git", exact: "1.28.0")
    ],
    targets: [
        .target(name: "LanShotCore", dependencies: [
            .product(name: "NIOCore", package: "swift-nio"),
            .product(name: "NIOHTTP1", package: "swift-nio"),
            .product(name: "NIOFoundationCompat", package: "swift-nio"),
            .product(name: "NIOTransportServices", package: "swift-nio-transport-services")
        ]),
        .executableTarget(name: "LanShot", dependencies: ["LanShotCore"]),
        .testTarget(name: "LanShotCoreTests", dependencies: [
            "LanShotCore",
            .product(name: "NIOEmbedded", package: "swift-nio")
        ])
    ],
    swiftLanguageModes: [.v5]
)
```

Create the bootstrap executable as:

```swift
import Foundation
print("LanShot development build")
```

Set `.gitignore` to:

```gitignore
.build/
.swiftpm/
dist/
.DS_Store
```

Run: `swift test --filter SharedContractsTests`

Expected: FAIL with missing `AppRole`, `CaptureTaskResponse`, or related symbols.

- [ ] **Step 3: Implement the shared types**

```swift
public enum AppRole: String, Codable, CaseIterable, Sendable {
    case capture
    case receiver
}

public enum CaptureTaskStatus: String, Codable, Sendable {
    case pending, running, completed, failed, expired
}

public enum CaptureFailureCode: String, Codable, CaseIterable, Sendable {
    case permissionRequired = "permission_required"
    case sessionLocked = "session_locked"
    case displayAsleep = "display_asleep"
    case captureFailed = "capture_failed"
    case encodingFailed = "encoding_failed"
}

public struct APIError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
}

public struct CaptureTaskResponse: Codable, Equatable, Sendable {
    public let id: UUID
    public let status: CaptureTaskStatus
    public let expiresAt: Date
    public let error: APIError?
}

public enum APIJSON {
    public static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()
    public static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}
```

Add these exact runtime and clock contracts:

```swift
public enum RuntimeStatus: Equatable, Sendable {
    case idle
    case ready
    case capturing
    case uploading
    case completed(Date)
    case receiverOffline
    case permissionRequired
    case macUnavailable(String)
    case uploadFailed(String)
}

public protocol WallClock: Sendable {
    var now: Date { get }
    func sleep(for seconds: TimeInterval) async throws
}

public struct SystemWallClock: WallClock {
    public var now: Date { Date() }
    public func sleep(for seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }
}
```

- [ ] **Step 4: Run shared tests and resolve dependencies**

Run: `swift test --filter SharedContractsTests`

Expected: 3 tests pass and `Package.resolved` pins SwiftNIO 2.101.3 and NIO Transport Services 1.28.0.

- [ ] **Step 5: Commit the skeleton**

```bash
git add Package.swift Package.resolved .gitignore Sources/LanShot/main.swift Sources/LanShotCore/Shared Tests/LanShotCoreTests/SharedContractsTests.swift
git commit -m "chore: scaffold LanShot core package"
```

### Task 2: Remote Capture Task State Machine

**Files:**
- Create: `Sources/LanShotCore/Receiver/CaptureTaskStore.swift`
- Create: `Tests/LanShotCoreTests/CaptureTaskStoreTests.swift`
- Create: `Tests/LanShotCoreTests/TestSupport/ManualClock.swift`

- [ ] **Step 1: Write failing state-machine tests**

Tests must construct `CaptureTaskStore(clock:uuid:)` with a fixed clock and UUID and assert:

```swift
let created = await store.createOrReuse()
XCTAssertEqual(created.status, .pending)
let reused = await store.createOrReuse()
XCTAssertEqual(reused.id, created.id)

let claimed = await store.claimNext()
XCTAssertEqual(claimed?.status, .running)
clock.advance(by: 59)
XCTAssertEqual((await store.task(id: created.id))?.status, .running)
clock.advance(by: 2)
XCTAssertEqual((await store.task(id: created.id))?.status, .expired)
XCTAssertEqual(await store.complete(id: created.id, path: "/late.jpg"), .gone)
```

Add separate tests for failure codes, unknown IDs, redelivery without deadline extension, completed upload idempotency, and forward-only terminal states.

- [ ] **Step 2: Run the focused tests and observe the missing store**

Run: `swift test --filter CaptureTaskStoreTests`

Expected: FAIL because `CaptureTaskStore` is undefined.

- [ ] **Step 3: Implement the actor with explicit transitions**

```swift
public actor CaptureTaskStore {
    public enum CompletionResult: Equatable, Sendable {
        case stored(CaptureTaskResponse)
        case duplicate(CaptureTaskResponse)
        case notFound
        case gone
    }

    public init(clock: any WallClock = SystemWallClock(), uuid: @escaping @Sendable () -> UUID = UUID.init)
    public func createOrReuse() -> CaptureTaskResponse
    public func claimNext() -> CaptureTaskResponse?
    public func task(id: UUID) -> CaptureTaskResponse?
    public func complete(id: UUID, path: String) -> CompletionResult
    public func fail(id: UUID, code: CaptureFailureCode, message: String) -> CompletionResult
}
```

Use one private active record. Creation sets `now + 60`; first claim changes pending to running and resets the deadline once to `now + 60`; redelivery returns the same running record without extending it. Every public method calls `expireIfNeeded()`. Expired, completed, and failed records never transition again.

- [ ] **Step 4: Run the task-store tests**

Run: `swift test --filter CaptureTaskStoreTests`

Expected: all state, expiry, redelivery, and idempotency tests pass.

- [ ] **Step 5: Commit the state machine**

```bash
git add Sources/LanShotCore/Receiver/CaptureTaskStore.swift Tests/LanShotCoreTests/CaptureTaskStoreTests.swift Tests/LanShotCoreTests/TestSupport/ManualClock.swift
git commit -m "feat: add capture task state machine"
```

### Task 3: Atomic Image Storage and Seven-Day Retention

**Files:**
- Create: `Sources/LanShotCore/Receiver/ImageStore.swift`
- Create: `Sources/LanShotCore/Receiver/RetentionScheduler.swift`
- Create: `Tests/LanShotCoreTests/ImageStoreTests.swift`
- Create: `Tests/LanShotCoreTests/RetentionSchedulerTests.swift`
- Create: `Tests/LanShotCoreTests/TestSupport/TemporaryDirectory.swift`

- [ ] **Step 1: Write failing storage tests**

Create a temporary root and assert that storing JPEG bytes `[0xFF, 0xD8, 0xFF, 0xD9]` at a fixed local date produces:

```text
<root>/2026-07-17/2026-07-17_09-30-00_00000000-0000-0000-0000-000000000001.jpg
```

Assert the same UUID returns the original URL, non-JPEG data throws `invalidJPEG`, data larger than 25 MB throws `tooLarge`, a file at exactly seven days remains, a file one second older is deleted, and empty date directories are removed.

- [ ] **Step 2: Run focused tests and observe missing storage types**

Run: `swift test --filter 'ImageStoreTests|RetentionSchedulerTests'`

Expected: FAIL because `ImageStore` and `RetentionScheduler` do not exist.

- [ ] **Step 3: Implement storage and cleanup**

```swift
public actor ImageStore {
    public static let maximumBytes = 25 * 1_024 * 1_024
    public init(root: URL, calendar: Calendar = .current, fileManager: FileManager = .default)
    public func storeJPEG(_ data: Data, captureID: UUID, now: Date) throws -> URL
    public func cleanup(now: Date) throws -> Int
}

public final class RetentionScheduler {
    public init(store: ImageStore, clock: any WallClock = SystemWallClock())
    public func start()
    public func stop()
}
```

Validate JPEG start bytes before directory creation. Search existing date directories for the UUID suffix to preserve idempotency after restart. Write with `Data.write(to:options:.atomic)`. Cleanup compares file modification dates against `now - 7 * 24 * 60 * 60`, then removes empty directories. The scheduler cleans immediately and then every 86,400 seconds until cancelled.

- [ ] **Step 4: Run storage tests**

Run: `swift test --filter 'ImageStoreTests|RetentionSchedulerTests'`

Expected: all storage, boundary, idempotency, and scheduling tests pass.

- [ ] **Step 5: Commit storage**

```bash
git add Sources/LanShotCore/Receiver/ImageStore.swift Sources/LanShotCore/Receiver/RetentionScheduler.swift Tests/LanShotCoreTests/ImageStoreTests.swift Tests/LanShotCoreTests/RetentionSchedulerTests.swift Tests/LanShotCoreTests/TestSupport/TemporaryDirectory.swift
git commit -m "feat: add atomic image storage and retention"
```

### Task 4: Capture Scheduling and Serialized Coordination

**Files:**
- Create: `Sources/LanShotCore/Capture/CaptureContracts.swift`
- Create: `Sources/LanShotCore/Capture/CaptureScheduler.swift`
- Create: `Sources/LanShotCore/Capture/CaptureCoordinator.swift`
- Create: `Tests/LanShotCoreTests/CaptureSchedulerTests.swift`
- Create: `Tests/LanShotCoreTests/CaptureCoordinatorTests.swift`
- Create: `Tests/LanShotCoreTests/TestSupport/CaptureFakes.swift`

- [ ] **Step 1: Write failing scheduler and coordinator tests**

Use fakes conforming to these boundaries:

```swift
public protocol ScreenCapturing: Sendable { func captureMainDisplay() async throws -> CGImage }
public protocol JPEGEncoding: Sendable { func encode(_ image: CGImage, quality: Double) throws -> Data }
public protocol CaptureUploading: Sendable {
    func upload(_ data: Data, captureID: UUID, remoteTaskID: UUID?) async throws
    func reportFailure(taskID: UUID, code: CaptureFailureCode, message: String) async
}
```

Assert the scheduler is disabled by default with a stored five-minute interval, supports only 1/5/10/30/60 minutes, and produces no duplicate timer after repeated starts. Assert the coordinator serializes simultaneous local and remote requests, encodes at `0.8`, reuses the remote task UUID as capture UUID, deduplicates redelivery, maps each `CaptureError` to its stable failure code, and continues after failure.

- [ ] **Step 2: Run focused tests**

Run: `swift test --filter 'CaptureSchedulerTests|CaptureCoordinatorTests'`

Expected: FAIL because capture contracts and implementations are missing.

- [ ] **Step 3: Implement scheduler and coordinator**

```swift
public enum CaptureInterval: Int, CaseIterable, Codable, Sendable {
    case one = 1, five = 5, ten = 10, thirty = 30, sixty = 60
}

public enum CaptureRequest: Sendable {
    case scheduled(UUID)
    case local(UUID)
    case remote(UUID)
}

public actor CaptureCoordinator {
    public init(capturer: any ScreenCapturing, encoder: any JPEGEncoding,
                uploader: any CaptureUploading)
    @discardableResult public func submit(_ request: CaptureRequest) async -> RuntimeStatus
}
```

Actor isolation alone does not serialize work across `await`. Maintain an internal FIFO of `(CaptureRequest, CheckedContinuation<RuntimeStatus, Never>)`, an `isDraining` flag, and exactly one drain task. `submit` appends and starts the drain only when idle; the drain removes one item, awaits the full capture/encode/upload pipeline, resumes that continuation, and then advances. Keep a bounded in-memory set of the last 100 completed remote UUIDs for redelivery deduplication. `CaptureScheduler` owns one cancellable task, calls an injected async handler, and restarts only when enabled or the interval changes.

- [ ] **Step 4: Run capture-core tests**

Run: `swift test --filter 'CaptureSchedulerTests|CaptureCoordinatorTests'`

Expected: all interval, serialization, quality, deduplication, error mapping, and recovery tests pass.

- [ ] **Step 5: Commit capture core**

```bash
git add Sources/LanShotCore/Capture/CaptureContracts.swift Sources/LanShotCore/Capture/CaptureScheduler.swift Sources/LanShotCore/Capture/CaptureCoordinator.swift Tests/LanShotCoreTests/CaptureSchedulerTests.swift Tests/LanShotCoreTests/CaptureCoordinatorTests.swift Tests/LanShotCoreTests/TestSupport/CaptureFakes.swift
git commit -m "feat: serialize scheduled and remote captures"
```

### Task 5: Receiver HTTP Service, Control Page, and Bonjour Publishing

**Files:**
- Create: `Sources/LanShotCore/Receiver/ControlPage.swift`
- Create: `Sources/LanShotCore/Receiver/ReceiverRouter.swift`
- Create: `Sources/LanShotCore/Receiver/ReceiverHTTPHandler.swift`
- Create: `Sources/LanShotCore/Receiver/ReceiverServer.swift`
- Create: `Sources/LanShotCore/Receiver/ReceiverModeService.swift`
- Create: `Tests/LanShotCoreTests/ReceiverRouterTests.swift`
- Create: `Tests/LanShotCoreTests/ReceiverHTTPHandlerTests.swift`
- Create: `Tests/LanShotCoreTests/ReceiverServerIntegrationTests.swift`

- [ ] **Step 1: Write route and framing tests**

Cover every contract path and status:

```text
GET  /                                      200 text/html
POST /api/v1/tasks                          201 new, 200 reused
GET  /api/v1/tasks/<uuid>                   200 or 404
GET  /api/v1/agent/next?timeout=25          200 or 204
POST /api/v1/tasks/<uuid>/image             201, 200 duplicate, 410 late
POST /api/v1/tasks/<uuid>/failure           200, 404 unknown, 410 late
POST /api/v1/images                         201, 200 duplicate
```

Use `NIOEmbedded.EmbeddedChannel` to send `.head`, `.body`, `.end`; assert response framing, `Content-Length`, JSON content type, 400 invalid UUID/JSON, 413 body over 25 MB, 415 wrong content type, and 500 messages without filesystem paths.

- [ ] **Step 2: Run receiver HTTP tests and observe missing routes**

Run: `swift test --filter 'ReceiverRouterTests|ReceiverHTTPHandlerTests|ReceiverServerIntegrationTests'`

Expected: FAIL because receiver HTTP types are missing.

- [ ] **Step 3: Implement the router and embedded page**

```swift
public struct HTTPResponse: Sendable {
    public let status: HTTPResponseStatus
    public let headers: HTTPHeaders
    public let body: ByteBuffer
}

public actor ReceiverRouter {
    public init(tasks: CaptureTaskStore, images: ImageStore, clock: any WallClock)
    public func route(method: HTTPMethod, uri: String, headers: HTTPHeaders,
                      body: ByteBuffer) async -> HTTPResponse
}
```

`ControlPage.html` is embedded as a dedicated multiline string in `ControlPage.swift`. It contains a responsive page title, one `立即截图` button, one status line, `fetch('/api/v1/tasks', {method:'POST'})`, and status polling every 500 ms until a terminal state. It has no history list, login, image preview, or external assets.

For `/api/v1/agent/next`, clamp `timeout` to `1...25` seconds and check `CaptureTaskStore.claimNext()` every 250 ms using the injected clock. Stop immediately when the request task is cancelled.

- [ ] **Step 4: Implement and integration-test the NIOTS server**

Use `NIOTSEventLoopGroup`, an `NWListener(using:.tcp,on:8787)` with `listener.service = .init(type:"_lanshot._tcp")`, and `NIOTSListenerBootstrap.withNWListener(listener)`. Configure NIOHTTP1 server pipelining and add `ReceiverHTTPHandler`. Route work and file I/O must run outside the event loop; return to the channel event loop only to write `.head`, `.body`, `.end`.

Run: `swift test --filter 'ReceiverRouterTests|ReceiverHTTPHandlerTests|ReceiverServerIntegrationTests'`

Expected: all route tests pass, and loopback flow task -> claim -> JPEG -> completed creates exactly one file.

- [ ] **Step 5: Commit receiver service**

```bash
git add Sources/LanShotCore/Receiver/ControlPage.swift Sources/LanShotCore/Receiver/ReceiverRouter.swift Sources/LanShotCore/Receiver/ReceiverHTTPHandler.swift Sources/LanShotCore/Receiver/ReceiverServer.swift Sources/LanShotCore/Receiver/ReceiverModeService.swift Tests/LanShotCoreTests/ReceiverRouterTests.swift Tests/LanShotCoreTests/ReceiverHTTPHandlerTests.swift Tests/LanShotCoreTests/ReceiverServerIntegrationTests.swift
git commit -m "feat: add LAN receiver service"
```

### Task 6: ScreenCaptureKit, Receiver Client, and Bonjour Discovery

**Files:**
- Create: `Sources/LanShotCore/Capture/ScreenCapturer.swift`
- Create: `Sources/LanShotCore/Capture/JPEGEncoder.swift`
- Create: `Sources/LanShotCore/Capture/MacSessionMonitor.swift`
- Create: `Sources/LanShotCore/Capture/ReceiverClient.swift`
- Create: `Sources/LanShotCore/Capture/BonjourReceiverDiscovery.swift`
- Create: `Sources/LanShotCore/Capture/CaptureModeService.swift`
- Create: `Tests/LanShotCoreTests/ScreenCaptureSupportTests.swift`
- Create: `Tests/LanShotCoreTests/ReceiverClientTests.swift`
- Create: `Tests/LanShotCoreTests/TestSupport/HTTPClientTransportFake.swift`

- [ ] **Step 1: Write client and capture-support tests**

Inject a fake `HTTPClientTransport` and assert: long poll decodes a task; 204 returns no task; remote image uses `/api/v1/tasks/<id>/image`; scheduled image uses `/api/v1/images` plus `X-LanShot-Capture-ID`; failure uses the stable JSON code; transport failures retry after 0.5, 1, and 2 seconds; 4xx terminal responses do not retry.

For capture support, extract pure helpers and assert `CGMainDisplayID()` matching chooses the correct display ID, locked/asleep state maps to the correct error, and incomplete SCStream frames are ignored.

- [ ] **Step 2: Run focused capture integration tests**

Run: `swift test --filter 'ScreenCaptureSupportTests|ReceiverClientTests'`

Expected: FAIL because system capture, discovery, and client types are missing.

- [ ] **Step 3: Implement system capture and session state**

```swift
public final class ScreenCapturer: ScreenCapturing, @unchecked Sendable {
    public init(session: MacSessionMonitor = .init())
    public func hasPermission() -> Bool
    @discardableResult public func requestPermission() -> Bool
    public func captureMainDisplay() async throws -> CGImage
}
```

Check `CGPreflightScreenCaptureAccess`, request with `CGRequestScreenCaptureAccess`, and then check active session state and `CGDisplayIsAsleep`. Match the `SCDisplay` for `CGMainDisplayID()`. On macOS 14+, call `SCScreenshotManager.captureImage`; on macOS 13, start `SCStream`, resume exactly one continuation on the first complete frame, and stop the stream. `MacSessionMonitor` observes workspace session, screen, and system sleep/wake notifications. Implement `ImageIOJPEGEncoder` with `CGImageDestination`, UTType JPEG, and the supplied quality value.

- [ ] **Step 4: Implement receiver discovery/client and run tests**

Use `NWBrowser(for:.bonjour(type:"_lanshot._tcp",domain:nil),using:.tcp)` and expose the selected `NWEndpoint`. Define `HTTPClientTransport.send(_ request: ClientHTTPRequest, to endpoint: NWEndpoint)`; the production NIOTS/NIOHTTP1 transport connects directly to the Bonjour service endpoint, so no IP or deprecated `NetService` resolution is required. Keep the HTTP result decoder separate from channel transport and use the tested three-attempt retry policy.

`CaptureModeService.start()` starts discovery, local scheduling, and a cancellable 25-second long-poll loop; `stop()` cancels all three. It never replays an expired task after wake.

Run: `swift test --filter 'ScreenCaptureSupportTests|ReceiverClientTests'`

Expected: all client, retry, state, and frame-selection tests pass.

- [ ] **Step 5: Commit capture integration**

```bash
git add Sources/LanShotCore/Capture/ScreenCapturer.swift Sources/LanShotCore/Capture/JPEGEncoder.swift Sources/LanShotCore/Capture/MacSessionMonitor.swift Sources/LanShotCore/Capture/ReceiverClient.swift Sources/LanShotCore/Capture/BonjourReceiverDiscovery.swift Sources/LanShotCore/Capture/CaptureModeService.swift Tests/LanShotCoreTests/ScreenCaptureSupportTests.swift Tests/LanShotCoreTests/ReceiverClientTests.swift Tests/LanShotCoreTests/TestSupport/HTTPClientTransportFake.swift
git commit -m "feat: connect capture Mac to LAN receiver"
```

### Task 7: Dual-Mode Menu Bar App

**Files:**
- Create: `Sources/LanShot/LanShotApp.swift`
- Delete: `Sources/LanShot/main.swift`
- Create: `Sources/LanShot/AppModel.swift`
- Create: `Sources/LanShot/Views/RoleSelectionView.swift`
- Create: `Sources/LanShot/Views/MenuBarContentView.swift`
- Create: `Sources/LanShot/Views/CaptureModeView.swift`
- Create: `Sources/LanShot/Views/ReceiverModeView.swift`
- Create: `Sources/LanShot/LaunchAtLoginController.swift`
- Create: `Tests/LanShotCoreTests/AppModeControllerTests.swift`
- Create: `Sources/LanShotCore/Shared/AppModeController.swift`

- [ ] **Step 1: Write failing role lifecycle tests**

Use fake `ModeRuntime` objects and assert first launch has no role, selecting a role persists it and starts that runtime, selecting the same role is idempotent, and switching roles awaits `stop()` before `start()`.

```swift
public protocol ModeRuntime: Sendable {
    func start() async throws
    func stop() async
}
```

- [ ] **Step 2: Implement and test `AppModeController`**

Run: `swift test --filter AppModeControllerTests`

Expected before implementation: FAIL for missing controller.

Implement a `@MainActor` observable controller with injected capture/receiver runtimes and a role persistence closure. Re-run the command; expected: all lifecycle tests pass.

- [ ] **Step 3: Build the compact SwiftUI scenes**

```swift
@main
struct LanShotApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        MenuBarExtra("LanShot", systemImage: model.menuBarSymbol) {
            MenuBarContentView(model: model)
                .frame(width: 320)
        }
        .menuBarExtraStyle(.window)
        Settings { RoleSelectionView(model: model).frame(width: 360) }
    }
}
```

Capture view contains receiver/permission status, interval picker, schedule toggle, `立即截图`, and latest result. Receiver view contains running status, selectable LAN URL, reveal-folder button, and latest receipt time. Role selection is a segmented control. Use SF Symbols for status and command icons, compact typography, no marketing copy, and no nested cards.

- [ ] **Step 4: Add login launch and production dependency assembly**

Wrap `SMAppService.mainApp.register()`/`unregister()` in `LaunchAtLoginController`. `AppModel` constructs the default `~/Pictures/LanShot` store, receiver server, task store, ScreenCapturer, receiver client, coordinator, and services. On launch it restores the role but never enables scheduled capture unless the stored toggle is true.

Run: `swift test && swift build`

Expected: tests pass and the `LanShot` executable builds with no errors.

- [ ] **Step 5: Commit the app UI**

```bash
git add Sources/LanShot Sources/LanShotCore/Shared/AppModeController.swift Tests/LanShotCoreTests/AppModeControllerTests.swift
git commit -m "feat: add dual-mode menu bar app"
```

### Task 8: Packaging, Documentation, and End-to-End Verification

**Files:**
- Create: `Packaging/Info.plist`
- Create: `scripts/build-app.sh`
- Create: `scripts/verify.sh`
- Create: `Tests/LanShotCoreTests/RemoteCaptureFlowTests.swift`
- Create: `README.md`
- Create: `docs/manual-test-checklist.md`

- [ ] **Step 1: Add the end-to-end test**

Start `ReceiverServer` on `127.0.0.1:0` with a temporary image root, use the real HTTP client plus a fake capturer, then assert:

```swift
let task = try await browser.createTask()
let delivered = try await client.nextTask(timeout: 1)
XCTAssertEqual(delivered?.id, task.id)
await coordinator.submit(.remote(task.id))
let terminal = try await browser.waitForTask(task.id)
XCTAssertEqual(terminal.status, .completed)
XCTAssertEqual(try temporaryJPEGFiles().count, 1)
```

Add cases for repeated browser clicks and redelivery of the same UUID; both must still create one file.

- [ ] **Step 2: Run the end-to-end test**

Run: `swift test --filter RemoteCaptureFlowTests`

Expected: remote task flow completes and exactly one JPEG exists.

- [ ] **Step 3: Add bundle metadata and build script**

`Info.plist` must set `CFBundleIdentifier=com.zhouguichao.LanShot`, `CFBundleExecutable=LanShot`, `LSMinimumSystemVersion=13.0`, `LSUIElement=true`, `NSScreenCaptureUsageDescription`, `NSLocalNetworkUsageDescription`, `NSBonjourServices=[_lanshot._tcp]`, and `NSAppTransportSecurity/NSAllowsLocalNetworking=true`.

`scripts/build-app.sh` runs a release arm64 build, creates `dist/LanShot.app/Contents/{MacOS,Resources}`, copies the executable and plist, chooses the first Apple Development identity or falls back to ad-hoc `-`, signs with hardened runtime, verifies the signature, and creates `dist/LanShot.zip`.

- [ ] **Step 4: Add deterministic verification and operator docs**

`scripts/verify.sh` runs:

```bash
swift test
swift build -c release --arch arm64
plutil -lint Packaging/Info.plist
scripts/build-app.sh
codesign --verify --deep --strict --verbose=2 dist/LanShot.app
```

README documents install on both Macs, first-run right-click Open if necessary, role selection, Screen Recording permission and app restart, macOS firewall/local-network prompts, receiver URL, storage path, seven-day deletion, and the trusted-LAN/no-port-forwarding constraint. The manual checklist covers two Macs plus mobile Safari, permission denial, receiver restart, Wi-Fi interruption, sleep/wake, lock/unlock, repeated clicks, and file cleanup.

- [ ] **Step 5: Run the full gate and commit delivery files**

Run: `bash scripts/verify.sh`

Expected: all tests pass; release build succeeds; `dist/LanShot.app` and `dist/LanShot.zip` exist; plist and code signature verify.

```bash
git add Packaging/Info.plist scripts/build-app.sh scripts/verify.sh Tests/LanShotCoreTests/RemoteCaptureFlowTests.swift README.md docs/manual-test-checklist.md
git commit -m "docs: add LanShot installation and release workflow"
```

## Final Manual Gate

- Install the same signed app on both Macs.
- Select Receiver mode on the second Mac and confirm port `8787` plus Bonjour advertisement.
- Select Capture mode on the first Mac, grant Screen Recording permission, and restart the app.
- From mobile Safari, open `http://<receiver-host>.local:8787`, tap once, and confirm one JPEG appears under the receiver's dated folder.
- Enable a one-minute schedule, wait for one upload, disable it, and confirm no further scheduled files appear.
- Confirm locked/asleep/offline states fail without capturing later after wake.
- Confirm files older than seven days are deleted while boundary files are retained.
