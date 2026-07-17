import Foundation
import NIOCore
import NIOHTTP1
import XCTest
@testable import LanShotCore

final class ReceiverRouterTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)
    private let jpeg = Data([0xFF, 0xD8, 0xFF, 0xD9])

    func testRootReturnsMinimalChineseControlPage() async throws {
        let fixture = try makeFixture()

        let response = await fixture.router.route(
            method: .GET,
            uri: "/",
            headers: HTTPHeaders(),
            body: ByteBuffer()
        )

        XCTAssertEqual(response.status, .ok)
        XCTAssertEqual(response.headers.first(name: "content-type"), "text/html; charset=utf-8")
        let html = response.body.getString(at: response.body.readerIndex, length: response.body.readableBytes)
        XCTAssertTrue(html?.contains("局域网截图") == true)
        XCTAssertTrue(html?.contains("立即截图") == true)
        XCTAssertTrue(html?.contains("setTimeout(poll, 500)") == true)
        XCTAssertFalse(html?.contains("登录") == true)
        XCTAssertFalse(html?.contains("<img") == true)
    }

    func testCreateReturnsCreatedThenReusedAndTaskCanBeQueried() async throws {
        let id = makeUUID(1)
        let fixture = try makeFixture(ids: [id])

        let created = await fixture.router.route(method: .POST, uri: "/api/v1/tasks")
        let reused = await fixture.router.route(method: .POST, uri: "/api/v1/tasks")
        let queried = await fixture.router.route(method: .GET, uri: "/api/v1/tasks/\(id.uuidString)")

        XCTAssertEqual(created.status, .created)
        XCTAssertEqual(reused.status, .ok)
        XCTAssertEqual(queried.status, .ok)
        XCTAssertEqual(try decodeTask(created).id, id)
        assertJSONFraming(created)
    }

    func testClaimImageCompletionAndDuplicateCreateOneFile() async throws {
        let id = makeUUID(1)
        let fixture = try makeFixture(ids: [id])
        _ = await fixture.router.route(method: .POST, uri: "/api/v1/tasks")

        let claim = await fixture.router.route(
            method: .GET,
            uri: "/api/v1/agent/next?timeout=1"
        )
        let uploaded = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(id.uuidString)/image",
            headers: ["content-type": "image/jpeg"],
            body: buffer(jpeg)
        )
        let duplicate = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(id.uuidString)/image",
            headers: ["content-type": "image/jpeg"],
            body: buffer(jpeg)
        )
        let queried = await fixture.router.route(method: .GET, uri: "/api/v1/tasks/\(id.uuidString)")

        XCTAssertEqual(claim.status, .ok)
        XCTAssertEqual(uploaded.status, .created)
        XCTAssertEqual(duplicate.status, .ok)
        XCTAssertEqual(try decodeTask(queried).status, .completed)
        XCTAssertEqual(try jpegFiles(under: fixture.root).count, 1)
    }

    func testLongPollWithoutWorkReturnsNoContent() async throws {
        let fixture = try makeFixture()

        let response = await fixture.router.route(
            method: .GET,
            uri: "/api/v1/agent/next?timeout=1"
        )

        XCTAssertEqual(response.status, .noContent)
        XCTAssertEqual(response.body.readableBytes, 0)
        XCTAssertEqual(response.headers.first(name: "content-length"), "0")
    }

    func testFailureValidationAndTerminalStatusMapping() async throws {
        let id = makeUUID(1)
        let unknown = makeUUID(2)
        let fixture = try makeFixture(ids: [id])
        _ = await fixture.router.route(method: .POST, uri: "/api/v1/tasks")
        _ = await fixture.router.route(method: .GET, uri: "/api/v1/agent/next?timeout=1")

        let invalid = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(id.uuidString)/failure",
            headers: ["content-type": "application/json"],
            body: buffer(Data(#"{"code":"made_up","message":"no"}"#.utf8))
        )
        let unknownResponse = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(unknown.uuidString)/failure",
            headers: ["content-type": "application/json"],
            body: buffer(Data(#"{"code":"capture_failed","message":"failed"}"#.utf8))
        )
        let failed = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(id.uuidString)/failure",
            headers: ["content-type": "application/json"],
            body: buffer(Data(#"{"code":"capture_failed","message":"failed"}"#.utf8))
        )
        let lateImage = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/tasks/\(id.uuidString)/image",
            headers: ["content-type": "image/jpeg"],
            body: buffer(jpeg)
        )

        XCTAssertEqual(invalid.status, .badRequest)
        XCTAssertEqual(unknownResponse.status, .notFound)
        XCTAssertEqual(failed.status, .ok)
        XCTAssertEqual(lateImage.status, .gone)
        XCTAssertEqual(try jpegFiles(under: fixture.root).count, 0)
    }

    func testStandaloneImagesRequireCaptureIDAndDeduplicate() async throws {
        let id = makeUUID(1)
        let fixture = try makeFixture()
        let missing = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/images",
            headers: ["content-type": "image/jpeg"],
            body: buffer(jpeg)
        )
        let headers: HTTPHeaders = [
            "content-type": "image/jpeg",
            "x-lanshot-capture-id": id.uuidString
        ]

        let stored = await fixture.router.route(
            method: .POST, uri: "/api/v1/images", headers: headers, body: buffer(jpeg)
        )
        let duplicate = await fixture.router.route(
            method: .POST, uri: "/api/v1/images", headers: headers, body: buffer(jpeg)
        )

        XCTAssertEqual(missing.status, .badRequest)
        XCTAssertEqual(stored.status, .created)
        XCTAssertEqual(duplicate.status, .ok)
        XCTAssertEqual(try jpegFiles(under: fixture.root).count, 1)
    }

    func testMalformedRoutesAndUploadTypesReturnStructuredErrors() async throws {
        let fixture = try makeFixture()
        let malformedID = await fixture.router.route(method: .GET, uri: "/api/v1/tasks/not-a-uuid")
        let wrongType = await fixture.router.route(
            method: .POST,
            uri: "/api/v1/images",
            headers: ["content-type": "text/plain", "x-lanshot-capture-id": UUID().uuidString],
            body: buffer(jpeg)
        )

        XCTAssertEqual(malformedID.status, .badRequest)
        XCTAssertEqual(wrongType.status, .unsupportedMediaType)
        assertJSONFraming(malformedID)
    }

    private func makeFixture(ids: [UUID] = []) throws -> Fixture {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let generator = ReceiverUUIDGenerator(ids.isEmpty ? [makeUUID(99)] : ids)
        let clock = ManualClock(now: now)
        let tasks = CaptureTaskStore(clock: clock, uuid: { generator.next() })
        let images = ImageStore(root: root)
        return Fixture(
            router: ReceiverRouter(tasks: tasks, images: images, clock: clock),
            root: root,
            temporaryDirectory: temporaryDirectory
        )
    }

    private func decodeTask(_ response: HTTPResponse) throws -> CaptureTaskResponse {
        try APIJSON.decoder.decode(CaptureTaskResponse.self, from: Data(response.body.readableBytesView))
    }

    private func assertJSONFraming(_ response: HTTPResponse, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(response.headers.first(name: "content-type"), "application/json; charset=utf-8", file: file, line: line)
        XCTAssertEqual(response.headers.first(name: "content-length"), "\(response.body.readableBytes)", file: file, line: line)
    }

    private func buffer(_ data: Data) -> ByteBuffer {
        var buffer = ByteBufferAllocator().buffer(capacity: data.count)
        buffer.writeBytes(data)
        return buffer
    }

    private func makeUUID(_ value: Int) -> UUID {
        UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", value))!
    }

    private func jpegFiles(under root: URL) throws -> [URL] {
        guard FileManager.default.fileExists(atPath: root.path) else { return [] }
        return FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)?
            .compactMap { $0 as? URL }
            .filter { $0.pathExtension == "jpg" } ?? []
    }
}

private struct Fixture {
    let router: ReceiverRouter
    let root: URL
    let temporaryDirectory: TemporaryDirectory
}

private final class ReceiverUUIDGenerator: @unchecked Sendable {
    private let lock = NSLock()
    private let ids: [UUID]
    private var index = 0

    init(_ ids: [UUID]) {
        self.ids = ids
    }

    func next() -> UUID {
        lock.lock()
        defer { lock.unlock() }
        let id = ids[min(index, ids.count - 1)]
        index += 1
        return id
    }
}
