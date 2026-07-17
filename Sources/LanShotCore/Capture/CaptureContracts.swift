import CoreGraphics
import Foundation

public protocol ScreenCapturing: Sendable {
    func captureMainDisplay() async throws -> CGImage
}

public protocol JPEGEncoding: Sendable {
    func encode(_ image: CGImage, quality: Double) throws -> Data
}

public protocol CaptureUploading: Sendable {
    func upload(_ data: Data, captureID: UUID, remoteTaskID: UUID?) async throws
    func reportFailure(taskID: UUID, code: CaptureFailureCode, message: String) async
}

public enum CaptureInterval: Int, CaseIterable, Codable, Sendable {
    case one = 1
    case five = 5
    case ten = 10
    case thirty = 30
    case sixty = 60

    public var seconds: TimeInterval {
        TimeInterval(rawValue * 60)
    }
}

public enum CaptureRequest: Equatable, Sendable {
    case scheduled(UUID)
    case local(UUID)
    case remote(UUID)

    public var captureID: UUID {
        switch self {
        case .scheduled(let id), .local(let id), .remote(let id):
            return id
        }
    }

    public var remoteTaskID: UUID? {
        guard case .remote(let id) = self else { return nil }
        return id
    }
}

public enum CaptureError: Error, Equatable, Sendable {
    case permissionRequired
    case sessionLocked
    case displayAsleep
    case captureFailed(String)
    case encodingFailed(String)

    public var failureCode: CaptureFailureCode {
        switch self {
        case .permissionRequired:
            return .permissionRequired
        case .sessionLocked:
            return .sessionLocked
        case .displayAsleep:
            return .displayAsleep
        case .captureFailed:
            return .captureFailed
        case .encodingFailed:
            return .encodingFailed
        }
    }

    public var message: String {
        switch self {
        case .permissionRequired:
            return "Screen recording permission is required."
        case .sessionLocked:
            return "The Mac session is locked."
        case .displayAsleep:
            return "The display is asleep."
        case .captureFailed(let message), .encodingFailed(let message):
            return message
        }
    }
}

extension CaptureError: LocalizedError {
    public var errorDescription: String? { message }
}
