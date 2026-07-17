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

    public init(code: String, message: String) {
        self.code = code
        self.message = message
    }
}

public struct CaptureTaskResponse: Codable, Equatable, Sendable {
    public let id: UUID
    public let status: CaptureTaskStatus
    public let expiresAt: Date
    public let error: APIError?

    public init(id: UUID, status: CaptureTaskStatus, expiresAt: Date, error: APIError?) {
        self.id = id
        self.status = status
        self.expiresAt = expiresAt
        self.error = error
    }
}

public enum APIJSON {
    public static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    public static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
