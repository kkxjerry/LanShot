import CoreGraphics
import CoreImage
import CoreMedia
import Foundation
import ScreenCaptureKit

enum ScreenCaptureSupport {
    static func indexOfDisplay(
        matching displayID: CGDirectDisplayID,
        displayIDs: [CGDirectDisplayID]
    ) -> Int? {
        displayIDs.firstIndex(of: displayID)
    }

    static func shouldAcceptFrame(status: SCFrameStatus, hasImageBuffer: Bool) -> Bool {
        status == .complete && hasImageBuffer
    }
}

public final class ScreenCapturer: ScreenCapturing, @unchecked Sendable {
    private let sessionMonitor: MacSessionMonitor

    public init(sessionMonitor: MacSessionMonitor = MacSessionMonitor()) {
        self.sessionMonitor = sessionMonitor
    }

    public func hasPermission() -> Bool {
        CGPreflightScreenCaptureAccess()
    }

    @discardableResult
    public func requestPermission() -> Bool {
        CGRequestScreenCaptureAccess()
    }

    public func captureMainDisplay() async throws -> CGImage {
        guard hasPermission() else { throw CaptureError.permissionRequired }
        if let stateError = sessionMonitor.currentState.captureError {
            throw stateError
        }

        let mainDisplayID = CGMainDisplayID()
        guard CGDisplayIsAsleep(mainDisplayID) == 0 else {
            throw CaptureError.displayAsleep
        }

        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: true
            )
        } catch {
            throw CaptureError.captureFailed("Could not read shareable screen content.")
        }

        let displayIDs = content.displays.map(\.displayID)
        guard let index = ScreenCaptureSupport.indexOfDisplay(
            matching: mainDisplayID,
            displayIDs: displayIDs
        ) else {
            throw CaptureError.captureFailed("The main display is unavailable.")
        }

        let display = content.displays[index]
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = display.width
        configuration.height = display.height
        configuration.showsCursor = true
        configuration.queueDepth = 1

        if #available(macOS 14.0, *) {
            do {
                return try await SCScreenshotManager.captureImage(
                    contentFilter: filter,
                    configuration: configuration
                )
            } catch {
                throw CaptureError.captureFailed("Screen capture failed.")
            }
        }

        return try await LegacyScreenshotCapture().capture(
            filter: filter,
            configuration: configuration
        )
    }
}

private final class LegacyScreenshotCapture: NSObject, SCStreamOutput, SCStreamDelegate,
    @unchecked Sendable {
    private let lock = NSLock()
    private let outputQueue = DispatchQueue(label: "com.zhouguichao.LanShot.legacy-capture")
    private let ciContext = CIContext(options: nil)
    private var continuation: CheckedContinuation<CGImage, Error>?
    private var stream: SCStream?
    private var finished = false
    private var cancellationRequested = false

    func capture(filter: SCContentFilter, configuration: SCStreamConfiguration) async throws -> CGImage {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let wasCancelled = lock.withLock {
                    guard !cancellationRequested else {
                        finished = true
                        return true
                    }
                    self.continuation = continuation
                    return false
                }
                guard !wasCancelled else {
                    continuation.resume(throwing: CancellationError())
                    return
                }

                let stream = SCStream(filter: filter, configuration: configuration, delegate: self)

                do {
                    try stream.addStreamOutput(
                        self,
                        type: .screen,
                        sampleHandlerQueue: outputQueue
                    )
                } catch {
                    finish(.failure(CaptureError.captureFailed("Could not start screen capture.")))
                    return
                }

                let shouldStart = lock.withLock {
                    guard !finished else { return false }
                    self.stream = stream
                    return true
                }
                guard shouldStart else {
                    try? stream.removeStreamOutput(self, type: .screen)
                    return
                }

                Task { [weak self, weak stream] in
                    guard let self, !self.isFinished else { return }
                    do {
                        try await stream?.startCapture()
                        if self.isFinished, let stream {
                            try? stream.removeStreamOutput(self, type: .screen)
                            try? await stream.stopCapture()
                        }
                    } catch {
                        self.finish(.failure(CaptureError.captureFailed("Could not start screen capture.")))
                    }
                }
            }
        } onCancel: { [weak self] in
            self?.cancel()
        }
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen,
              let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(
                  sampleBuffer,
                  createIfNecessary: false
              ) as? [[SCStreamFrameInfo: Any]],
              let attachments = attachmentsArray.first,
              let rawStatus = attachments[.status] as? Int,
              let status = SCFrameStatus(rawValue: rawStatus),
              ScreenCaptureSupport.shouldAcceptFrame(
                  status: status,
                  hasImageBuffer: sampleBuffer.imageBuffer != nil
              ),
              let imageBuffer = sampleBuffer.imageBuffer else {
            return
        }

        let bounds = CGRect(
            x: 0,
            y: 0,
            width: CVPixelBufferGetWidth(imageBuffer),
            height: CVPixelBufferGetHeight(imageBuffer)
        )
        let input = CIImage(cvPixelBuffer: imageBuffer)
        guard let image = ciContext.createCGImage(input, from: bounds) else {
            finish(.failure(CaptureError.captureFailed("Could not read the captured frame.")))
            return
        }
        finish(.success(image))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        finish(.failure(CaptureError.captureFailed("Screen capture stopped unexpectedly.")))
    }

    private func finish(_ result: Result<CGImage, Error>) {
        let values: (CheckedContinuation<CGImage, Error>?, SCStream?) = lock.withLock {
            guard !finished else { return (nil, nil) }
            finished = true
            let values = (continuation, stream)
            continuation = nil
            stream = nil
            return values
        }

        guard let continuation = values.0 else { return }
        continuation.resume(with: result)
        if let stream = values.1 {
            Task {
                try? stream.removeStreamOutput(self, type: .screen)
                try? await stream.stopCapture()
            }
        }
    }

    private var isFinished: Bool {
        lock.withLock { finished }
    }

    private func cancel() {
        let values: (CheckedContinuation<CGImage, Error>?, SCStream?) = lock.withLock {
            cancellationRequested = true
            guard let continuation, !finished else { return (nil, nil) }
            finished = true
            let values = (continuation, stream)
            self.continuation = nil
            self.stream = nil
            return values
        }

        guard let continuation = values.0 else { return }
        continuation.resume(throwing: CancellationError())
        if let stream = values.1 {
            Task {
                try? stream.removeStreamOutput(self, type: .screen)
                try? await stream.stopCapture()
            }
        }
    }
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}
