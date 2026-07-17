import Foundation
import NIOCore
import NIOHTTP1

public struct HTTPResponse: Sendable {
    public let status: HTTPResponseStatus
    public let headers: HTTPHeaders
    public let body: ByteBuffer

    public init(status: HTTPResponseStatus, headers: HTTPHeaders, body: ByteBuffer) {
        self.status = status
        self.headers = headers
        self.body = body
    }
}

public actor ReceiverRouter {
    private struct AgentTaskResponse: Encodable {
        let id: UUID
        let expiresAt: Date
    }

    private struct FailureRequest: Decodable {
        let code: CaptureFailureCode
        let message: String
    }

    private struct ImageResponse: Encodable {
        let id: UUID
    }

    private let tasks: CaptureTaskStore
    private let images: ImageStore
    private let clock: any WallClock
    private var storedImageIDs: Set<UUID> = []

    public init(tasks: CaptureTaskStore, images: ImageStore, clock: any WallClock) {
        self.tasks = tasks
        self.images = images
        self.clock = clock
    }

    public func route(
        method: HTTPMethod,
        uri: String,
        headers: HTTPHeaders = HTTPHeaders(),
        body: ByteBuffer = ByteBuffer()
    ) async -> HTTPResponse {
        guard body.readableBytes <= ImageStore.maximumBytes else {
            return error(.payloadTooLarge, code: "request_too_large", message: "请求内容过大")
        }

        guard let components = URLComponents(string: uri) else {
            return error(.badRequest, code: "invalid_request", message: "请求地址无效")
        }
        let path = components.path

        if method == .GET, path == "/" {
            return html(ControlPage.html)
        }
        if method == .POST, path == "/api/v1/tasks" {
            return await createTask()
        }
        if method == .GET, path == "/api/v1/agent/next" {
            return await claimNext(components: components)
        }
        if method == .POST, path == "/api/v1/images" {
            return await storeStandaloneImage(headers: headers, body: body)
        }

        let segments = path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        if segments.count >= 4,
           segments[0] == "api", segments[1] == "v1", segments[2] == "tasks" {
            guard let id = UUID(uuidString: segments[3]) else {
                return error(.badRequest, code: "invalid_capture_id", message: "截图编号无效")
            }
            if method == .GET, segments.count == 4 {
                return await task(id: id)
            }
            if method == .POST, segments.count == 5, segments[4] == "image" {
                return await storeTaskImage(id: id, headers: headers, body: body)
            }
            if method == .POST, segments.count == 5, segments[4] == "failure" {
                return await failTask(id: id, headers: headers, body: body)
            }
        }

        return error(.notFound, code: "not_found", message: "请求不存在")
    }

    private func createTask() async -> HTTPResponse {
        switch await tasks.createOrReuseResult() {
        case .created(let response):
            return json(.created, response)
        case .reused(let response):
            return json(.ok, response)
        }
    }

    private func task(id: UUID) async -> HTTPResponse {
        guard let response = await tasks.task(id: id) else {
            return error(.notFound, code: "task_not_found", message: "截图任务不存在")
        }
        return json(.ok, response)
    }

    private func claimNext(components: URLComponents) async -> HTTPResponse {
        let requested = components.queryItems?
            .first(where: { $0.name == "timeout" })?
            .value
            .flatMap(Int.init) ?? 25
        let timeout = min(25, max(1, requested))
        let deadline = clock.now.addingTimeInterval(TimeInterval(timeout))

        while !Task.isCancelled {
            if let response = await tasks.claimNext() {
                return json(.ok, AgentTaskResponse(id: response.id, expiresAt: response.expiresAt))
            }
            let remaining = deadline.timeIntervalSince(clock.now)
            guard remaining > 0 else { break }
            do {
                try await clock.sleep(for: min(0.25, remaining))
            } catch {
                break
            }
        }
        return empty(.noContent)
    }

    private func storeTaskImage(
        id: UUID,
        headers: HTTPHeaders,
        body: ByteBuffer
    ) async -> HTTPResponse {
        guard hasMediaType("image/jpeg", headers: headers) else {
            return error(.unsupportedMediaType, code: "unsupported_media_type", message: "仅支持 JPEG 图片")
        }
        guard let current = await tasks.task(id: id) else {
            return error(.notFound, code: "task_not_found", message: "截图任务不存在")
        }
        switch current.status {
        case .completed:
            return json(.ok, current)
        case .running:
            break
        case .pending, .failed, .expired:
            return error(.gone, code: "task_gone", message: "截图任务已结束")
        }

        do {
            let storedURL = try await images.storeJPEG(
                Data(body.readableBytesView),
                captureID: id,
                now: clock.now
            )
            let result = await tasks.complete(id: id, path: storedURL.path)
            switch result {
            case .stored(let response):
                storedImageIDs.insert(id)
                return json(.created, response)
            case .duplicate(let response):
                storedImageIDs.insert(id)
                return json(.ok, response)
            case .notFound:
                return error(.notFound, code: "task_not_found", message: "截图任务不存在")
            case .gone:
                return error(.gone, code: "task_gone", message: "截图任务已结束")
            }
        } catch {
            return imageStoreError(error)
        }
    }

    private func failTask(
        id: UUID,
        headers: HTTPHeaders,
        body: ByteBuffer
    ) async -> HTTPResponse {
        guard hasMediaType("application/json", headers: headers) else {
            return error(.unsupportedMediaType, code: "unsupported_media_type", message: "请求必须使用 JSON")
        }
        let request: FailureRequest
        do {
            request = try APIJSON.decoder.decode(FailureRequest.self, from: Data(body.readableBytesView))
        } catch {
            return self.error(.badRequest, code: "invalid_json", message: "失败信息无效")
        }

        switch await tasks.fail(id: id, code: request.code, message: request.message) {
        case .stored(let response), .duplicate(let response):
            return json(.ok, response)
        case .notFound:
            return error(.notFound, code: "task_not_found", message: "截图任务不存在")
        case .gone:
            return error(.gone, code: "task_gone", message: "截图任务已结束")
        }
    }

    private func storeStandaloneImage(headers: HTTPHeaders, body: ByteBuffer) async -> HTTPResponse {
        guard hasMediaType("image/jpeg", headers: headers) else {
            return error(.unsupportedMediaType, code: "unsupported_media_type", message: "仅支持 JPEG 图片")
        }
        guard let rawID = headers.first(name: "x-lanshot-capture-id"),
              let id = UUID(uuidString: rawID) else {
            return error(.badRequest, code: "invalid_capture_id", message: "缺少有效的截图编号")
        }

        if storedImageIDs.contains(id) {
            return json(.ok, ImageResponse(id: id))
        }
        do {
            _ = try await images.storeJPEG(Data(body.readableBytesView), captureID: id, now: clock.now)
            storedImageIDs.insert(id)
            return json(.created, ImageResponse(id: id))
        } catch {
            return imageStoreError(error)
        }
    }

    private func hasMediaType(_ expected: String, headers: HTTPHeaders) -> Bool {
        guard let value = headers.first(name: "content-type") else { return false }
        return value.split(separator: ";", maxSplits: 1).first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() == expected
    }

    private func imageStoreError(_ caught: Error) -> HTTPResponse {
        switch caught as? ImageStoreError {
        case .invalidJPEG:
            return error(.badRequest, code: "invalid_jpeg", message: "JPEG 图片无效")
        case .tooLarge:
            return error(.payloadTooLarge, code: "request_too_large", message: "请求内容过大")
        case .unsafeStoragePath, .none:
            return error(.internalServerError, code: "internal_server_error", message: "服务器无法保存图片")
        }
    }

    private func json<T: Encodable>(_ status: HTTPResponseStatus, _ value: T) -> HTTPResponse {
        do {
            return response(
                status: status,
                contentType: "application/json; charset=utf-8",
                data: try APIJSON.encoder.encode(value)
            )
        } catch {
            return self.error(.internalServerError, code: "internal_server_error", message: "服务器响应失败")
        }
    }

    private func error(_ status: HTTPResponseStatus, code: String, message: String) -> HTTPResponse {
        json(status, APIError(code: code, message: message))
    }

    private func html(_ value: String) -> HTTPResponse {
        response(status: .ok, contentType: "text/html; charset=utf-8", data: Data(value.utf8))
    }

    private func empty(_ status: HTTPResponseStatus) -> HTTPResponse {
        response(status: status, contentType: nil, data: Data())
    }

    private func response(
        status: HTTPResponseStatus,
        contentType: String?,
        data: Data
    ) -> HTTPResponse {
        var headers = HTTPHeaders()
        if let contentType {
            headers.add(name: "content-type", value: contentType)
        }
        headers.add(name: "content-length", value: "\(data.count)")
        headers.add(name: "cache-control", value: "no-store")
        var body = ByteBufferAllocator().buffer(capacity: data.count)
        body.writeBytes(data)
        return HTTPResponse(status: status, headers: headers, body: body)
    }
}
