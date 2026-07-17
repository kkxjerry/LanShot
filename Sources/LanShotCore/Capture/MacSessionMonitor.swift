import AppKit
import Foundation

public struct MacSessionState: Equatable, Sendable {
    public var isSessionActive: Bool
    public var isScreenAwake: Bool
    public var isSystemAwake: Bool

    public static let available = MacSessionState(
        isSessionActive: true,
        isScreenAwake: true,
        isSystemAwake: true
    )

    public init(isSessionActive: Bool, isScreenAwake: Bool, isSystemAwake: Bool) {
        self.isSessionActive = isSessionActive
        self.isScreenAwake = isScreenAwake
        self.isSystemAwake = isSystemAwake
    }

    public var captureError: CaptureError? {
        guard isSessionActive else { return .sessionLocked }
        guard isScreenAwake, isSystemAwake else { return .displayAsleep }
        return nil
    }
}

public final class MacSessionMonitor: @unchecked Sendable {
    private let lock = NSLock()
    private let notificationCenter: NotificationCenter
    private var state: MacSessionState
    private var observers: [NSObjectProtocol] = []

    public init(
        initialState: MacSessionState = .available,
        observeWorkspace: Bool = true,
        notificationCenter: NotificationCenter = NSWorkspace.shared.notificationCenter
    ) {
        state = initialState
        self.notificationCenter = notificationCenter
        if observeWorkspace {
            installObservers()
        }
    }

    deinit {
        for observer in observers {
            notificationCenter.removeObserver(observer)
        }
    }

    public var currentState: MacSessionState {
        lock.withLock { state }
    }

    private func installObservers() {
        observe(NSWorkspace.sessionDidResignActiveNotification) { state in
            state.isSessionActive = false
        }
        observe(NSWorkspace.sessionDidBecomeActiveNotification) { state in
            state.isSessionActive = true
        }
        observe(NSWorkspace.screensDidSleepNotification) { state in
            state.isScreenAwake = false
        }
        observe(NSWorkspace.screensDidWakeNotification) { state in
            state.isScreenAwake = true
        }
        observe(NSWorkspace.willSleepNotification) { state in
            state.isSystemAwake = false
        }
        observe(NSWorkspace.didWakeNotification) { state in
            state.isSystemAwake = true
        }
    }

    private func observe(
        _ name: Notification.Name,
        mutate: @escaping @Sendable (inout MacSessionState) -> Void
    ) {
        let observer = notificationCenter.addObserver(
            forName: name,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            self.lock.withLock {
                mutate(&self.state)
            }
        }
        observers.append(observer)
    }
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}
