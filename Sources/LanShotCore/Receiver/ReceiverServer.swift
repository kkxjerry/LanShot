import Foundation
import Network
import NIOCore
import NIOHTTP1
import NIOTransportServices

public enum ReceiverServerError: Error, Equatable, Sendable {
    case listenerDidNotBind
}

public final class ReceiverServer: @unchecked Sendable {
    public static let defaultPort: UInt16 = 8787
    public static let serviceType = "_lanshot._tcp"

    private let router: ReceiverRouter
    private let host: String
    private let configuredPort: UInt16
    private let advertiseBonjour: Bool
    private let lock = NSLock()
    private var group: NIOTSEventLoopGroup?
    private var channel: Channel?
    private var listener: NWListener?
    private var currentPort: UInt16?

    public init(
        router: ReceiverRouter,
        host: String = "0.0.0.0",
        port: UInt16 = ReceiverServer.defaultPort,
        advertiseBonjour: Bool = true
    ) {
        self.router = router
        self.host = host
        configuredPort = port
        self.advertiseBonjour = advertiseBonjour
    }

    @discardableResult
    public func start() throws -> UInt16 {
        lock.lock()
        defer { lock.unlock() }
        if let currentPort { return currentPort }

        let nwPort: NWEndpoint.Port = configuredPort == 0
            ? .any
            : NWEndpoint.Port(rawValue: configuredPort)!
        let parameters = NWParameters.tcp
        if host != "0.0.0.0" && host != "::" {
            parameters.requiredLocalEndpoint = .hostPort(host: NWEndpoint.Host(host), port: nwPort)
        }
        parameters.allowLocalEndpointReuse = true
        let listener = try NWListener(using: parameters, on: nwPort)
        if advertiseBonjour {
            listener.service = .init(type: Self.serviceType)
        }

        let group = NIOTSEventLoopGroup(loopCount: 1)
        do {
            let channel = try NIOTSListenerBootstrap(group: group)
                .childChannelInitializer { [router] child in
                    child.eventLoop.makeCompletedFuture {
                        try child.pipeline.syncOperations.configureHTTPServerPipeline(
                            withPipeliningAssistance: true,
                            withErrorHandling: true
                        )
                        try child.pipeline.syncOperations.addHandler(
                            ReceiverHTTPHandler(router: router)
                        )
                    }
                }
                .withNWListener(listener)
                .wait()
            guard let boundPort = listener.port?.rawValue else {
                try channel.close().wait()
                try group.syncShutdownGracefully()
                throw ReceiverServerError.listenerDidNotBind
            }
            self.group = group
            self.channel = channel
            self.listener = listener
            currentPort = boundPort
            return boundPort
        } catch {
            try? group.syncShutdownGracefully()
            throw error
        }
    }

    public func stop() {
        let channel: Channel?
        let group: NIOTSEventLoopGroup?
        lock.lock()
        channel = self.channel
        group = self.group
        self.channel = nil
        self.group = nil
        listener = nil
        currentPort = nil
        lock.unlock()

        try? channel?.close().wait()
        try? group?.syncShutdownGracefully()
    }

    deinit {
        stop()
    }
}
