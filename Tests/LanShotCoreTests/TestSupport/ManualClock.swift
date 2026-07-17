import Foundation
import LanShotCore

final class ManualClock: WallClock, @unchecked Sendable {
    private let lock = NSLock()
    private var current: Date

    init(now: Date) {
        current = now
    }

    var now: Date {
        lock.lock()
        defer { lock.unlock() }
        return current
    }

    func sleep(for seconds: TimeInterval) async throws {
        guard seconds.isFinite else { throw ClockError.invalidDuration }
        guard seconds > 0 else { return }
        advance(by: seconds)
    }

    func advance(by seconds: TimeInterval) {
        lock.lock()
        defer { lock.unlock() }
        current = current.addingTimeInterval(seconds)
    }
}
