import Foundation
import Darwin

public enum ImageStoreError: Error, Equatable, Sendable {
    case invalidJPEG
    case tooLarge
    case unsafeStoragePath
}

public actor ImageStore {
    public static let maximumBytes = 25 * 1_024 * 1_024

    private static let retentionInterval: TimeInterval = 7 * 24 * 60 * 60

    private let root: URL
    private let calendar: Calendar
    private let fileManager: FileManager
    private let writer: @Sendable (Data, URL, Data.WritingOptions) throws -> Void
    private var storedURLs: [UUID: URL] = [:]

    public init(
        root: URL,
        calendar: Calendar = .current,
        fileManager: FileManager = .default
    ) {
        self.root = root
        self.calendar = calendar
        self.fileManager = fileManager
        writer = { data, destination, options in
            try data.write(to: destination, options: options)
        }
    }

    init(
        root: URL,
        calendar: Calendar = .current,
        fileManager: FileManager = .default,
        writer: @escaping @Sendable (Data, URL, Data.WritingOptions) throws -> Void
    ) {
        self.root = root
        self.calendar = calendar
        self.fileManager = fileManager
        self.writer = writer
    }

    public func storeJPEG(_ data: Data, captureID: UUID, now: Date) throws -> URL {
        guard data.count <= Self.maximumBytes else {
            throw ImageStoreError.tooLarge
        }
        guard data.count >= 2, data[data.startIndex] == 0xFF,
              data[data.index(after: data.startIndex)] == 0xD8 else {
            throw ImageStoreError.invalidJPEG
        }

        if let storedURL = storedURLs[captureID],
           fileManager.fileExists(atPath: storedURL.path) {
            return storedURL
        }
        storedURLs.removeValue(forKey: captureID)

        if let existingURL = try existingURL(for: captureID) {
            storedURLs[captureID] = existingURL
            return existingURL
        }

        let date = formatted(now, as: "yyyy-MM-dd")
        let time = formatted(now, as: "yyyy-MM-dd_HH-mm-ss")
        let directory = root.appendingPathComponent(date, isDirectory: true)
        try prepareStorageDirectory(directory)
        let destination = directory.appendingPathComponent(
            "\(time)_\(captureID.uuidString).jpg",
            isDirectory: false
        )
        try writer(data, destination, .atomic)
        storedURLs[captureID] = destination
        return destination
    }

    @discardableResult
    public func cleanup(now: Date) throws -> Int {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: root.path, isDirectory: &isDirectory) else {
            return 0
        }
        guard isDirectory.boolValue else {
            return 0
        }

        let cutoff = now.addingTimeInterval(-Self.retentionInterval)
        let directories = try fileManager.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        )
        var removedCount = 0

        for directory in directories.sorted(by: { $0.path < $1.path }) {
            guard isDateDirectoryName(directory.lastPathComponent) else {
                continue
            }
            let physicalDirectory = root.appendingPathComponent(
                directory.lastPathComponent,
                isDirectory: true
            )
            try validatePhysicalDirectory(physicalDirectory)

            let files = try fileManager.contentsOfDirectory(
                at: physicalDirectory,
                includingPropertiesForKeys: [.isRegularFileKey, .contentModificationDateKey]
            )
            for file in files where file.pathExtension.lowercased() == "jpg" {
                let fileValues = try file.resourceValues(
                    forKeys: [.isRegularFileKey, .contentModificationDateKey]
                )
                guard fileValues.isRegularFile == true,
                      let modificationDate = fileValues.contentModificationDate,
                      modificationDate < cutoff else {
                    continue
                }

                try fileManager.removeItem(at: file)
                removedCount += 1
            }

            if try fileManager.contentsOfDirectory(atPath: physicalDirectory.path).isEmpty {
                try fileManager.removeItem(at: physicalDirectory)
            }
        }

        for captureID in storedURLs.keys.filter({ captureID in
            guard let url = storedURLs[captureID] else { return false }
            return !fileManager.fileExists(atPath: url.path)
        }) {
            storedURLs.removeValue(forKey: captureID)
        }

        return removedCount
    }

    private func existingURL(for captureID: UUID) throws -> URL? {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: root.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            return nil
        }

        let suffix = "_\(captureID.uuidString).jpg"
        let directories = try fileManager.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        )
        for directory in directories.sorted(by: { $0.path < $1.path }) {
            guard isDateDirectoryName(directory.lastPathComponent) else {
                continue
            }
            let physicalDirectory = root.appendingPathComponent(
                directory.lastPathComponent,
                isDirectory: true
            )
            try validatePhysicalDirectory(physicalDirectory)

            let files = try fileManager.contentsOfDirectory(
                at: physicalDirectory,
                includingPropertiesForKeys: [.isRegularFileKey],
                options: [.skipsHiddenFiles]
            )
            for file in files.sorted(by: { $0.path < $1.path })
            where file.lastPathComponent.hasSuffix(suffix) {
                let fileValues = try file.resourceValues(forKeys: [.isRegularFileKey])
                if fileValues.isRegularFile == true {
                    return root
                        .appendingPathComponent(physicalDirectory.lastPathComponent, isDirectory: true)
                        .appendingPathComponent(file.lastPathComponent, isDirectory: false)
                }
            }
        }

        return nil
    }

    private func prepareStorageDirectory(_ directory: URL) throws {
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            var status = stat()
            if lstat(directory.path, &status) == 0 {
                throw ImageStoreError.unsafeStoragePath
            }
            throw error
        }
        try validatePhysicalDirectory(directory)
    }

    private func validatePhysicalDirectory(_ directory: URL) throws {
        var status = stat()
        guard lstat(directory.path, &status) == 0 else {
            let errorCode = errno
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errorCode))
        }
        guard status.st_mode & S_IFMT == S_IFDIR else {
            throw ImageStoreError.unsafeStoragePath
        }
    }

    private func formatted(_ date: Date, as format: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = format
        return formatter.string(from: date)
    }

    private func isDateDirectoryName(_ name: String) -> Bool {
        let parts = name.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3,
              parts[0].count == 4,
              parts[1].count == 2,
              parts[2].count == 2 else {
            return false
        }
        return parts.allSatisfy { part in
            part.allSatisfy(\.isNumber)
        }
    }
}
