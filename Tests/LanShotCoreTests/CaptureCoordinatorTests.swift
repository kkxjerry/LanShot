import Foundation
import XCTest
@testable import LanShotCore

final class CaptureCoordinatorTests: XCTestCase {
    private let completedAt = Date(timeIntervalSince1970: 1_700_000_123)

    func testConcurrentSubmissionsRunTheWholePipelineFIFO() async {
        let capturer = FakeScreenCapturer(capturesToBlock: 1)
        let encoder = FakeJPEGEncoder()
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, encoder: encoder, uploader: uploader)
        let firstID = UUID(uuidString: "00000000-0000-0000-0000-000000000201")!
        let secondID = UUID(uuidString: "00000000-0000-0000-0000-000000000202")!
        let thirdID = UUID(uuidString: "00000000-0000-0000-0000-000000000203")!

        let first = Task { await coordinator.submit(.local(firstID)) }
        await assertEventually { await capturer.blockedCaptureCount == 1 }
        let second = Task { await coordinator.submit(.remote(secondID)) }
        for _ in 0..<20 { await Task.yield() }
        let third = Task { await coordinator.submit(.scheduled(thirdID)) }
        for _ in 0..<20 { await Task.yield() }

        await capturer.releaseNextCapture()

        let statuses = await [first.value, second.value, third.value]
        XCTAssertEqual(statuses, Array(repeating: .completed(completedAt), count: 3))
        let maximumActiveCaptures = await capturer.maximumActiveCaptures
        let uploads = await uploader.uploads
        XCTAssertEqual(maximumActiveCaptures, 1)
        XCTAssertEqual(uploads.map(\.captureID), [firstID, secondID, thirdID])
    }

    func testUsesPointEightQualityAndRoutesRequestIDs() async {
        let capturer = FakeScreenCapturer()
        let encoder = FakeJPEGEncoder(data: Data([1, 2, 3]))
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, encoder: encoder, uploader: uploader)
        let scheduledID = UUID(uuidString: "00000000-0000-0000-0000-000000000211")!
        let localID = UUID(uuidString: "00000000-0000-0000-0000-000000000212")!
        let remoteID = UUID(uuidString: "00000000-0000-0000-0000-000000000213")!

        _ = await coordinator.submit(.scheduled(scheduledID))
        _ = await coordinator.submit(.local(localID))
        _ = await coordinator.submit(.remote(remoteID))

        XCTAssertEqual(encoder.qualities, [0.8, 0.8, 0.8])
        let uploads = await uploader.uploads
        XCTAssertEqual(uploads, [
            UploadCall(data: Data([1, 2, 3]), captureID: scheduledID, remoteTaskID: nil),
            UploadCall(data: Data([1, 2, 3]), captureID: localID, remoteTaskID: nil),
            UploadCall(data: Data([1, 2, 3]), captureID: remoteID, remoteTaskID: remoteID)
        ])
    }

    func testCompletedRemoteRedeliveryDoesNotCaptureAgain() async {
        let capturer = FakeScreenCapturer()
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, uploader: uploader)
        let taskID = UUID(uuidString: "00000000-0000-0000-0000-000000000221")!

        let first = await coordinator.submit(.remote(taskID))
        let redelivery = await coordinator.submit(.remote(taskID))

        XCTAssertEqual(first, .completed(completedAt))
        XCTAssertEqual(redelivery, .completed(completedAt))
        let captureCallCount = await capturer.captureCallCount
        let uploads = await uploader.uploads
        XCTAssertEqual(captureCallCount, 1)
        XCTAssertEqual(uploads.count, 1)
    }

    func testRemoteRedeliveryQueuedWhileOriginalIsRunningIsDeduplicatedWhenReached() async {
        let capturer = FakeScreenCapturer(capturesToBlock: 1)
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, uploader: uploader)
        let taskID = UUID(uuidString: "00000000-0000-0000-0000-000000000222")!

        let original = Task { await coordinator.submit(.remote(taskID)) }
        await assertEventually { await capturer.blockedCaptureCount == 1 }
        let redelivery = Task { await coordinator.submit(.remote(taskID)) }
        for _ in 0..<20 { await Task.yield() }
        await capturer.releaseNextCapture()

        let statuses = await [original.value, redelivery.value]
        XCTAssertEqual(statuses, [.completed(completedAt), .completed(completedAt)])
        let captureCallCount = await capturer.captureCallCount
        let uploads = await uploader.uploads
        XCTAssertEqual(captureCallCount, 1)
        XCTAssertEqual(uploads.count, 1)
    }

    func testRemoteDeduplicationRetainsOnlyTheMostRecentOneHundredCompletedIDs() async {
        let capturer = FakeScreenCapturer()
        let coordinator = makeCoordinator(capturer: capturer)
        let ids = (0..<101).map { index in
            UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", index + 1))!
        }

        for id in ids {
            _ = await coordinator.submit(.remote(id))
        }
        _ = await coordinator.submit(.remote(ids[1]))
        var captureCallCount = await capturer.captureCallCount
        XCTAssertEqual(captureCallCount, 101)

        _ = await coordinator.submit(.remote(ids[0]))
        captureCallCount = await capturer.captureCallCount
        XCTAssertEqual(captureCallCount, 102)
    }

    func testEveryCaptureErrorMapsToStableRemoteFailureCodeAndRuntimeStatus() async {
        let cases: [(CaptureError, CaptureFailureCode, String, RuntimeStatus)] = [
            (.permissionRequired, .permissionRequired, "Screen recording permission is required.", .permissionRequired),
            (.sessionLocked, .sessionLocked, "The Mac session is locked.", .macUnavailable("The Mac session is locked.")),
            (.displayAsleep, .displayAsleep, "The display is asleep.", .macUnavailable("The display is asleep.")),
            (.captureFailed("The screen could not be captured."), .captureFailed, "The screen could not be captured.", .macUnavailable("The screen could not be captured.")),
            (.encodingFailed("The screenshot could not be encoded."), .encodingFailed, "The screenshot could not be encoded.", .macUnavailable("The screenshot could not be encoded."))
        ]

        for (index, item) in cases.enumerated() {
            let capturer = FakeScreenCapturer(outcomes: [.failure(item.0)])
            let uploader = FakeCaptureUploader()
            let coordinator = makeCoordinator(capturer: capturer, uploader: uploader)
            let taskID = UUID(uuidString: String(format: "00000000-0000-0000-0001-%012d", index + 1))!

            let status = await coordinator.submit(.remote(taskID))

            XCTAssertEqual(status, item.3)
            let reports = await uploader.failureReports
            XCTAssertEqual(reports, [FailureReport(taskID: taskID, code: item.1, message: item.2)])
        }
    }

    func testEncoderCaptureErrorIsReportedAsEncodingFailure() async {
        let encoder = FakeJPEGEncoder(outcomes: [
            .failure(CaptureError.encodingFailed("JPEG encoding was unavailable."))
        ])
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(encoder: encoder, uploader: uploader)
        let taskID = UUID(uuidString: "00000000-0000-0000-0000-000000000231")!

        let status = await coordinator.submit(.remote(taskID))

        XCTAssertEqual(status, .macUnavailable("JPEG encoding was unavailable."))
        let reports = await uploader.failureReports
        XCTAssertEqual(reports, [
            FailureReport(taskID: taskID, code: .encodingFailed, message: "JPEG encoding was unavailable.")
        ])
    }

    func testLocalFailureIsNotReportedToRemoteTaskEndpoint() async {
        let capturer = FakeScreenCapturer(outcomes: [.failure(CaptureError.sessionLocked)])
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, uploader: uploader)

        let status = await coordinator.submit(.local(UUID()))

        XCTAssertEqual(status, .macUnavailable("The Mac session is locked."))
        let reports = await uploader.failureReports
        XCTAssertTrue(reports.isEmpty)
    }

    func testUploadFailureReturnsUploadFailedWithoutReportingCaptureFailure() async {
        let uploader = FakeCaptureUploader(outcomes: [.failure(StubError.uploadTransport)])
        let coordinator = makeCoordinator(uploader: uploader)
        let taskID = UUID(uuidString: "00000000-0000-0000-0000-000000000241")!

        let status = await coordinator.submit(.remote(taskID))

        guard case .uploadFailed(let message) = status else {
            return XCTFail("Expected uploadFailed, got \(status)")
        }
        XCTAssertFalse(message.isEmpty)
        let reports = await uploader.failureReports
        XCTAssertTrue(reports.isEmpty)
    }

    func testFailureDoesNotStopQueuedRequests() async {
        let image = makeTestImage()
        let capturer = FakeScreenCapturer(
            image: image,
            outcomes: [
                .failure(CaptureError.captureFailed("First capture failed.")),
                .image(image)
            ],
            capturesToBlock: 1
        )
        let uploader = FakeCaptureUploader()
        let coordinator = makeCoordinator(capturer: capturer, uploader: uploader)
        let failedID = UUID(uuidString: "00000000-0000-0000-0000-000000000251")!
        let successfulID = UUID(uuidString: "00000000-0000-0000-0000-000000000252")!

        let failed = Task { await coordinator.submit(.remote(failedID)) }
        await assertEventually { await capturer.blockedCaptureCount == 1 }
        let successful = Task { await coordinator.submit(.local(successfulID)) }
        for _ in 0..<20 { await Task.yield() }
        await capturer.releaseNextCapture()

        let statuses = await [failed.value, successful.value]
        XCTAssertEqual(statuses, [
            .macUnavailable("First capture failed."),
            .completed(completedAt)
        ])
        let uploads = await uploader.uploads
        XCTAssertEqual(uploads.map(\.captureID), [successfulID])
    }

    private func makeCoordinator(
        capturer: FakeScreenCapturer = FakeScreenCapturer(),
        encoder: FakeJPEGEncoder = FakeJPEGEncoder(),
        uploader: FakeCaptureUploader = FakeCaptureUploader()
    ) -> CaptureCoordinator {
        CaptureCoordinator(
            capturer: capturer,
            encoder: encoder,
            uploader: uploader,
            clock: FixedWallClock(now: completedAt)
        )
    }
}
