import Foundation
import XCTest
@testable import LanShotCore

final class ReceiverServerIntegrationTests: XCTestCase {
    func testLoopbackTaskClaimUploadCompletesWithOneStoredFile() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let id = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
        let tasks = CaptureTaskStore(uuid: { id })
        let images = ImageStore(root: root)
        let router = ReceiverRouter(tasks: tasks, images: images, clock: SystemWallClock())
        let server = ReceiverServer(router: router, host: "127.0.0.1", port: 0, advertiseBonjour: false)
        let port = try server.start()
        defer { server.stop() }
        let base = URL(string: "http://127.0.0.1:\(port)")!

        let created = try await request(base.appendingPathComponent("api/v1/tasks"), method: "POST")
        XCTAssertEqual(created.status, 201)
        let claimed = try await request(
            URL(string: "\(base.absoluteString)/api/v1/agent/next?timeout=1")!,
            method: "GET"
        )
        XCTAssertEqual(claimed.status, 200)
        let uploaded = try await request(
            base.appendingPathComponent("api/v1/tasks/\(id.uuidString)/image"),
            method: "POST",
            contentType: "image/jpeg",
            body: Data([0xFF, 0xD8, 0xFF, 0xD9])
        )
        XCTAssertEqual(uploaded.status, 201)

        let task = try await request(base.appendingPathComponent("api/v1/tasks/\(id.uuidString)"), method: "GET")
        XCTAssertEqual(try APIJSON.decoder.decode(CaptureTaskResponse.self, from: task.data).status, .completed)
        let files = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)?
            .compactMap { $0 as? URL }.filter { $0.pathExtension == "jpg" } ?? []
        XCTAssertEqual(files.count, 1)
    }

    private func request(
        _ url: URL,
        method: String,
        contentType: String? = nil,
        body: Data? = nil
    ) async throws -> (data: Data, status: Int) {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        return (data, try XCTUnwrap((response as? HTTPURLResponse)?.statusCode))
    }
}
