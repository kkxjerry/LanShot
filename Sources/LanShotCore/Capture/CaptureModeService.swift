import Foundation

public actor CaptureModeService {
    public private(set) var isRunning = false

    private let discovery: any ReceiverDiscovering
    private let scheduler: CaptureScheduler
    private let client: ReceiverClient
    private let coordinator: CaptureCoordinator
    private let clock: any WallClock
    private var runID = UUID()
    private var longPollTask: Task<Void, Never>?

    public init(
        discovery: any ReceiverDiscovering = BonjourReceiverDiscovery(),
        scheduler: CaptureScheduler,
        client: ReceiverClient,
        coordinator: CaptureCoordinator,
        clock: any WallClock = SystemWallClock()
    ) {
        self.discovery = discovery
        self.scheduler = scheduler
        self.client = client
        self.coordinator = coordinator
        self.clock = clock
    }

    deinit {
        longPollTask?.cancel()
    }

    public func start() async {
        guard !isRunning else { return }
        isRunning = true
        runID = UUID()
        let activeRunID = runID
        let client = client

        discovery.setUpdateHandler { endpoint in
            Task { await client.setEndpoint(endpoint) }
        }
        await client.setEndpoint(discovery.selectedEndpoint)
        discovery.start()
        await scheduler.start()

        longPollTask = Task { [weak self] in
            await self?.runLongPollLoop(runID: activeRunID)
        }
    }

    public func stop() async {
        guard isRunning || longPollTask != nil else { return }
        isRunning = false
        runID = UUID()

        let task = longPollTask
        longPollTask = nil
        task?.cancel()
        await task?.value

        await scheduler.stop()
        discovery.stop()
        discovery.setUpdateHandler(nil)
        await client.setEndpoint(nil)
    }

    private func runLongPollLoop(runID: UUID) async {
        while isRunning, self.runID == runID, !Task.isCancelled {
            guard await client.hasEndpoint else {
                if !(await pauseAfterFailure()) { return }
                continue
            }

            do {
                let remoteTask = try await client.nextTask()
                guard !Task.isCancelled, isRunning, self.runID == runID else { return }
                guard let remoteTask, remoteTask.expiresAt > clock.now else { continue }
                _ = await coordinator.submit(.remote(remoteTask.id))
            } catch is CancellationError {
                return
            } catch {
                if !(await pauseAfterFailure()) { return }
            }
        }
    }

    private func pauseAfterFailure() async -> Bool {
        do {
            try await clock.sleep(for: 0.5)
            return !Task.isCancelled
        } catch {
            return false
        }
    }
}
