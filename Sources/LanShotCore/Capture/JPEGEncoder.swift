import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

public struct ImageIOJPEGEncoder: JPEGEncoding {
    public init() {}

    public func encode(_ image: CGImage, quality: Double) throws -> Data {
        guard quality.isFinite, (0...1).contains(quality) else {
            throw CaptureError.encodingFailed("JPEG quality must be between 0 and 1.")
        }

        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            output,
            UTType.jpeg.identifier as CFString,
            1,
            nil
        ) else {
            throw CaptureError.encodingFailed("Could not create the JPEG encoder.")
        }

        let properties = [
            kCGImageDestinationLossyCompressionQuality: quality
        ] as CFDictionary
        CGImageDestinationAddImage(destination, image, properties)
        guard CGImageDestinationFinalize(destination) else {
            throw CaptureError.encodingFailed("Could not finish JPEG encoding.")
        }
        return output as Data
    }
}
