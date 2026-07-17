import Foundation
import XCTest
@testable import LanShotCore

final class ImageStoreTests: XCTestCase {
    private let jpeg = Data([0xFF, 0xD8, 0xFF, 0xD9])

    func testStoreUsesReceiverCalendarForDatedPathAndFilename() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let captureID = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
        let now = ISO8601DateFormatter().date(from: "2026-07-17T00:30:00Z")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        let store = ImageStore(root: root, calendar: calendar)

        let storedURL = try await store.storeJPEG(jpeg, captureID: captureID, now: now)

        let expectedURL = root
            .appendingPathComponent("2026-07-16", isDirectory: true)
            .appendingPathComponent(
                "2026-07-16_17-30-00_00000000-0000-0000-0000-000000000001.jpg"
            )
        XCTAssertEqual(storedURL.standardizedFileURL, expectedURL.standardizedFileURL)
        XCTAssertEqual(try Data(contentsOf: storedURL), jpeg)
    }

    func testStoreRejectsDataWithoutJPEGStartBytesBeforeCreatingRoot() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let store = ImageStore(root: root)
        let invalidPayloads = [Data(), Data([0xFF]), Data([0x00, 0xD8, 0xFF, 0xD9])]

        for payload in invalidPayloads {
            do {
                _ = try await store.storeJPEG(payload, captureID: UUID(), now: Date())
                XCTFail("Expected invalid JPEG payload to be rejected")
            } catch {
                XCTAssertEqual(error as? ImageStoreError, .invalidJPEG)
            }
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testStoreAcceptsPayloadAtMaximumSize() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let store = ImageStore(root: root)
        var payload = Data(repeating: 0, count: ImageStore.maximumBytes)
        payload[0] = 0xFF
        payload[1] = 0xD8

        let storedURL = try await store.storeJPEG(payload, captureID: UUID(), now: Date())

        let attributes = try FileManager.default.attributesOfItem(atPath: storedURL.path)
        XCTAssertEqual(attributes[.size] as? Int, ImageStore.maximumBytes)
    }

    func testStoreRejectsPayloadLargerThanMaximumBeforeCreatingRoot() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let store = ImageStore(root: root)
        var payload = Data(repeating: 0, count: ImageStore.maximumBytes + 1)
        payload[0] = 0xFF
        payload[1] = 0xD8

        do {
            _ = try await store.storeJPEG(payload, captureID: UUID(), now: Date())
            XCTFail("Expected oversized JPEG payload to be rejected")
        } catch {
            XCTAssertEqual(error as? ImageStoreError, .tooLarge)
        }

        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testAtomicStoreLeavesOnlyCompletedJPEGInDateDirectory() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let now = ISO8601DateFormatter().date(from: "2026-07-17T09:30:00Z")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let writer = RecordingImageWriter()
        let store = ImageStore(root: root, calendar: calendar) { data, destination, options in
            try writer.write(data, to: destination, options: options)
        }

        let storedURL = try await store.storeJPEG(jpeg, captureID: UUID(), now: now)

        let call = try XCTUnwrap(writer.calls.first)
        XCTAssertEqual(writer.calls.count, 1)
        XCTAssertEqual(call.data, jpeg)
        XCTAssertEqual(call.destination, storedURL)
        XCTAssertTrue(call.options.contains(.atomic))
        let contents = try FileManager.default.contentsOfDirectory(
            at: storedURL.deletingLastPathComponent(),
            includingPropertiesForKeys: nil
        )
        XCTAssertEqual(contents.map(\.lastPathComponent), [storedURL.lastPathComponent])
    }

    func testStoreRejectsSymlinkedDateDirectoryWithoutWritingOutsideRoot() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let outside = temporaryDirectory.url.appendingPathComponent("Outside", isDirectory: true)
        let linkedDateDirectory = root.appendingPathComponent("2026-07-17", isDirectory: true)
        let fileManager = FileManager.default
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: outside, withIntermediateDirectories: true)
        try fileManager.createSymbolicLink(at: linkedDateDirectory, withDestinationURL: outside)
        let now = ISO8601DateFormatter().date(from: "2026-07-17T09:30:00Z")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let store = ImageStore(root: root, calendar: calendar)

        do {
            _ = try await store.storeJPEG(jpeg, captureID: UUID(), now: now)
            XCTFail("Expected a symlinked date directory to be rejected")
        } catch {
            XCTAssertEqual(error as? ImageStoreError, .unsafeStoragePath)
        }

        XCTAssertTrue(try fileManager.contentsOfDirectory(atPath: outside.path).isEmpty)
    }

    func testDuplicateCaptureIDReturnsOriginalFileInSameStoreAndAfterRebuild() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let captureID = UUID(uuidString: "5C7B6C87-6725-45ED-8BDB-83154733B244")!
        let firstDate = ISO8601DateFormatter().date(from: "2026-07-17T09:30:00Z")!
        let laterDate = firstDate.addingTimeInterval(86_400)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let originalStore = ImageStore(root: root, calendar: calendar)

        let originalURL = try await originalStore.storeJPEG(jpeg, captureID: captureID, now: firstDate)
        let sameRunURL = try await originalStore.storeJPEG(
            Data([0xFF, 0xD8, 0x01]),
            captureID: captureID,
            now: laterDate
        )
        let rebuiltStore = ImageStore(root: root, calendar: calendar)
        let rebuiltURL = try await rebuiltStore.storeJPEG(
            Data([0xFF, 0xD8, 0x02]),
            captureID: captureID,
            now: laterDate
        )

        XCTAssertEqual(sameRunURL, originalURL)
        XCTAssertEqual(rebuiltURL, originalURL)
        XCTAssertEqual(try Data(contentsOf: originalURL), jpeg)
        XCTAssertEqual(try jpegFiles(under: root).count, 1)
    }

    func testCleanupUsesStrictSevenDayBoundaryAndRemovesEmptyDateDirectories() async throws {
        let temporaryDirectory = try TemporaryDirectory()
        let root = temporaryDirectory.url.appendingPathComponent("Images", isDirectory: true)
        let retentionDirectory = root.appendingPathComponent("2026-07-10", isDirectory: true)
        let emptyDateDirectory = root.appendingPathComponent("2026-07-08", isDirectory: true)
        let unrelatedDirectory = root.appendingPathComponent("notes", isDirectory: true)
        let fileManager = FileManager.default
        for directory in [retentionDirectory, emptyDateDirectory, unrelatedDirectory] {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        let boundaryFile = retentionDirectory.appendingPathComponent("boundary.jpg")
        let expiredFile = retentionDirectory.appendingPathComponent("expired.jpg")
        try jpeg.write(to: boundaryFile)
        try jpeg.write(to: expiredFile)
        let now = ISO8601DateFormatter().date(from: "2026-07-17T12:00:00Z")!
        let cutoff = now.addingTimeInterval(-7 * 24 * 60 * 60)
        try fileManager.setAttributes([.modificationDate: cutoff], ofItemAtPath: boundaryFile.path)
        try fileManager.setAttributes(
            [.modificationDate: cutoff.addingTimeInterval(-1)],
            ofItemAtPath: expiredFile.path
        )
        let store = ImageStore(root: root)

        let removedCount = try await store.cleanup(now: now)

        XCTAssertEqual(removedCount, 1)
        XCTAssertTrue(fileManager.fileExists(atPath: boundaryFile.path))
        XCTAssertFalse(fileManager.fileExists(atPath: expiredFile.path))
        XCTAssertTrue(fileManager.fileExists(atPath: retentionDirectory.path))
        XCTAssertFalse(fileManager.fileExists(atPath: emptyDateDirectory.path))
        XCTAssertTrue(fileManager.fileExists(atPath: unrelatedDirectory.path))
    }

    private func jpegFiles(under root: URL) throws -> [URL] {
        guard let enumerator = FileManager.default.enumerator(
            at: root,
            includingPropertiesForKeys: [.isRegularFileKey]
        ) else {
            return []
        }

        return try enumerator.compactMap { element in
            guard let url = element as? URL else { return nil }
            let values = try url.resourceValues(forKeys: [.isRegularFileKey])
            return values.isRegularFile == true && url.pathExtension == "jpg" ? url : nil
        }
    }
}

private final class RecordingImageWriter: @unchecked Sendable {
    struct Call {
        let data: Data
        let destination: URL
        let options: Data.WritingOptions
    }

    private let lock = NSLock()
    private var storage: [Call] = []

    var calls: [Call] {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func write(_ data: Data, to destination: URL, options: Data.WritingOptions) throws {
        lock.lock()
        storage.append(Call(data: data, destination: destination, options: options))
        lock.unlock()
        try data.write(to: destination, options: options)
    }
}
