import Foundation

public final class ReceiverModeService: @unchecked Sendable {
    private let server: ReceiverServer
    private let retention: RetentionScheduler
    private let lock = NSLock()
    private var running = false

    public init(server: ReceiverServer, retention: RetentionScheduler) {
        self.server = server
        self.retention = retention
    }

    @discardableResult
    public func start() throws -> UInt16 {
        lock.lock()
        defer { lock.unlock() }
        if running {
            return try server.start()
        }

        retention.start()
        do {
            let port = try server.start()
            running = true
            return port
        } catch {
            retention.stop()
            throw error
        }
    }

    public func stop() {
        lock.lock()
        let wasRunning = running
        running = false
        lock.unlock()
        guard wasRunning else { return }
        server.stop()
        retention.stop()
    }

    deinit {
        stop()
    }
}
