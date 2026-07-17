import CoreGraphics
import Foundation
import XCTest
@testable import LanShotCore

final class LockedBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) {
        self.value = value
    }

    func withValue<Result>(_ body: (inout Value) -> Result) -> Result {
        lock.lock()
        defer { lock.unlock() }
        return body(&value)
    }
}

final class ManualWallClock: WallClock, @unchecked Sendable {
    private struct PendingSleep {
        let id: UUID
        let continuation: CheckedContinuation<Void, Error>
    }

    private struct State {
        var now: Date
        var pendingSleeps: [PendingSleep] = []
        var requestedDurations: [TimeInterval] = []
        var cancelledBeforeRegistration: Set<UUID> = []
    }

    private let state: LockedBox<State>

    init(now: Date = Date(timeIntervalSince1970: 1_700_000_000)) {
        state = LockedBox(State(now: now))
    }

    var now: Date {
        state.withValue { $0.now }
    }

    func sleep(for seconds: TimeInterval) async throws {
        let id = UUID()

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let shouldCancel = state.withValue { state in
                    if Task.isCancelled || state.cancelledBeforeRegistration.remove(id) != nil {
                        return true
                    }
                    state.requestedDurations.append(seconds)
                    state.pendingSleeps.append(PendingSleep(id: id, continuation: continuation))
                    return false
                }

                if shouldCancel {
                    continuation.resume(throwing: CancellationError())
                }
            }
        } onCancel: {
            self.cancelSleep(id: id)
        }
    }

    var pendingSleepCount: Int {
        state.withValue { $0.pendingSleeps.count }
    }

    var durations: [TimeInterval] {
        state.withValue { $0.requestedDurations }
    }

    @discardableResult
    func resumeNextSleep() -> Bool {
        let continuation = state.withValue { state -> CheckedContinuation<Void, Error>? in
            guard !state.pendingSleeps.isEmpty else { return nil }
            return state.pendingSleeps.removeFirst().continuation
        }
        continuation?.resume()
        return continuation != nil
    }

    private func cancelSleep(id: UUID) {
        let continuation = state.withValue { state -> CheckedContinuation<Void, Error>? in
            if let index = state.pendingSleeps.firstIndex(where: { $0.id == id }) {
                return state.pendingSleeps.remove(at: index).continuation
            }
            state.cancelledBeforeRegistration.insert(id)
            return nil
        }
        continuation?.resume(throwing: CancellationError())
    }
}

struct FixedWallClock: WallClock {
    let now: Date

    func sleep(for seconds: TimeInterval) async throws {}
}

actor SchedulerRequestRecorder {
    private var storage: [CaptureRequest] = []

    func append(_ request: CaptureRequest) {
        storage.append(request)
    }

    var requests: [CaptureRequest] {
        storage
    }
}

enum FakeCaptureOutcome: @unchecked Sendable {
    case image(CGImage)
    case failure(Error)
}

actor FakeScreenCapturer: ScreenCapturing {
    private let fallbackImage: CGImage
    private var outcomes: [FakeCaptureOutcome]
    private var capturesToBlock: Int
    private var blockedContinuations: [CheckedContinuation<Void, Never>] = []
    private var activeCaptures = 0
    private(set) var captureCallCount = 0
    private(set) var maximumActiveCaptures = 0

    init(
        image: CGImage = makeTestImage(),
        outcomes: [FakeCaptureOutcome] = [],
        capturesToBlock: Int = 0
    ) {
        fallbackImage = image
        self.outcomes = outcomes
        self.capturesToBlock = capturesToBlock
    }

    func captureMainDisplay() async throws -> CGImage {
        captureCallCount += 1
        activeCaptures += 1
        maximumActiveCaptures = max(maximumActiveCaptures, activeCaptures)
        defer { activeCaptures -= 1 }

        if capturesToBlock > 0 {
            capturesToBlock -= 1
            await withCheckedContinuation { continuation in
                blockedContinuations.append(continuation)
            }
        }

        guard !outcomes.isEmpty else { return fallbackImage }
        switch outcomes.removeFirst() {
        case .image(let image):
            return image
        case .failure(let error):
            throw error
        }
    }

    var blockedCaptureCount: Int {
        blockedContinuations.count
    }

    @discardableResult
    func releaseNextCapture() -> Bool {
        guard !blockedContinuations.isEmpty else { return false }
        blockedContinuations.removeFirst().resume()
        return true
    }
}

enum FakeEncodingOutcome: @unchecked Sendable {
    case data(Data)
    case failure(Error)
}

final class FakeJPEGEncoder: JPEGEncoding, @unchecked Sendable {
    private struct State {
        var outcomes: [FakeEncodingOutcome]
        var qualities: [Double] = []
    }

    private let fallbackData: Data
    private let state: LockedBox<State>

    init(data: Data = Data([0xFF, 0xD8, 0xFF]), outcomes: [FakeEncodingOutcome] = []) {
        fallbackData = data
        state = LockedBox(State(outcomes: outcomes))
    }

    func encode(_ image: CGImage, quality: Double) throws -> Data {
        let outcome = state.withValue { state -> FakeEncodingOutcome? in
            state.qualities.append(quality)
            guard !state.outcomes.isEmpty else { return nil }
            return state.outcomes.removeFirst()
        }

        switch outcome {
        case .data(let data):
            return data
        case .failure(let error):
            throw error
        case nil:
            return fallbackData
        }
    }

    var qualities: [Double] {
        state.withValue { $0.qualities }
    }
}

struct UploadCall: Equatable, Sendable {
    let data: Data
    let captureID: UUID
    let remoteTaskID: UUID?
}

struct FailureReport: Equatable, Sendable {
    let taskID: UUID
    let code: CaptureFailureCode
    let message: String
}

enum FakeUploadOutcome: @unchecked Sendable {
    case success
    case failure(Error)
}

actor FakeCaptureUploader: CaptureUploading {
    private var outcomes: [FakeUploadOutcome]
    private var uploadStorage: [UploadCall] = []
    private var reportStorage: [FailureReport] = []

    init(outcomes: [FakeUploadOutcome] = []) {
        self.outcomes = outcomes
    }

    func upload(_ data: Data, captureID: UUID, remoteTaskID: UUID?) async throws {
        uploadStorage.append(UploadCall(data: data, captureID: captureID, remoteTaskID: remoteTaskID))
        guard !outcomes.isEmpty else { return }

        switch outcomes.removeFirst() {
        case .success:
            return
        case .failure(let error):
            throw error
        }
    }

    func reportFailure(taskID: UUID, code: CaptureFailureCode, message: String) async {
        reportStorage.append(FailureReport(taskID: taskID, code: code, message: message))
    }

    var uploads: [UploadCall] {
        uploadStorage
    }

    var failureReports: [FailureReport] {
        reportStorage
    }
}

enum StubError: Error {
    case captureTransport
    case encodingTransport
    case uploadTransport
}

func makeTestImage() -> CGImage {
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let context = CGContext(
        data: nil,
        width: 1,
        height: 1,
        bitsPerComponent: 8,
        bytesPerRow: 4,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
    return context.makeImage()!
}

func waitUntil(
    timeoutNanoseconds: UInt64 = 1_000_000_000,
    condition: @escaping () async -> Bool
) async -> Bool {
    let pollNanoseconds: UInt64 = 1_000_000
    var elapsed: UInt64 = 0

    while elapsed < timeoutNanoseconds {
        if await condition() { return true }
        try? await Task.sleep(nanoseconds: pollNanoseconds)
        elapsed += pollNanoseconds
    }
    return await condition()
}

func assertEventually(
    timeoutNanoseconds: UInt64 = 1_000_000_000,
    file: StaticString = #filePath,
    line: UInt = #line,
    condition: @escaping () async -> Bool
) async {
    let result = await waitUntil(timeoutNanoseconds: timeoutNanoseconds, condition: condition)
    XCTAssertTrue(result, file: file, line: line)
}
