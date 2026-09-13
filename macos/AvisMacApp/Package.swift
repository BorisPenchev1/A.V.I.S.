// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AvisMacApp",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(
            name: "AvisMacApp",
            targets: ["AvisMacApp"]
        )
    ],
    targets: [
        .executableTarget(
            name: "AvisMacApp",
            path: ".",
            exclude: ["Package.swift"]
        )
    ]
)
