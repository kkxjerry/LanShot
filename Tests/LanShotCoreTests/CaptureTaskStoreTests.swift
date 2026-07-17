import Foundation
import XCTest
@testable import LanShotCore

final class CaptureTaskStoreTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_700_000_000)

    func testCreateMakesPendingTaskWithSixtySecondDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })

        let created = await store.createOrReuse()

        XCTAssertEqual(created.id, id)
        XCTAssertEqual(created.status, .pending)
        XCTAssertEqual(created.expiresAt, start.addingTimeInterval(60))
        XCTAssertNil(created.error)
    }

    func testCreateReusesPendingAndRunningTask() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })

        let created = await store.createOrReuse()
        let pendingReuse = await store.createOrReuse()
        let claimed = await store.claimNext()
        let runningReuse = await store.createOrReuse()

        XCTAssertEqual(pendingReuse, created)
        XCTAssertEqual(runningReuse, claimed)
    }

    func testCreateOrReuseResultReportsWhetherTaskWasCreatedAtomically() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })

        let first = await store.createOrReuseResult()
        let second = await store.createOrReuseResult()

        XCTAssertEqual(first, .created(CaptureTaskResponse(
            id: id,
            status: .pending,
            expiresAt: start.addingTimeInterval(60),
            error: nil
        )))
        XCTAssertEqual(second, .reused(first.response))
    }

    func testCreateOrReuseExpiresOldTaskBeforeCreatingNewTask() async {
        let firstID = makeUUID(1)
        let secondID = makeUUID(2)
        let ids = UUIDGenerator([firstID, secondID])
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { ids.next() })
        _ = await store.createOrReuse()
        clock.advance(by: 61)

        let created = await store.createOrReuse()
        let expired = await store.task(id: firstID)

        XCTAssertEqual(created.id, secondID)
        XCTAssertEqual(created.status, .pending)
        XCTAssertEqual(expired?.status, .expired)
    }

    func testFirstClaimMovesPendingTaskToRunningAndResetsDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        clock.advance(by: 15)

        let claimed = await store.claimNext()

        XCTAssertEqual(claimed?.id, id)
        XCTAssertEqual(claimed?.status, .running)
        XCTAssertEqual(claimed?.expiresAt, start.addingTimeInterval(75))
    }

    func testRunningRedeliveryDoesNotExtendDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        let firstDelivery = await store.claimNext()
        clock.advance(by: 20)

        let redelivery = await store.claimNext()

        XCTAssertEqual(redelivery, firstDelivery)
        XCTAssertEqual(redelivery?.expiresAt, start.addingTimeInterval(60))
    }

    func testPendingTaskExpiresBeforeItCanBeClaimed() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        clock.advance(by: 61)

        let expired = await store.task(id: id)
        let claimed = await store.claimNext()

        XCTAssertEqual(expired?.status, .expired)
        XCTAssertNil(claimed)
    }

    func testClaimNextExpiresPendingTaskWhenItIsFirstCallAfterDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        clock.advance(by: 61)

        let claimed = await store.claimNext()
        let expired = await store.task(id: id)

        XCTAssertNil(claimed)
        XCTAssertEqual(expired?.status, .expired)
    }

    func testRunningTaskExpiresAtResultDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        clock.advance(by: 61)

        let expired = await store.task(id: id)

        XCTAssertEqual(expired?.status, .expired)
    }

    func testCompleteStoresCompletedTask() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        let running = await store.claimNext()

        let result = await store.complete(id: id, path: "/captures/one.jpg")
        let stored = await store.task(id: id)

        let expected = CaptureTaskResponse(
            id: id,
            status: .completed,
            expiresAt: running!.expiresAt,
            error: nil
        )
        XCTAssertEqual(result, .stored(expected))
        XCTAssertEqual(stored, expected)
    }

    func testFailureStoresStableCodeAndMessage() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        let running = await store.claimNext()

        let result = await store.fail(
            id: id,
            code: .permissionRequired,
            message: "Screen recording permission is required"
        )
        let stored = await store.task(id: id)

        let expected = CaptureTaskResponse(
            id: id,
            status: .failed,
            expiresAt: running!.expiresAt,
            error: APIError(
                code: CaptureFailureCode.permissionRequired.rawValue,
                message: "Screen recording permission is required"
            )
        )
        XCTAssertEqual(result, .stored(expected))
        XCTAssertEqual(stored, expected)
    }

    func testFailExpiresRunningTaskWhenItIsFirstCallAfterDeadline() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        clock.advance(by: 61)

        let result: CaptureTaskStore.CompletionResult = await store.fail(
            id: id,
            code: .captureFailed,
            message: "Too late"
        )
        let expired = await store.task(id: id)

        XCTAssertEqual(result, .gone)
        XCTAssertEqual(expired?.status, .expired)
    }

    func testPendingTaskRejectsResultsWithoutChangingState() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()

        let completion = await store.complete(id: id, path: "/captures/early.jpg")
        let afterCompletion = await store.task(id: id)
        let failure = await store.fail(
            id: id,
            code: .captureFailed,
            message: "Too early"
        )
        let afterFailure = await store.task(id: id)
        let claimed = await store.claimNext()

        XCTAssertEqual(completion, .gone)
        XCTAssertEqual(afterCompletion?.status, .pending)
        XCTAssertEqual(failure, .gone)
        XCTAssertEqual(afterFailure?.status, .pending)
        XCTAssertEqual(claimed?.status, .running)
    }

    func testUnknownTaskReturnsNotFound() async {
        let knownID = makeUUID(1)
        let unknownID = makeUUID(2)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { knownID })

        let task = await store.task(id: unknownID)
        let completion = await store.complete(id: unknownID, path: "/captures/missing.jpg")
        let failure = await store.fail(
            id: unknownID,
            code: .captureFailed,
            message: "Capture failed"
        )

        XCTAssertNil(task)
        XCTAssertEqual(completion, .notFound)
        XCTAssertEqual(failure, .notFound)
    }

    func testLateResultsAreGoneAndCannotReverseExpiry() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        clock.advance(by: 61)

        let completion = await store.complete(id: id, path: "/captures/late.jpg")
        let failure = await store.fail(
            id: id,
            code: .captureFailed,
            message: "Too late"
        )
        let stored = await store.task(id: id)

        XCTAssertEqual(completion, .gone)
        XCTAssertEqual(failure, .gone)
        XCTAssertEqual(stored?.status, .expired)
    }

    func testDuplicateCompleteIsIdempotentAndFailureCannotReverseCompletion() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        let first = await store.complete(id: id, path: "/captures/original.jpg")

        let duplicate = await store.complete(id: id, path: "/captures/retry.jpg")
        let failure = await store.fail(
            id: id,
            code: .captureFailed,
            message: "Must not replace completion"
        )

        guard case let .stored(response) = first else {
            return XCTFail("Expected the first completion to be stored")
        }
        XCTAssertEqual(duplicate, .duplicate(response))
        XCTAssertEqual(failure, .gone)
        let stored = await store.task(id: id)
        XCTAssertEqual(stored, response)
    }

    func testDuplicateFailureIsIdempotentAndCompletionCannotReverseFailure() async {
        let id = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { id })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        let first = await store.fail(
            id: id,
            code: .sessionLocked,
            message: "Session is locked"
        )

        let duplicate = await store.fail(
            id: id,
            code: .captureFailed,
            message: "Replacement must be ignored"
        )
        let completion = await store.complete(id: id, path: "/captures/invalid.jpg")

        guard case let .stored(response) = first else {
            return XCTFail("Expected the first failure to be stored")
        }
        XCTAssertEqual(duplicate, .duplicate(response))
        XCTAssertEqual(completion, .gone)
        let stored = await store.task(id: id)
        XCTAssertEqual(stored, response)
    }

    func testTerminalTaskAllowsNewTaskWhileOldTaskRemainsQueryable() async {
        let firstID = makeUUID(1)
        let secondID = makeUUID(2)
        let ids = UUIDGenerator([firstID, secondID])
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { ids.next() })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        _ = await store.complete(id: firstID, path: "/captures/first.jpg")

        let second = await store.createOrReuse()
        let first = await store.task(id: firstID)

        XCTAssertEqual(second.id, secondID)
        XCTAssertEqual(second.status, .pending)
        XCTAssertEqual(first?.status, .completed)
    }

    func testCreateSkipsHistoricalUUIDCollisionWithoutCorruptingHistory() async {
        let terminalIDs = (1...100).map(makeUUID)
        let freshID = makeUUID(101)
        let generator = UUIDGenerator(terminalIDs + [terminalIDs[0], freshID])
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { generator.next() })

        for (index, id) in terminalIDs.enumerated() {
            _ = await store.createOrReuse()
            _ = await store.claimNext()
            _ = await store.complete(id: id, path: "/captures/\(index).jpg")
        }

        let created = await store.createOrReuse()
        let oldTerminal = await store.task(id: terminalIDs[0])
        clock.advance(by: 61)
        let expiredFreshTask = await store.task(id: freshID)

        XCTAssertEqual(created.id, freshID)
        XCTAssertEqual(created.status, .pending)
        XCTAssertEqual(oldTerminal?.status, .completed)
        XCTAssertEqual(expiredFreshTask?.status, .expired)
    }

    func testPersistentHistoricalUUIDCollisionUsesFallbackWithoutOverwriting() async {
        let oldID = makeUUID(1)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { oldID })
        _ = await store.createOrReuse()
        _ = await store.claimNext()
        _ = await store.complete(id: oldID, path: "/captures/old.jpg")

        let created = await store.createOrReuse()
        let oldTerminal = await store.task(id: oldID)

        XCTAssertNotEqual(created.id, oldID)
        XCTAssertEqual(created.status, .pending)
        XCTAssertEqual(oldTerminal?.status, .completed)
    }

    func testStoreRetainsAtLeastMostRecentOneHundredTerminalTasks() async {
        let ids = (1...101).map(makeUUID)
        let generator = UUIDGenerator(ids)
        let clock = ManualClock(now: start)
        let store = CaptureTaskStore(clock: clock, uuid: { generator.next() })

        for (index, id) in ids.enumerated() {
            _ = await store.createOrReuse()
            _ = await store.claimNext()
            _ = await store.complete(id: id, path: "/captures/\(index).jpg")
        }

        for id in ids.suffix(100) {
            let retained = await store.task(id: id)
            XCTAssertEqual(retained?.status, .completed)
        }
    }

    private func makeUUID(_ value: Int) -> UUID {
        UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", value))!
    }
}

private final class UUIDGenerator: @unchecked Sendable {
    private let lock = NSLock()
    private let ids: [UUID]
    private var index = 0

    init(_ ids: [UUID]) {
        self.ids = ids
    }

    func next() -> UUID {
        lock.lock()
        defer { lock.unlock() }
        precondition(index < ids.count, "UUIDGenerator exhausted")
        defer { index += 1 }
        return ids[index]
    }
}
