import Foundation

public enum RuntimeStatus: Equatable, Sendable {
    case idle
    case ready
    case capturing
    case uploading
    case completed(Date)
    case receiverOffline
    case permissionRequired
    case macUnavailable(String)
    case uploadFailed(String)
}
