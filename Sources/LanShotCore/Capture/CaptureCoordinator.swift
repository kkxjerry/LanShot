import CoreGraphics
import Foundation

public actor CaptureCoordinator {
    private struct PendingCapture {
        let request: CaptureRequest
        let continuation: CheckedContinuation<RuntimeStatus, Never>
    }

    private let capturer: any ScreenCapturing
    private let encoder: any JPEGEncoding
    private let uploader: any CaptureUploading
    private let clock: any WallClock

    private var queue: [PendingCapture] = []
    private var isDraining = false
    private var drainTask: Task<Void, Never>?
    private var completedRemoteIDs: Set<UUID> = []
    private var completedRemoteIDOrder: [UUID] = []

    public init(
        capturer: any ScreenCapturing,
        encoder: any JPEGEncoding,
        uploader: any CaptureUploading,
        clock: any WallClock = SystemWallClock()
    ) {
        self.capturer = capturer
        self.encoder = encoder
        self.uploader = uploader
        self.clock = clock
    }

    @discardableResult
    public func submit(_ request: CaptureRequest) async -> RuntimeStatus {
        await withCheckedContinuation { continuation in
            queue.append(PendingCapture(request: request, continuation: continuation))
            startDrainIfNeeded()
        }
    }

    private func startDrainIfNeeded() {
        guard !isDraining else { return }
        isDraining = true
        drainTask = Task { await self.drainQueue() }
    }

    private func drainQueue() async {
        while !queue.isEmpty {
            let pending = queue.removeFirst()
            let status: RuntimeStatus

            if let taskID = pending.request.remoteTaskID, completedRemoteIDs.contains(taskID) {
                status = .completed(clock.now)
            } else {
                status = await perform(pending.request)
                if case .completed = status, let taskID = pending.request.remoteTaskID {
                    rememberCompletedRemoteID(taskID)
                }
            }

            pending.continuation.resume(returning: status)
        }

        isDraining = false
        drainTask = nil
    }

    private func perform(_ request: CaptureRequest) async -> RuntimeStatus {
        let image: CGImage
        do {
            image = try await capturer.captureMainDisplay()
        } catch let error as CaptureError {
            return await handle(error, for: request)
        } catch {
            return await handle(.captureFailed("Screen capture failed."), for: request)
        }

        let data: Data
        do {
            data = try encoder.encode(image, quality: 0.8)
        } catch let error as CaptureError {
            return await handle(error, for: request)
        } catch {
            return await handle(.encodingFailed("JPEG encoding failed."), for: request)
        }

        do {
            try await uploader.upload(
                data,
                captureID: request.captureID,
                remoteTaskID: request.remoteTaskID
            )
        } catch {
            return .uploadFailed("Upload failed.")
        }

        return .completed(clock.now)
    }

    private func handle(_ error: CaptureError, for request: CaptureRequest) async -> RuntimeStatus {
        if let taskID = request.remoteTaskID {
            await uploader.reportFailure(
                taskID: taskID,
                code: error.failureCode,
                message: error.message
            )
        }

        switch error {
        case .permissionRequired:
            return .permissionRequired
        case .sessionLocked, .displayAsleep, .captureFailed, .encodingFailed:
            return .macUnavailable(error.message)
        }
    }

    private func rememberCompletedRemoteID(_ id: UUID) {
        guard completedRemoteIDs.insert(id).inserted else { return }
        completedRemoteIDOrder.append(id)

        if completedRemoteIDOrder.count > 100 {
            completedRemoteIDs.remove(completedRemoteIDOrder.removeFirst())
        }
    }
}
