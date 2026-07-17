import Foundation
import NIOCore
import NIOEmbedded
import NIOHTTP1
import XCTest
@testable import LanShotCore

final class ReceiverHTTPHandlerTests: XCTestCase {
    func testHandlerCollectsRequestPartsAndWritesCompleteFramedResponse() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let router = ReceiverRouter(
            tasks: CaptureTaskStore(),
            images: ImageStore(root: temporaryDirectory.url),
            clock: SystemWallClock()
        )
        let channel = EmbeddedChannel(handler: ReceiverHTTPHandler(router: router))
        var head = HTTPRequestHead(version: .http1_1, method: .GET, uri: "/")
        head.headers.add(name: "host", value: "localhost")

        XCTAssertNoThrow(try channel.writeInbound(HTTPServerRequestPart.head(head)))
        XCTAssertNoThrow(try channel.writeInbound(HTTPServerRequestPart.end(nil)))
        let parts = try await waitForResponse(channel)

        guard case .head(let responseHead) = parts[0] else { return XCTFail("Expected head") }
        guard case .body(.byteBuffer(let body)) = parts[1] else { return XCTFail("Expected body") }
        guard case .end = parts[2] else { return XCTFail("Expected end") }
        XCTAssertEqual(responseHead.status, .ok)
        XCTAssertEqual(responseHead.headers.first(name: "content-length"), "\(body.readableBytes)")
        _ = try channel.finish()
    }

    func testHandlerRejectsDeclaredOrAccumulatedBodiesOverLimitImmediately() throws {
        let temporaryDirectory = try TemporaryDirectory()
        let router = ReceiverRouter(
            tasks: CaptureTaskStore(),
            images: ImageStore(root: temporaryDirectory.url),
            clock: SystemWallClock()
        )
        let channel = EmbeddedChannel(handler: ReceiverHTTPHandler(router: router))
        var head = HTTPRequestHead(version: .http1_1, method: .POST, uri: "/api/v1/images")
        head.headers.add(name: "content-length", value: "\(ImageStore.maximumBytes + 1)")

        XCTAssertNoThrow(try channel.writeInbound(HTTPServerRequestPart.head(head)))

        guard case .head(let responseHead)? = try channel.readOutbound(as: HTTPServerResponsePart.self) else {
            return XCTFail("Expected immediate response head")
        }
        XCTAssertEqual(responseHead.status, .payloadTooLarge)
        _ = try channel.readOutbound(as: HTTPServerResponsePart.self)
        _ = try channel.readOutbound(as: HTTPServerResponsePart.self)
        _ = try channel.finish()
    }

    private func waitForResponse(_ channel: EmbeddedChannel) async throws -> [HTTPServerResponsePart] {
        for _ in 0..<100 {
            channel.embeddedEventLoop.run()
            if let head = try channel.readOutbound(as: HTTPServerResponsePart.self) {
                var parts = [head]
                while let part = try channel.readOutbound(as: HTTPServerResponsePart.self) {
                    parts.append(part)
                }
                if parts.contains(where: { if case .end = $0 { return true }; return false }) {
                    return parts
                }
            }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        throw HandlerTestError.timedOut
    }
}

private enum HandlerTestError: Error {
    case timedOut
}
