import Foundation
import NIOCore
import NIOHTTP1

public final class ReceiverHTTPHandler: ChannelInboundHandler, @unchecked Sendable {
    public typealias InboundIn = HTTPServerRequestPart
    public typealias OutboundOut = HTTPServerResponsePart

    private let router: ReceiverRouter
    private var requestHead: HTTPRequestHead?
    private var requestBody = ByteBuffer()
    private var rejectingRequest = false
    private var routeTask: Task<Void, Never>?

    public init(router: ReceiverRouter) {
        self.router = router
    }

    public func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        switch unwrapInboundIn(data) {
        case .head(let head):
            begin(head, context: context)
        case .body(var body):
            append(&body, context: context)
        case .end:
            finish(context: context)
        }
    }

    public func channelInactive(context: ChannelHandlerContext) {
        routeTask?.cancel()
        routeTask = nil
        context.fireChannelInactive()
    }

    public func handlerRemoved(context: ChannelHandlerContext) {
        routeTask?.cancel()
        routeTask = nil
    }

    public func errorCaught(context: ChannelHandlerContext, error: Error) {
        routeTask?.cancel()
        routeTask = nil
        context.close(promise: nil)
    }

    private func begin(_ head: HTTPRequestHead, context: ChannelHandlerContext) {
        requestHead = head
        requestBody.clear()
        rejectingRequest = false

        if let rawLength = head.headers.first(name: "content-length") {
            guard let length = Int(rawLength), length >= 0 else {
                reject(.badRequest, code: "invalid_content_length", message: "请求长度无效", context: context)
                return
            }
            if length > ImageStore.maximumBytes {
                reject(.payloadTooLarge, code: "request_too_large", message: "请求内容过大", context: context)
            }
        }
    }

    private func append(_ incoming: inout ByteBuffer, context: ChannelHandlerContext) {
        guard !rejectingRequest else { return }
        guard requestHead != nil else {
            reject(.badRequest, code: "invalid_request", message: "请求格式无效", context: context)
            return
        }
        guard incoming.readableBytes <= ImageStore.maximumBytes - requestBody.readableBytes else {
            requestBody.clear()
            reject(.payloadTooLarge, code: "request_too_large", message: "请求内容过大", context: context)
            return
        }
        requestBody.writeBuffer(&incoming)
    }

    private func finish(context: ChannelHandlerContext) {
        guard !rejectingRequest else {
            resetRequest()
            return
        }
        guard let head = requestHead else {
            reject(.badRequest, code: "invalid_request", message: "请求格式无效", context: context)
            resetRequest()
            return
        }

        let body = requestBody
        let router = router
        let channel = context.channel
        resetRequest()
        routeTask = Task {
            let response = await router.route(
                method: head.method,
                uri: head.uri,
                headers: head.headers,
                body: body
            )
            guard !Task.isCancelled else { return }
            channel.eventLoop.execute {
                guard channel.isActive else { return }
                let responseHead = HTTPResponseHead(
                    version: head.version,
                    status: response.status,
                    headers: response.headers
                )
                channel.write(HTTPServerResponsePart.head(responseHead), promise: nil)
                if response.body.readableBytes > 0 {
                    channel.write(
                        HTTPServerResponsePart.body(.byteBuffer(response.body)),
                        promise: nil
                    )
                }
                channel.writeAndFlush(HTTPServerResponsePart.end(nil), promise: nil)
            }
        }
    }

    private func reject(
        _ status: HTTPResponseStatus,
        code: String,
        message: String,
        context: ChannelHandlerContext
    ) {
        guard !rejectingRequest else { return }
        rejectingRequest = true
        let data = (try? APIJSON.encoder.encode(APIError(code: code, message: message))) ?? Data()
        var headers = HTTPHeaders()
        headers.add(name: "content-type", value: "application/json; charset=utf-8")
        headers.add(name: "content-length", value: "\(data.count)")
        headers.add(name: "cache-control", value: "no-store")
        var body = ByteBufferAllocator().buffer(capacity: data.count)
        body.writeBytes(data)
        context.write(wrapOutboundOut(.head(HTTPResponseHead(
            version: requestHead?.version ?? .http1_1,
            status: status,
            headers: headers
        ))), promise: nil)
        if body.readableBytes > 0 {
            context.write(wrapOutboundOut(.body(.byteBuffer(body))), promise: nil)
        }
        context.writeAndFlush(wrapOutboundOut(.end(nil)), promise: nil)
    }

    private func resetRequest() {
        requestHead = nil
        requestBody.clear()
        rejectingRequest = false
    }
}
