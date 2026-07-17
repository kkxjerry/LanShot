import Foundation

public actor CaptureTaskStore {
    public enum CreateResult: Equatable, Sendable {
        case created(CaptureTaskResponse)
        case reused(CaptureTaskResponse)

        public var response: CaptureTaskResponse {
            switch self {
            case .created(let response), .reused(let response):
                return response
            }
        }
    }

    public enum CompletionResult: Equatable, Sendable {
        case stored(CaptureTaskResponse)
        case duplicate(CaptureTaskResponse)
        case notFound
        case gone
    }

    private static let deadline: TimeInterval = 60
    private static let injectedUUIDRetryLimit = 16
    private static let terminalHistoryLimit = 100

    private struct Record {
        let id: UUID
        var status: CaptureTaskStatus
        var expiresAt: Date
        var error: APIError?
        var path: String?

        var response: CaptureTaskResponse {
            CaptureTaskResponse(
                id: id,
                status: status,
                expiresAt: expiresAt,
                error: error
            )
        }
    }

    private let clock: any WallClock
    private let uuid: @Sendable () -> UUID
    private var records: [UUID: Record] = [:]
    private var activeID: UUID?
    private var terminalIDs: [UUID] = []

    public init(
        clock: any WallClock = SystemWallClock(),
        uuid: @escaping @Sendable () -> UUID = { UUID() }
    ) {
        self.clock = clock
        self.uuid = uuid
    }

    public func createOrReuse() -> CaptureTaskResponse {
        createOrReuseResult().response
    }

    public func createOrReuseResult() -> CreateResult {
        expireIfNeeded()

        if let activeID, let record = records[activeID] {
            switch record.status {
            case .pending, .running:
                return .reused(record.response)
            case .completed, .failed, .expired:
                self.activeID = nil
            }
        }

        let id = makeUniqueID()
        let record = Record(
            id: id,
            status: .pending,
            expiresAt: clock.now.addingTimeInterval(Self.deadline),
            error: nil,
            path: nil
        )
        records[id] = record
        activeID = id
        return .created(record.response)
    }

    private func makeUniqueID() -> UUID {
        for _ in 0..<Self.injectedUUIDRetryLimit {
            let candidate = uuid()
            if records[candidate] == nil {
                return candidate
            }
        }

        while true {
            let candidate = UUID()
            if records[candidate] == nil {
                return candidate
            }
        }
    }

    public func claimNext() -> CaptureTaskResponse? {
        expireIfNeeded()
        guard let activeID, var record = records[activeID] else { return nil }

        switch record.status {
        case .pending:
            record.status = .running
            record.expiresAt = clock.now.addingTimeInterval(Self.deadline)
            records[activeID] = record
            return record.response
        case .running:
            return record.response
        case .completed, .failed, .expired:
            self.activeID = nil
            return nil
        }
    }

    public func task(id: UUID) -> CaptureTaskResponse? {
        expireIfNeeded()
        return records[id]?.response
    }

    public func complete(id: UUID, path: String) -> CompletionResult {
        expireIfNeeded()
        guard var record = records[id] else { return .notFound }

        switch record.status {
        case .running:
            record.status = .completed
            record.path = path
            let response = record.response
            storeTerminal(record)
            return .stored(response)
        case .completed:
            return .duplicate(record.response)
        case .pending, .failed, .expired:
            return .gone
        }
    }

    public func fail(
        id: UUID,
        code: CaptureFailureCode,
        message: String
    ) -> CompletionResult {
        expireIfNeeded()
        guard var record = records[id] else { return .notFound }

        switch record.status {
        case .running:
            record.status = .failed
            record.error = APIError(code: code.rawValue, message: message)
            let response = record.response
            storeTerminal(record)
            return .stored(response)
        case .failed:
            return .duplicate(record.response)
        case .pending, .completed, .expired:
            return .gone
        }
    }

    private func expireIfNeeded() {
        guard let activeID, var record = records[activeID] else {
            activeID = nil
            return
        }
        guard record.expiresAt <= clock.now else { return }

        switch record.status {
        case .pending, .running:
            record.status = .expired
            storeTerminal(record)
        case .completed, .failed, .expired:
            self.activeID = nil
        }
    }

    private func storeTerminal(_ record: Record) {
        records[record.id] = record
        if activeID == record.id {
            activeID = nil
        }
        terminalIDs.append(record.id)

        while terminalIDs.count > Self.terminalHistoryLimit {
            let expiredID = terminalIDs.removeFirst()
            records.removeValue(forKey: expiredID)
        }
    }
}
