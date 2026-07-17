import Foundation
import Network
import XCTest
@testable import LanShotCore

final class ReceiverClientTests: XCTestCase {
    private let endpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: 8_787)
    private let taskID = UUID(uuidString: "00000000-0000-0000-0000-000000000601")!

    func testNextTaskUsesTwentyFiveSecondLongPollAndDecodesTask() async throws {
        let expiresAt = Date(timeIntervalSince1970: 1_700_000_060)
        let body = try APIJSON.encoder.encode(RemoteCaptureTask(id: taskID, expiresAt: expiresAt))
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 200, body: body))
        ])
        let client = ReceiverClient(endpoint: endpoint, transport: transport)

        let task = try await client.nextTask()

        XCTAssertEqual(task, RemoteCaptureTask(id: taskID, expiresAt: expiresAt))
        let requests = await transport.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0].method, .get)
        XCTAssertEqual(requests[0].path, "/api/v1/agent/next?timeout=25")
        XCTAssertTrue(requests[0].body.isEmpty)
    }

    func testNextTaskReturnsNilForNoContent() async throws {
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 204))
        ])
        let client = ReceiverClient(endpoint: endpoint, transport: transport)

        let task = try await client.nextTask()

        XCTAssertNil(task)
    }

    func testRemoteUploadUsesTaskImageEndpointAndJPEGBody() async throws {
        let image = Data([0xff, 0xd8, 0xff])
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 201))
        ])
        let client = ReceiverClient(endpoint: endpoint, transport: transport)

        try await client.upload(image, captureID: taskID, remoteTaskID: taskID)

        let recordedRequests = await transport.requests
        let request = try XCTUnwrap(recordedRequests.first)
        XCTAssertEqual(request.method, .post)
        XCTAssertEqual(request.path, "/api/v1/tasks/\(taskID.uuidString.lowercased())/image")
        XCTAssertEqual(request.header(named: "Content-Type"), "image/jpeg")
        XCTAssertNil(request.header(named: "X-LanShot-Capture-ID"))
        XCTAssertEqual(request.body, image)
    }

    func testDirectUploadUsesImagesEndpointAndCaptureIDHeader() async throws {
        let image = Data([1, 2, 3])
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 201))
        ])
        let client = ReceiverClient(endpoint: endpoint, transport: transport)

        try await client.upload(image, captureID: taskID, remoteTaskID: nil)

        let recordedRequests = await transport.requests
        let request = try XCTUnwrap(recordedRequests.first)
        XCTAssertEqual(request.path, "/api/v1/images")
        XCTAssertEqual(request.header(named: "X-LanShot-Capture-ID"), taskID.uuidString.lowercased())
        XCTAssertEqual(request.body, image)
    }

    func testFailureReportUsesStableCodeJSON() async throws {
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 200))
        ])
        let client = ReceiverClient(endpoint: endpoint, transport: transport)

        await client.reportFailure(taskID: taskID, code: .sessionLocked, message: "Locked")

        let recordedRequests = await transport.requests
        let request = try XCTUnwrap(recordedRequests.first)
        XCTAssertEqual(request.path, "/api/v1/tasks/\(taskID.uuidString.lowercased())/failure")
        XCTAssertEqual(request.header(named: "Content-Type"), "application/json")
        XCTAssertEqual(
            try APIJSON.decoder.decode(APIError.self, from: request.body),
            APIError(code: "session_locked", message: "Locked")
        )
    }

    func testTransportFailuresRetryThreeTimesWithExponentialDelays() async throws {
        let transport = HTTPClientTransportFake(outcomes: [
            .failure(HTTPClientTransportStubError.offline),
            .failure(HTTPClientTransportStubError.offline),
            .failure(HTTPClientTransportStubError.offline),
            .response(ClientHTTPResponse(statusCode: 201))
        ])
        let clock = RecordingHTTPClientClock()
        let client = ReceiverClient(endpoint: endpoint, transport: transport, clock: clock)

        try await client.upload(Data([1]), captureID: taskID, remoteTaskID: nil)

        let requestCount = await transport.requests.count
        XCTAssertEqual(requestCount, 4)
        XCTAssertEqual(clock.durations, [0.5, 1, 2])
    }

    func testServerFailuresRetryButClientErrorsDoNot() async throws {
        let retryingTransport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 503)),
            .response(ClientHTTPResponse(statusCode: 201))
        ])
        let retryClock = RecordingHTTPClientClock()
        let retryingClient = ReceiverClient(endpoint: endpoint, transport: retryingTransport, clock: retryClock)
        try await retryingClient.upload(Data([1]), captureID: taskID, remoteTaskID: nil)
        let retryingRequestCount = await retryingTransport.requests.count
        XCTAssertEqual(retryingRequestCount, 2)
        XCTAssertEqual(retryClock.durations, [0.5])

        let terminalTransport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 400, body: Data("/private/secret".utf8)))
        ])
        let terminalClock = RecordingHTTPClientClock()
        let terminalClient = ReceiverClient(endpoint: endpoint, transport: terminalTransport, clock: terminalClock)
        do {
            try await terminalClient.upload(Data([1]), captureID: taskID, remoteTaskID: nil)
            XCTFail("Expected a terminal client error")
        } catch {
            XCTAssertEqual(error as? ReceiverClientError, .requestRejected(statusCode: 400))
            XCTAssertFalse(String(describing: error).contains("secret"))
        }
        let terminalRequestCount = await terminalTransport.requests.count
        XCTAssertEqual(terminalRequestCount, 1)
        XCTAssertTrue(terminalClock.durations.isEmpty)
    }

    func testCancellationDoesNotRetryLongPoll() async {
        let transport = HTTPClientTransportFake(outcomes: [.waitForCancellation])
        let clock = RecordingHTTPClientClock()
        let client = ReceiverClient(endpoint: endpoint, transport: transport, clock: clock)
        let poll = Task { try await client.nextTask() }
        await assertEventually { await transport.requests.count == 1 }

        poll.cancel()

        do {
            _ = try await poll.value
            XCTFail("Expected cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        let requestCount = await transport.requests.count
        XCTAssertEqual(requestCount, 1)
        XCTAssertTrue(clock.durations.isEmpty)
    }

    func testCaptureModeStartsAndStopsDiscoverySchedulerAndLongPoll() async {
        let discovery = ReceiverDiscoveryFake(endpoint: endpoint)
        let transport = HTTPClientTransportFake(outcomes: [.waitForCancellation])
        let client = ReceiverClient(transport: transport)
        let scheduler = CaptureScheduler(clock: ManualWallClock(), handler: { _ in })
        let coordinator = CaptureCoordinator(
            capturer: FakeScreenCapturer(),
            encoder: FakeJPEGEncoder(),
            uploader: client
        )
        let service = CaptureModeService(
            discovery: discovery,
            scheduler: scheduler,
            client: client,
            coordinator: coordinator
        )

        await service.start()
        await assertEventually { await transport.requests.count == 1 }
        XCTAssertEqual(discovery.startCount, 1)
        let enabledAfterStart = await scheduler.isEnabled
        XCTAssertTrue(enabledAfterStart)

        await service.stop()
        await assertEventually { await transport.cancelledSendCount == 1 }
        XCTAssertEqual(discovery.stopCount, 1)
        let enabledAfterStop = await scheduler.isEnabled
        XCTAssertFalse(enabledAfterStop)
    }

    func testCaptureModeDropsExpiredRemoteTask() async throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let expired = RemoteCaptureTask(id: taskID, expiresAt: now)
        let transport = HTTPClientTransportFake(outcomes: [
            .response(ClientHTTPResponse(statusCode: 200, body: try APIJSON.encoder.encode(expired))),
            .waitForCancellation
        ])
        let discovery = ReceiverDiscoveryFake(endpoint: endpoint)
        let client = ReceiverClient(transport: transport)
        let capturer = FakeScreenCapturer()
        let scheduler = CaptureScheduler(clock: ManualWallClock(), handler: { _ in })
        let coordinator = CaptureCoordinator(
            capturer: capturer,
            encoder: FakeJPEGEncoder(),
            uploader: client
        )
        let service = CaptureModeService(
            discovery: discovery,
            scheduler: scheduler,
            client: client,
            coordinator: coordinator,
            clock: FixedWallClock(now: now)
        )

        await service.start()
        await assertEventually { await transport.requests.count == 2 }
        await service.stop()

        let captureCallCount = await capturer.captureCallCount
        XCTAssertEqual(captureCallCount, 0)
    }
}

private final class ReceiverDiscoveryFake: ReceiverDiscovering, @unchecked Sendable {
    private let lock = NSLock()
    private var handler: (@Sendable (NWEndpoint?) -> Void)?
    private let endpoint: NWEndpoint?
    private var starts = 0
    private var stops = 0

    init(endpoint: NWEndpoint?) {
        self.endpoint = endpoint
    }

    var selectedEndpoint: NWEndpoint? { endpoint }
    var startCount: Int { lock.withLock { starts } }
    var stopCount: Int { lock.withLock { stops } }

    func setUpdateHandler(_ handler: (@Sendable (NWEndpoint?) -> Void)?) {
        lock.withLock { self.handler = handler }
    }

    func start() {
        let handler = lock.withLock { () -> (@Sendable (NWEndpoint?) -> Void)? in
            starts += 1
            return self.handler
        }
        handler?(endpoint)
    }

    func stop() {
        lock.withLock { stops += 1 }
    }
}
