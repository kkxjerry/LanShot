import Foundation
import Network

public protocol ReceiverDiscovering: Sendable {
    var selectedEndpoint: NWEndpoint? { get }
    func setUpdateHandler(_ handler: (@Sendable (NWEndpoint?) -> Void)?)
    func start()
    func stop()
}

public final class BonjourReceiverDiscovery: ReceiverDiscovering, @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.zhouguichao.LanShot.receiver-discovery")
    private let lock = NSLock()
    private var browser: NWBrowser?
    private var endpoint: NWEndpoint?
    private var updateHandler: (@Sendable (NWEndpoint?) -> Void)?

    public init() {}

    public var selectedEndpoint: NWEndpoint? {
        lock.withLock { endpoint }
    }

    public func setUpdateHandler(_ handler: (@Sendable (NWEndpoint?) -> Void)?) {
        lock.withLock { updateHandler = handler }
    }

    public func start() {
        queue.async { [weak self] in
            guard let self, self.browser == nil else { return }

            let browser = NWBrowser(
                for: .bonjour(type: "_lanshot._tcp", domain: nil),
                using: .tcp
            )
            browser.browseResultsChangedHandler = { [weak self] results, _ in
                let endpoint = results
                    .map(\.endpoint)
                    .sorted { String(describing: $0) < String(describing: $1) }
                    .first
                self?.publish(endpoint)
            }
            browser.stateUpdateHandler = { [weak self, weak browser] state in
                switch state {
                case .failed, .cancelled:
                    self?.publish(nil)
                    if let self, self.browser === browser {
                        self.browser = nil
                    }
                default:
                    break
                }
            }
            self.browser = browser
            browser.start(queue: self.queue)
        }
    }

    public func stop() {
        queue.async { [weak self] in
            guard let self else { return }
            let browser = self.browser
            self.browser = nil
            browser?.cancel()
            self.publish(nil)
        }
    }

    private func publish(_ endpoint: NWEndpoint?) {
        let handler = lock.withLock { () -> (@Sendable (NWEndpoint?) -> Void)? in
            self.endpoint = endpoint
            return updateHandler
        }
        handler?(endpoint)
    }
}
