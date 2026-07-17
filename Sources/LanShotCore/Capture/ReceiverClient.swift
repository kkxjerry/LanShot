import Foundation
import Network
import NIOCore
import NIOFoundationCompat
import NIOHTTP1
import NIOTransportServices

public enum ClientHTTPMethod: String, Equatable, Sendable {
    case get = "GET"
    case post = "POST"
}

public struct ClientHTTPRequest: Equatable, Sendable {
    public let method: ClientHTTPMethod
    public let path: String
    public let headers: [String: String]
    public let body: Data

    public init(
        method: ClientHTTPMethod,
        path: String,
        headers: [String: String] = [:],
        body: Data = Data()
    ) {
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
    }

    public func header(named name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }
}

public struct ClientHTTPResponse: Equatable, Sendable {
    public let statusCode: Int
    public let headers: [String: String]
    public let body: Data

    public init(
        statusCode: Int,
        headers: [String: String] = [:],
        body: Data = Data()
    ) {
        self.statusCode = statusCode
        self.headers = headers
        self.body = body
    }

    public func header(named name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }
}

public protocol HTTPClientTransport: Sendable {
    func send(_ request: ClientHTTPRequest, to endpoint: NWEndpoint) async throws -> ClientHTTPResponse
}

public struct RemoteCaptureTask: Codable, Equatable, Sendable {
    public let id: UUID
    public let expiresAt: Date

    public init(id: UUID, expiresAt: Date) {
        self.id = id
        self.expiresAt = expiresAt
    }
}

public enum ReceiverClientError: Error, Equatable, Sendable {
    case receiverUnavailable
    case requestRejected(statusCode: Int)
    case serverUnavailable
    case invalidResponse
}

extension ReceiverClientError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .receiverUnavailable:
            return "The receiver is unavailable."
        case .requestRejected:
            return "The receiver rejected the request."
        case .serverUnavailable:
            return "The receiver could not complete the request."
        case .invalidResponse:
            return "The receiver returned an invalid response."
        }
    }
}

public actor ReceiverClient: CaptureUploading {
    private static let retryDelays: [TimeInterval] = [0.5, 1, 2]

    private var endpoint: NWEndpoint?
    private let transport: any HTTPClientTransport
    private let clock: any WallClock

    public init(
        endpoint: NWEndpoint? = nil,
        transport: any HTTPClientTransport = NIOTSHTTPClientTransport(),
        clock: any WallClock = SystemWallClock()
    ) {
        self.endpoint = endpoint
        self.transport = transport
        self.clock = clock
    }

    public var hasEndpoint: Bool { endpoint != nil }

    public func setEndpoint(_ endpoint: NWEndpoint?) {
        self.endpoint = endpoint
    }

    public func nextTask() async throws -> RemoteCaptureTask? {
        let response = try await sendWithRetry(
            ClientHTTPRequest(method: .get, path: "/api/v1/agent/next?timeout=25")
        )

        switch response.statusCode {
        case 204:
            return nil
        case 200:
            do {
                return try APIJSON.decoder.decode(RemoteCaptureTask.self, from: response.body)
            } catch {
                throw ReceiverClientError.invalidResponse
            }
        default:
            throw statusError(response.statusCode)
        }
    }

    public func upload(_ data: Data, captureID: UUID, remoteTaskID: UUID?) async throws {
        var headers = ["Content-Type": "image/jpeg"]
        let path: String

        if let remoteTaskID {
            path = "/api/v1/tasks/\(remoteTaskID.uuidString.lowercased())/image"
        } else {
            path = "/api/v1/images"
            headers["X-LanShot-Capture-ID"] = captureID.uuidString.lowercased()
        }

        let response = try await sendWithRetry(
            ClientHTTPRequest(method: .post, path: path, headers: headers, body: data)
        )
        guard response.statusCode == 200 || response.statusCode == 201 else {
            throw statusError(response.statusCode)
        }
    }

    public func reportFailure(taskID: UUID, code: CaptureFailureCode, message: String) async {
        let body: Data
        do {
            body = try APIJSON.encoder.encode(APIError(code: code.rawValue, message: message))
        } catch {
            return
        }

        do {
            let response = try await sendWithRetry(ClientHTTPRequest(
                method: .post,
                path: "/api/v1/tasks/\(taskID.uuidString.lowercased())/failure",
                headers: ["Content-Type": "application/json"],
                body: body
            ))
            guard response.statusCode == 200 else { return }
        } catch {
            // CaptureUploading intentionally makes failure reporting best-effort.
        }
    }

    private func sendWithRetry(_ request: ClientHTTPRequest) async throws -> ClientHTTPResponse {
        guard let endpoint else { throw ReceiverClientError.receiverUnavailable }

        for attempt in 0...Self.retryDelays.count {
            try Task.checkCancellation()

            do {
                let response = try await transport.send(request, to: endpoint)
                guard (500...599).contains(response.statusCode), attempt < Self.retryDelays.count else {
                    return response
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                guard attempt < Self.retryDelays.count else {
                    throw ReceiverClientError.receiverUnavailable
                }
            }

            try await clock.sleep(for: Self.retryDelays[attempt])
        }

        throw ReceiverClientError.receiverUnavailable
    }

    private func statusError(_ statusCode: Int) -> ReceiverClientError {
        if (400...499).contains(statusCode) {
            return .requestRejected(statusCode: statusCode)
        }
        if (500...599).contains(statusCode) {
            return .serverUnavailable
        }
        return .invalidResponse
    }
}

public final class NIOTSHTTPClientTransport: HTTPClientTransport, @unchecked Sendable {
    private static let sharedEventLoopGroup = NIOTSEventLoopGroup(loopCount: 1)
    private let eventLoopGroup: NIOTSEventLoopGroup

    public init() {
        eventLoopGroup = Self.sharedEventLoopGroup
    }

    public func send(_ request: ClientHTTPRequest, to endpoint: NWEndpoint) async throws -> ClientHTTPResponse {
        guard request.path.first == "/", !request.path.contains("\r"), !request.path.contains("\n") else {
            throw ReceiverClientError.invalidResponse
        }

        let eventLoop = eventLoopGroup.next()
        let responsePromise = eventLoop.makePromise(of: ClientHTTPResponse.self)
        let connection = HTTPClientConnectionState()
        let bootstrap = NIOTSConnectionBootstrap(group: eventLoop)
            .connectTimeout(.seconds(10))
            .channelInitializer { channel in
                channel.pipeline.addHTTPClientHandlers().flatMap {
                    channel.pipeline.addHandler(
                        ClientHTTPResponseHandler(responsePromise: responsePromise)
                    )
                }
            }

        return try await withTaskCancellationHandler {
            let channel = try await bootstrap.connect(endpoint: endpoint).get()
            connection.set(channel)
            try Task.checkCancellation()

            var headers = HTTPHeaders()
            for (name, value) in request.headers {
                headers.add(name: name, value: value)
            }
            if !headers.contains(name: "Host") {
                headers.add(name: "Host", value: "lanshot.local")
            }
            headers.replaceOrAdd(name: "Content-Length", value: "\(request.body.count)")
            headers.replaceOrAdd(name: "Connection", value: "close")

            let method: HTTPMethod = request.method == .get ? .GET : .POST
            let head = HTTPRequestHead(version: .http1_1, method: method, uri: request.path, headers: headers)
            try await channel.write(NIOAny(HTTPClientRequestPart.head(head))).get()

            if !request.body.isEmpty {
                var buffer = channel.allocator.buffer(capacity: request.body.count)
                buffer.writeBytes(request.body)
                try await channel.write(NIOAny(HTTPClientRequestPart.body(.byteBuffer(buffer)))).get()
            }

            try await channel.writeAndFlush(NIOAny(HTTPClientRequestPart.end(nil))).get()
            let response = try await responsePromise.futureResult.get()
            connection.close()
            return response
        } onCancel: {
            connection.cancel()
        }
    }
}

private final class HTTPClientConnectionState: @unchecked Sendable {
    private let lock = NSLock()
    private var channel: Channel?
    private var isCancelled = false

    func set(_ channel: Channel) {
        let shouldClose = lock.withLock { () -> Bool in
            self.channel = channel
            return isCancelled
        }
        if shouldClose { channel.close(promise: nil) }
    }

    func cancel() {
        let channel = lock.withLock { () -> Channel? in
            isCancelled = true
            return self.channel
        }
        channel?.close(promise: nil)
    }

    func close() {
        lock.withLock { channel }?.close(promise: nil)
    }
}

private final class ClientHTTPResponseHandler: ChannelInboundHandler, @unchecked Sendable {
    typealias InboundIn = HTTPClientResponsePart

    private static let maximumBodyBytes = 1_048_576
    private let responsePromise: EventLoopPromise<ClientHTTPResponse>
    private var head: HTTPResponseHead?
    private var body = Data()
    private var isComplete = false

    init(responsePromise: EventLoopPromise<ClientHTTPResponse>) {
        self.responsePromise = responsePromise
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        switch unwrapInboundIn(data) {
        case .head(let head):
            guard self.head == nil else { return fail(context) }
            self.head = head
        case .body(let buffer):
            guard body.count + buffer.readableBytes <= Self.maximumBodyBytes else {
                return fail(context)
            }
            body.append(contentsOf: buffer.readableBytesView)
        case .end:
            guard let head else { return fail(context) }
            var headers: [String: String] = [:]
            for (name, value) in head.headers {
                headers[name.lowercased()] = value
            }
            complete(.success(ClientHTTPResponse(
                statusCode: Int(head.status.code),
                headers: headers,
                body: body
            )))
            context.close(promise: nil)
        }
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        complete(.failure(error))
        context.close(promise: nil)
    }

    func channelInactive(context: ChannelHandlerContext) {
        if !isComplete {
            complete(.failure(ReceiverClientError.invalidResponse))
        }
        context.fireChannelInactive()
    }

    private func fail(_ context: ChannelHandlerContext) {
        complete(.failure(ReceiverClientError.invalidResponse))
        context.close(promise: nil)
    }

    private func complete(_ result: Result<ClientHTTPResponse, Error>) {
        guard !isComplete else { return }
        isComplete = true
        responsePromise.completeWith(result)
    }
}
