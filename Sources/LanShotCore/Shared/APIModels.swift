import Foundation

public enum CaptureTaskStatus: String, Codable, Sendable {
    case pending
    case running
    case completed
    case failed
    case expired
}

public enum CaptureFailureCode: String, Codable, CaseIterable, Sendable {
    case permissionRequired = "permission_required"
    case sessionLocked = "session_locked"
    case displayAsleep = "display_asleep"
    case captureFailed = "capture_failed"
    case encodingFailed = "encoding_failed"
}

public struct APIError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
}

public struct CaptureTaskResponse: Codable, Equatable, Sendable {
    public let id: UUID
    public let status: CaptureTaskStatus
    public let expiresAt: Date
    public let error: APIError?
}

public enum APIJSON {
    public static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    public static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}
