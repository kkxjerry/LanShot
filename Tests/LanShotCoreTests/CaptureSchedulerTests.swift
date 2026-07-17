import Foundation
import XCTest
@testable import LanShotCore

final class CaptureSchedulerTests: XCTestCase {
    func testDefaultsToFiveMinutesAndDisabled() async {
        let scheduler = CaptureScheduler(
            clock: ManualWallClock(),
            idGenerator: { UUID() },
            handler: { _ in }
        )

        let interval = await scheduler.interval
        let isEnabled = await scheduler.isEnabled

        XCTAssertEqual(interval, .five)
        XCTAssertFalse(isEnabled)
    }

    func testIntervalRawValuesAreTheSupportedMinuteOptions() {
        XCTAssertEqual(CaptureInterval.allCases.map(\.rawValue), [1, 5, 10, 30, 60])
        XCTAssertEqual(CaptureInterval.allCases.map(\.seconds), [60, 300, 600, 1_800, 3_600])
    }

    func testStartCreatesOneLoopAndRepeatedStartsDoNotDuplicateIt() async {
        let clock = ManualWallClock()
        let recorder = SchedulerRequestRecorder()
        let captureID = UUID(uuidString: "00000000-0000-0000-0000-000000000101")!
        let scheduler = CaptureScheduler(
            clock: clock,
            idGenerator: { captureID },
            handler: { request in await recorder.append(request) }
        )

        await scheduler.start()
        await scheduler.start()
        await scheduler.start()

        await assertEventually { clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.durations, [300])
        XCTAssertTrue(clock.resumeNextSleep())
        await assertEventually { await recorder.requests.count == 1 }

        let requests = await recorder.requests
        XCTAssertEqual(requests, [.scheduled(captureID)])
        await assertEventually { clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.durations, [300, 300])

        await scheduler.stop()
        await assertEventually { clock.pendingSleepCount == 0 }
    }

    func testStopCancelsTheLoopAndStartCanCreateANewLoop() async {
        let clock = ManualWallClock()
        let recorder = SchedulerRequestRecorder()
        let scheduler = CaptureScheduler(
            clock: clock,
            idGenerator: { UUID() },
            handler: { request in await recorder.append(request) }
        )

        await scheduler.start()
        await assertEventually { clock.pendingSleepCount == 1 }

        await scheduler.stop()
        await assertEventually { clock.pendingSleepCount == 0 }
        XCTAssertFalse(clock.resumeNextSleep())
        let requestsAfterStop = await recorder.requests
        XCTAssertTrue(requestsAfterStop.isEmpty)

        await scheduler.start()
        await assertEventually { clock.pendingSleepCount == 1 }
        XCTAssertTrue(clock.resumeNextSleep())
        await assertEventually { await recorder.requests.count == 1 }

        await scheduler.stop()
    }

    func testChangingIntervalRestartsExactlyOneEnabledLoop() async {
        let clock = ManualWallClock()
        let recorder = SchedulerRequestRecorder()
        let scheduler = CaptureScheduler(
            clock: clock,
            idGenerator: { UUID() },
            handler: { request in await recorder.append(request) }
        )

        await scheduler.start()
        await assertEventually { clock.pendingSleepCount == 1 }
        XCTAssertEqual(clock.durations, [300])

        await scheduler.setInterval(.ten)
        await assertEventually {
            clock.pendingSleepCount == 1 && clock.durations.last == 600
        }
        await scheduler.setInterval(.ten)

        XCTAssertEqual(clock.pendingSleepCount, 1)
        XCTAssertEqual(clock.durations, [300, 600])
        XCTAssertTrue(clock.resumeNextSleep())
        await assertEventually { await recorder.requests.count == 1 }

        await scheduler.stop()
    }

    func testChangingIntervalWhileDisabledOnlyStoresTheSelection() async {
        let clock = ManualWallClock()
        let scheduler = CaptureScheduler(
            clock: clock,
            idGenerator: { UUID() },
            handler: { _ in }
        )

        await scheduler.setInterval(.one)

        let interval = await scheduler.interval
        let isEnabled = await scheduler.isEnabled
        XCTAssertEqual(interval, .one)
        XCTAssertFalse(isEnabled)
        XCTAssertEqual(clock.pendingSleepCount, 0)

        await scheduler.start()
        await assertEventually {
            clock.pendingSleepCount == 1 && clock.durations == [60]
        }
        await scheduler.stop()
    }

    func testReleasingSchedulerCancelsItsLoopBeforeTheClockCanFire() async {
        let clock = ManualWallClock()
        let recorder = SchedulerRequestRecorder()
        var scheduler: CaptureScheduler? = CaptureScheduler(
            clock: clock,
            idGenerator: { UUID() },
            handler: { request in await recorder.append(request) }
        )

        await scheduler?.start()
        await assertEventually { clock.pendingSleepCount == 1 }

        scheduler = nil
        for _ in 0..<20 { await Task.yield() }
        let advancedCancelledSleep = clock.resumeNextSleep()
        try? await Task.sleep(nanoseconds: 5_000_000)

        XCTAssertFalse(advancedCancelledSleep)
        let requests = await recorder.requests
        XCTAssertTrue(requests.isEmpty)
    }
}
