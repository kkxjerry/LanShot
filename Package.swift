// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "LanShot",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "LanShotCore", targets: ["LanShotCore"]),
        .executable(name: "LanShot", targets: ["LanShot"])
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", exact: "2.101.3"),
        .package(url: "https://github.com/apple/swift-nio-transport-services.git", exact: "1.28.0")
    ],
    targets: [
        .target(name: "LanShotCore", dependencies: [
            .product(name: "NIOCore", package: "swift-nio"),
            .product(name: "NIOHTTP1", package: "swift-nio"),
            .product(name: "NIOFoundationCompat", package: "swift-nio"),
            .product(name: "NIOTransportServices", package: "swift-nio-transport-services")
        ]),
        .executableTarget(name: "LanShot", dependencies: ["LanShotCore"]),
        .testTarget(name: "LanShotCoreTests", dependencies: [
            "LanShotCore",
            .product(name: "NIOEmbedded", package: "swift-nio")
        ])
    ],
    swiftLanguageModes: [.v5]
)
