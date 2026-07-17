import CoreGraphics
import ScreenCaptureKit
import XCTest
@testable import LanShotCore

final class ScreenCaptureSupportTests: XCTestCase {
    func testDisplayMatcherFindsMainDisplayID() {
        XCTAssertEqual(
            ScreenCaptureSupport.indexOfDisplay(
                matching: 42,
                displayIDs: [7, 42, 99]
            ),
            1
        )
        XCTAssertNil(
            ScreenCaptureSupport.indexOfDisplay(
                matching: 1,
                displayIDs: [7, 42, 99]
            )
        )
    }

    func testFrameAcceptanceRequiresCompleteFrameAndImageBuffer() {
        XCTAssertTrue(
            ScreenCaptureSupport.shouldAcceptFrame(status: .complete, hasImageBuffer: true)
        )
        XCTAssertFalse(
            ScreenCaptureSupport.shouldAcceptFrame(status: .idle, hasImageBuffer: true)
        )
        XCTAssertFalse(
            ScreenCaptureSupport.shouldAcceptFrame(status: .complete, hasImageBuffer: false)
        )
    }

    func testSessionStateMapsUnavailableReasons() {
        XCTAssertNil(MacSessionState.available.captureError)
        XCTAssertEqual(
            MacSessionState(isSessionActive: false, isScreenAwake: true, isSystemAwake: true).captureError,
            .sessionLocked
        )
        XCTAssertEqual(
            MacSessionState(isSessionActive: true, isScreenAwake: false, isSystemAwake: true).captureError,
            .displayAsleep
        )
        XCTAssertEqual(
            MacSessionState(isSessionActive: true, isScreenAwake: true, isSystemAwake: false).captureError,
            .displayAsleep
        )
    }

    func testJPEGEncoderProducesJPEGData() throws {
        let image = try XCTUnwrap(makeImage())
        let data = try ImageIOJPEGEncoder().encode(image, quality: 0.8)

        XCTAssertGreaterThan(data.count, 2)
        XCTAssertEqual(Array(data.prefix(2)), [0xFF, 0xD8])
    }

    func testJPEGEncoderRejectsQualityOutsideUnitInterval() throws {
        let image = try XCTUnwrap(makeImage())
        XCTAssertThrowsError(try ImageIOJPEGEncoder().encode(image, quality: -0.1))
        XCTAssertThrowsError(try ImageIOJPEGEncoder().encode(image, quality: 1.1))
    }

    private func makeImage() -> CGImage? {
        let pixels = Data([0x22, 0x66, 0xCC, 0xFF])
        guard let provider = CGDataProvider(data: pixels as CFData) else { return nil }
        return CGImage(
            width: 1,
            height: 1,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }
}
