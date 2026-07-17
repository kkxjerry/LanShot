import Foundation

public actor CaptureScheduler {
    public private(set) var interval: CaptureInterval
    public private(set) var isEnabled = false

    private let clock: any WallClock
    private let idGenerator: @Sendable () -> UUID
    private let handler: @Sendable (CaptureRequest) async -> Void
    private var loopTask: Task<Void, Never>?

    public init(
        interval: CaptureInterval = .five,
        clock: any WallClock = SystemWallClock(),
        idGenerator: @escaping @Sendable () -> UUID = { UUID() },
        handler: @escaping @Sendable (CaptureRequest) async -> Void
    ) {
        self.interval = interval
        self.clock = clock
        self.idGenerator = idGenerator
        self.handler = handler
    }

    deinit {
        loopTask?.cancel()
    }

    public func start() {
        guard !isEnabled else { return }
        isEnabled = true
        startLoop()
    }

    public func stop() {
        guard isEnabled || loopTask != nil else { return }
        isEnabled = false
        loopTask?.cancel()
        loopTask = nil
    }

    public func setInterval(_ interval: CaptureInterval) {
        guard self.interval != interval else { return }
        self.interval = interval

        guard isEnabled else { return }
        loopTask?.cancel()
        startLoop()
    }

    private func startLoop() {
        let seconds = interval.seconds
        let clock = clock
        let idGenerator = idGenerator
        let handler = handler

        loopTask = Task {
            while !Task.isCancelled {
                do {
                    try await clock.sleep(for: seconds)
                } catch {
                    return
                }

                guard !Task.isCancelled else { return }
                await handler(.scheduled(idGenerator()))
            }
        }
    }
}
