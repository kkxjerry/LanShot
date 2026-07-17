import Foundation

public final class RetentionScheduler: @unchecked Sendable {
    private static let cleanupInterval: TimeInterval = 24 * 60 * 60

    private let store: ImageStore
    private let clock: any WallClock
    private let lock = NSLock()
    private var loopTask: Task<Void, Never>?

    public init(store: ImageStore, clock: any WallClock = SystemWallClock()) {
        self.store = store
        self.clock = clock
    }

    public func start() {
        lock.lock()
        defer { lock.unlock() }
        guard loopTask == nil else { return }

        let store = store
        let clock = clock
        loopTask = Task {
            while !Task.isCancelled {
                _ = try? await store.cleanup(now: clock.now)
                guard !Task.isCancelled else { return }

                do {
                    try await clock.sleep(for: Self.cleanupInterval)
                } catch {
                    return
                }
            }
        }
    }

    public func stop() {
        let task: Task<Void, Never>?
        lock.lock()
        task = loopTask
        loopTask = nil
        lock.unlock()
        task?.cancel()
    }

    deinit {
        loopTask?.cancel()
    }
}
