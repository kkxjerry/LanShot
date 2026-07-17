import XCTest
@testable import LanShotCore

final class SharedContractsTests: XCTestCase {
    func testRolesRoundTripThroughRawValues() {
        XCTAssertEqual(AppRole(rawValue: "capture"), .capture)
        XCTAssertEqual(AppRole(rawValue: "receiver"), .receiver)
    }

    func testTaskResponseUsesStableWireValues() throws {
        let value = CaptureTaskResponse(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            status: .pending,
            expiresAt: Date(timeIntervalSince1970: 1_700_000_000),
            error: nil
        )
        let data = try APIJSON.encoder.encode(value)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["status"] as? String, "pending")
        XCTAssertEqual(json["id"] as? String, "00000000-0000-0000-0000-000000000001")
    }

    func testFailureCodesMatchHTTPContract() {
        XCTAssertEqual(Set(CaptureFailureCode.allCases.map(\.rawValue)), [
            "permission_required", "session_locked", "display_asleep",
            "capture_failed", "encoding_failed"
        ])
    }
}
