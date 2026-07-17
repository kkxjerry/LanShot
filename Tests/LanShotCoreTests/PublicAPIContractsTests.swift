import Foundation
import XCTest
import LanShotCore

final class PublicAPIContractsTests: XCTestCase {
    func testAPIModelsCanBeConstructedByLibraryClients() {
        let error = APIError(code: "capture_failed", message: "Capture failed")
        let response = CaptureTaskResponse(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            status: .failed,
            expiresAt: Date(timeIntervalSince1970: 1_700_000_000),
            error: error
        )

        XCTAssertEqual(response.error, error)
    }

    func testAPIJSONReturnsFreshISO8601Encoder() throws {
        let modifiedEncoder = APIJSON.encoder
        modifiedEncoder.dateEncodingStrategy = .secondsSince1970

        let freshEncoder = APIJSON.encoder
        let data = try freshEncoder.encode(Date(timeIntervalSince1970: 1_700_000_000))
        let wireValue = try JSONDecoder().decode(String.self, from: data)

        XCTAssertFalse(modifiedEncoder === freshEncoder)
        XCTAssertEqual(wireValue, "2023-11-14T22:13:20Z")
    }

    func testAPIJSONReturnsFreshISO8601Decoder() throws {
        let modifiedDecoder = APIJSON.decoder
        modifiedDecoder.dateDecodingStrategy = .secondsSince1970

        let freshDecoder = APIJSON.decoder
        let data = Data("\"2023-11-14T22:13:20Z\"".utf8)

        XCTAssertFalse(modifiedDecoder === freshDecoder)
        XCTAssertEqual(
            try freshDecoder.decode(Date.self, from: data),
            Date(timeIntervalSince1970: 1_700_000_000)
        )
    }

    func testSystemWallClockReturnsImmediatelyForNonPositiveDurations() async throws {
        let clock = SystemWallClock()

        try await clock.sleep(for: 0)
        try await clock.sleep(for: -1)
    }

    func testSystemWallClockRejectsInvalidDurations() async {
        let clock = SystemWallClock()
        let overflowingSeconds = Double(UInt64.max) / 1_000_000_000 + 1
        let invalidDurations = [
            TimeInterval.nan,
            TimeInterval.infinity,
            -TimeInterval.infinity,
            overflowingSeconds
        ]

        for duration in invalidDurations {
            do {
                try await clock.sleep(for: duration)
                XCTFail("Expected invalid duration error for \(duration)")
            } catch {
                XCTAssertEqual(error as? ClockError, .invalidDuration)
            }
        }
    }
}
