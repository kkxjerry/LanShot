import Foundation

public enum ClockError: Error, Equatable, Sendable {
    case invalidDuration
}

public protocol WallClock: Sendable {
    var now: Date { get }
    func sleep(for seconds: TimeInterval) async throws
}

public struct SystemWallClock: WallClock {
    public init() {}

    public var now: Date { Date() }

    public func sleep(for seconds: TimeInterval) async throws {
        guard seconds.isFinite else { throw ClockError.invalidDuration }
        guard seconds > 0 else { return }

        let nanoseconds = seconds * 1_000_000_000
        guard nanoseconds.isFinite, nanoseconds < Double(UInt64.max) else {
            throw ClockError.invalidDuration
        }

        try await Task.sleep(nanoseconds: UInt64(nanoseconds))
    }
}
