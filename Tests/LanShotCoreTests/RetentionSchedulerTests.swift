import Foundation
import XCTest
import LanShotCore

final class RetentionSchedulerTests: XCTestCase {
    func testStartRunsCleanupImmediately() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let expiredFile = try makeExpiredJPEG(under: root)
        let now = ISO8601DateFormatter().date(from: "2026-07-17T12:00:00Z")!
        try FileManager.default.setAttributes(
            [.modificationDate: now.addingTimeInterval(-7 * 86_400 - 1)],
            ofItemAtPath: expiredFile.path
        )
        let clock = ManualRetentionClock(now: now)
        let scheduler = RetentionScheduler(store: ImageStore(root: root), clock: clock)

        scheduler.start()

        await waitUntil { !FileManager.default.fileExists(atPath: expiredFile.path) }
        await waitUntil { clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.recordedDurations, [86_400])
        scheduler.stop()
        await waitUntil { clock.pendingSleepCount == 0 }
    }

    func testSchedulerRunsEveryDayAndStartStopRemainSingleLoop() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let now = ISO8601DateFormatter().date(from: "2026-07-17T12:00:00Z")!
        let clock = ManualRetentionClock(now: now)
        let scheduler = RetentionScheduler(store: ImageStore(root: root), clock: clock)

        scheduler.start()
        scheduler.start()
        await waitUntil { clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.recordedDurations, [86_400])

        let firstDailyFile = try makeExpiredJPEG(under: root, name: "daily.jpg")
        try FileManager.default.setAttributes(
            [.modificationDate: now.addingTimeInterval(-7 * 86_400 - 1)],
            ofItemAtPath: firstDailyFile.path
        )
        clock.advance(by: 86_400)
        await waitUntil { !FileManager.default.fileExists(atPath: firstDailyFile.path) }
        await waitUntil { clock.recordedDurations.count == 2 && clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.recordedDurations, [86_400, 86_400])

        scheduler.stop()
        await waitUntil { clock.pendingSleepCount == 0 }
        let stoppedFile = try makeExpiredJPEG(under: root, name: "stopped.jpg")
        try FileManager.default.setAttributes(
            [.modificationDate: clock.now.addingTimeInterval(-7 * 86_400 - 1)],
            ofItemAtPath: stoppedFile.path
        )
        clock.advance(by: 86_400)
        XCTAssertTrue(FileManager.default.fileExists(atPath: stoppedFile.path))
        XCTAssertEqual(clock.recordedDurations, [86_400, 86_400])

        scheduler.start()
        scheduler.start()
        await waitUntil { !FileManager.default.fileExists(atPath: stoppedFile.path) }
        await waitUntil { clock.recordedDurations.count == 3 && clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.recordedDurations, [86_400, 86_400, 86_400])
        scheduler.stop()
        await waitUntil { clock.pendingSleepCount == 0 }
    }

    private func makeExpiredJPEG(under root: URL, name: String = "expired.jpg") throws -> URL {
        let directory = root.appendingPathComponent("2026-07-01", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent(name)
        try Data([0xFF, 0xD8, 0xFF, 0xD9]).write(to: file)
        return file
    }

    private func waitUntil(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: @escaping () -> Bool
    ) async {
        for _ in 0..<2_000 {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        XCTFail("Timed out waiting for asynchronous condition", file: file, line: line)
    }
}

private final class ManualRetentionClock: WallClock, @unchecked Sendable {
    private struct Sleeper {
        let continuation: CheckedContinuation<Void, Error>
    }

    private let lock = NSLock()
    private var currentDate: Date
    private var sleepers: [UUID: Sleeper] = [:]
    private var cancelledSleeperIDs: Set<UUID> = []
    private var durations: [TimeInterval] = []

    init(now: Date) {
        currentDate = now
    }

    var now: Date {
        withLock { currentDate }
    }

    var recordedDurations: [TimeInterval] {
        withLock { durations }
    }

    var pendingSleepCount: Int {
        withLock { sleepers.count }
    }

    func sleep(for seconds: TimeInterval) async throws {
        let id = UUID()
        try Task.checkCancellation()

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                register(continuation, duration: seconds, id: id)
            }
        } onCancel: {
            cancelSleeper(id: id)
        }
    }

    func advance(by seconds: TimeInterval) {
        let continuations: [CheckedContinuation<Void, Error>] = withLock {
            currentDate = currentDate.addingTimeInterval(seconds)
            let pending = sleepers.values.map(\.continuation)
            sleepers.removeAll()
            return pending
        }
        continuations.forEach { $0.resume() }
    }

    private func register(
        _ continuation: CheckedContinuation<Void, Error>,
        duration: TimeInterval,
        id: UUID
    ) {
        let wasCancelled = withLock {
            if cancelledSleeperIDs.remove(id) != nil {
                return true
            }
            durations.append(duration)
            sleepers[id] = Sleeper(continuation: continuation)
            return false
        }

        if wasCancelled {
            continuation.resume(throwing: CancellationError())
        }
    }

    private func cancelSleeper(id: UUID) {
        let continuation: CheckedContinuation<Void, Error>? = withLock {
            guard let sleeper = sleepers.removeValue(forKey: id) else {
                cancelledSleeperIDs.insert(id)
                return nil
            }
            return sleeper.continuation
        }
        continuation?.resume(throwing: CancellationError())
    }

    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }
}
