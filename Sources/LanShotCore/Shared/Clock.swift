import Foundation

public protocol WallClock: Sendable {
    var now: Date { get }
    func sleep(for seconds: TimeInterval) async throws
}

public struct SystemWallClock: WallClock {
    public init() {}

    public var now: Date { Date() }

    public func sleep(for seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }
}
