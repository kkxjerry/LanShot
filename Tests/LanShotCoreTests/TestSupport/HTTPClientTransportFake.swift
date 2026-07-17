import Foundation
import Network
@testable import LanShotCore

enum HTTPClientTransportStubError: Error, Sendable {
    case offline
}

enum HTTPClientTransportOutcome: @unchecked Sendable {
    case response(ClientHTTPResponse)
    case failure(Error)
    case waitForCancellation
}

actor HTTPClientTransportFake: HTTPClientTransport {
    private var outcomes: [HTTPClientTransportOutcome]
    private(set) var requests: [ClientHTTPRequest] = []
    private(set) var endpoints: [NWEndpoint] = []
    private(set) var cancelledSendCount = 0

    init(outcomes: [HTTPClientTransportOutcome]) {
        self.outcomes = outcomes
    }

    func send(_ request: ClientHTTPRequest, to endpoint: NWEndpoint) async throws -> ClientHTTPResponse {
        requests.append(request)
        endpoints.append(endpoint)

        let outcome = outcomes.isEmpty
            ? .response(ClientHTTPResponse(statusCode: 204))
            : outcomes.removeFirst()

        switch outcome {
        case .response(let response):
            return response
        case .failure(let error):
            throw error
        case .waitForCancellation:
            do {
                try await Task.sleep(nanoseconds: UInt64.max)
                throw HTTPClientTransportStubError.offline
            } catch is CancellationError {
                cancelledSendCount += 1
                throw CancellationError()
            }
        }
    }
}

final class RecordingHTTPClientClock: WallClock, @unchecked Sendable {
    private let lock = NSLock()
    private var current: Date
    private var recordedDurations: [TimeInterval] = []

    init(now: Date = Date(timeIntervalSince1970: 1_700_000_000)) {
        current = now
    }

    var now: Date {
        lock.withLock { current }
    }

    var durations: [TimeInterval] {
        lock.withLock { recordedDurations }
    }

    func sleep(for seconds: TimeInterval) async throws {
        try Task.checkCancellation()
        lock.withLock {
            recordedDurations.append(seconds)
            current = current.addingTimeInterval(seconds)
        }
    }
}

private extension NSLock {
    func withLock<Value>(_ body: () -> Value) -> Value {
        lock()
        defer { unlock() }
        return body()
    }
}
