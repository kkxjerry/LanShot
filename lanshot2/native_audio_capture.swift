import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit
import Security
import Speech

func loadBailianAPIKey() throws -> String {
    if let value = ProcessInfo.processInfo.environment["DASHSCOPE_API_KEY"]?.trimmingCharacters(
        in: .whitespacesAndNewlines
    ), !value.isEmpty {
        return value
    }
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "com.lanshot.bailian",
        kSecAttrAccount as String: "DASHSCOPE_API_KEY",
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    guard status == errSecSuccess,
          let data = item as? Data,
          let value = String(data: data, encoding: .utf8),
          !value.isEmpty else {
        throw NSError(domain: "LanShotAudio", code: 11, userInfo: [
            NSLocalizedDescriptionKey: "Bailian API key is not configured"
        ])
    }
    return value
}

final class QwenRealtimeTranscriber: @unchecked Sendable {
    private let outputURL: URL
    private let errorURL: URL
    private let apiKey: String
    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: false
    )!
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?
    private var audioContinuation: AsyncStream<Data>.Continuation?
    private var completedSegments: [String] = []
    private var currentText = ""

    init(outputURL: URL, apiKey: String) {
        self.outputURL = outputURL
        self.errorURL = outputURL.deletingPathExtension().appendingPathExtension("errors.log")
        self.apiKey = apiKey
        try? FileManager.default.removeItem(at: outputURL)
        try? FileManager.default.removeItem(at: errorURL)
        try? "".write(to: outputURL, atomically: true, encoding: .utf8)
    }

    func start() async throws {
        let endpoint = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime"
        var request = URLRequest(url: URL(string: endpoint)!)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("realtime=v1", forHTTPHeaderField: "OpenAI-Beta")
        let socket = URLSession.shared.webSocketTask(with: request)
        self.socket = socket
        socket.resume()

        let update: [String: Any] = [
            "event_id": "event_\(UUID().uuidString)",
            "type": "session.update",
            "session": [
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": 16_000,
                "input_audio_transcription": ["language": "zh"],
                "turn_detection": [
                    "type": "server_vad",
                    "threshold": 0.0,
                    "silence_duration_ms": 500,
                ],
            ],
        ]
        let updateData = try JSONSerialization.data(withJSONObject: update)
        try await socket.send(.string(String(decoding: updateData, as: UTF8.self)))

        let (stream, continuation) = AsyncStream<Data>.makeStream(
            bufferingPolicy: .bufferingNewest(200)
        )
        audioContinuation = continuation
        sendTask = Task { [weak self, weak socket] in
            guard let self, let socket else { return }
            for await data in stream {
                let event: [String: Any] = [
                    "event_id": "event_\(UUID().uuidString)",
                    "type": "input_audio_buffer.append",
                    "audio": data.base64EncodedString(),
                ]
                do {
                    let json = try JSONSerialization.data(withJSONObject: event)
                    try await socket.send(.string(String(decoding: json, as: UTF8.self)))
                } catch {
                    self.logError("send failed: \(error.localizedDescription)")
                    break
                }
            }
        }
        receiveTask = Task { [weak self, weak socket] in
            guard let self, let socket else { return }
            do {
                while !Task.isCancelled {
                    let message = try await socket.receive()
                    switch message {
                    case .string(let text):
                        self.handleServerMessage(Data(text.utf8))
                    case .data(let data):
                        self.handleServerMessage(data)
                    @unknown default:
                        break
                    }
                }
            } catch {
                if !Task.isCancelled {
                    self.logError("receive failed: \(error.localizedDescription)")
                }
            }
        }
    }

    func append(_ inputBuffer: AVAudioPCMBuffer) {
        if converter == nil || converterInputFormat != inputBuffer.format {
            converter = AVAudioConverter(from: inputBuffer.format, to: targetFormat)
            converterInputFormat = inputBuffer.format
        }
        guard let converter else { return }
        let ratio = targetFormat.sampleRate / inputBuffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(inputBuffer.frameLength) * ratio) + 64
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: capacity
        ) else { return }
        var suppliedInput = false
        var conversionError: NSError?
        converter.convert(to: outputBuffer, error: &conversionError) { _, status in
            if suppliedInput {
                status.pointee = .noDataNow
                return nil
            }
            suppliedInput = true
            status.pointee = .haveData
            return inputBuffer
        }
        guard conversionError == nil,
              outputBuffer.frameLength > 0,
              let samples = outputBuffer.int16ChannelData?[0] else {
            return
        }
        audioContinuation?.yield(
            Data(bytes: samples, count: Int(outputBuffer.frameLength) * MemoryLayout<Int16>.size)
        )
    }

    func finish() async {
        audioContinuation?.finish()
        _ = await sendTask?.result
        if let socket {
            let event: [String: Any] = [
                "event_id": "event_\(UUID().uuidString)",
                "type": "session.finish",
            ]
            if let data = try? JSONSerialization.data(withJSONObject: event) {
                try? await socket.send(.string(String(decoding: data, as: UTF8.self)))
            }
            try? await Task.sleep(for: .milliseconds(800))
            socket.cancel(with: .normalClosure, reason: nil)
        }
        receiveTask?.cancel()
        writeTranscript()
    }

    private func handleServerMessage(_ data: Data) {
        guard let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = message["type"] as? String else {
            return
        }
        switch type {
        case "conversation.item.input_audio_transcription.delta",
             "conversation.item.input_audio_transcription.text":
            let text = message["text"] as? String ?? ""
            let stash = message["stash"] as? String ?? ""
            currentText = text + stash
            writeTranscript()
        case "conversation.item.input_audio_transcription.completed":
            let transcript = (message["transcript"] as? String)
                ?? (message["text"] as? String)
                ?? currentText
            if !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                completedSegments.append(transcript)
            }
            currentText = ""
            writeTranscript()
        case "error", "conversation.item.input_audio_transcription.failed":
            if let error = message["error"] as? [String: Any] {
                logError(error["message"] as? String ?? String(describing: error))
            } else {
                logError(String(decoding: data, as: UTF8.self))
            }
        default:
            break
        }
    }

    private func writeTranscript() {
        var parts = completedSegments
        if !currentText.isEmpty {
            parts.append(currentText)
        }
        try? parts.joined(separator: "\n").write(
            to: outputURL,
            atomically: true,
            encoding: .utf8
        )
    }

    private func logError(_ message: String) {
        let line = "\(Date()): \(message)\n"
        if let handle = try? FileHandle(forWritingTo: errorURL),
           let data = line.data(using: .utf8) {
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
            try? handle.close()
        } else {
            try? line.write(to: errorURL, atomically: true, encoding: .utf8)
        }
    }
}

final class LocalTranscriber: @unchecked Sendable {
    private let label: String
    private let outputURL: URL
    private let errorURL: URL
    private let recognizer: SFSpeechRecognizer
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var completedSegments: [String] = []
    private var currentText = ""
    private var stopped = false

    init(label: String, outputURL: URL, locale: Locale) throws {
        guard let recognizer = SFSpeechRecognizer(locale: locale) else {
            throw NSError(domain: "LanShotAudio", code: 7, userInfo: [
                NSLocalizedDescriptionKey: "speech locale is unavailable: \(locale.identifier)"
            ])
        }
        guard recognizer.supportsOnDeviceRecognition else {
            throw NSError(domain: "LanShotAudio", code: 8, userInfo: [
                NSLocalizedDescriptionKey: "on-device speech recognition is unavailable for \(locale.identifier)"
            ])
        }
        self.label = label
        self.outputURL = outputURL
        self.errorURL = outputURL.deletingPathExtension().appendingPathExtension("errors.log")
        self.recognizer = recognizer
        try? FileManager.default.removeItem(at: outputURL)
        try? FileManager.default.removeItem(at: errorURL)
    }

    private func startTaskLocked() {
        guard !stopped else { return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        self.request = request
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            self?.handle(result: result, error: error)
        }
    }

    private func handle(result: SFSpeechRecognitionResult?, error: Error?) {
        lock.lock()
        defer { lock.unlock() }
        if let result {
            currentText = result.bestTranscription.formattedString
            writeTranscriptLocked()
            print("[\(label)] \(currentText)")
            fflush(stdout)
        }
        if result?.isFinal == true || error != nil {
            if !currentText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                completedSegments.append(currentText)
            }
            currentText = ""
            request = nil
            task = nil
            writeTranscriptLocked()
            if let error, !stopped {
                let nsError = error as NSError
                let message = "\(Date()): \(nsError.domain) \(nsError.code): \(nsError.localizedDescription)\n"
                if let data = message.data(using: .utf8),
                   let handle = try? FileHandle(forWritingTo: errorURL) {
                    _ = try? handle.seekToEnd()
                    try? handle.write(contentsOf: data)
                    try? handle.close()
                } else {
                    try? message.write(to: errorURL, atomically: true, encoding: .utf8)
                }
            }
        }
    }

    func append(_ sampleBuffer: CMSampleBuffer) {
        lock.lock()
        if request == nil {
            startTaskLocked()
        }
        request?.appendAudioSampleBuffer(sampleBuffer)
        lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        if request == nil {
            startTaskLocked()
        }
        request?.append(buffer)
        lock.unlock()
    }

    func finish() {
        lock.lock()
        stopped = true
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        if !currentText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            completedSegments.append(currentText)
            currentText = ""
        }
        writeTranscriptLocked()
        lock.unlock()
    }

    private func writeTranscriptLocked() {
        var parts = completedSegments
        if !currentText.isEmpty {
            parts.append(currentText)
        }
        let text = parts.joined(separator: "\n")
        do {
            try text.write(to: outputURL, atomically: true, encoding: .utf8)
        } catch {
            fputs("transcript write error: \(error.localizedDescription)\n", stderr)
        }
    }
}

func transcribeAudioFile(_ audioURL: URL, to outputURL: URL, locale: Locale) async throws {
    guard let recognizer = SFSpeechRecognizer(locale: locale),
          recognizer.supportsOnDeviceRecognition else {
        throw NSError(domain: "LanShotAudio", code: 10, userInfo: [
            NSLocalizedDescriptionKey: "on-device file transcription is unavailable"
        ])
    }
    let request = SFSpeechURLRecognitionRequest(url: audioURL)
    request.requiresOnDeviceRecognition = true
    request.addsPunctuation = true
    request.taskHint = .dictation
    let transcript: String = try await withCheckedThrowingContinuation { continuation in
        let callbackLock = NSLock()
        var completed = false
        _ = recognizer.recognitionTask(with: request) { result, error in
            callbackLock.lock()
            defer { callbackLock.unlock() }
            guard !completed else { return }
            if let error {
                completed = true
                continuation.resume(throwing: error)
            } else if let result, result.isFinal {
                completed = true
                continuation.resume(returning: result.bestTranscription.formattedString)
            }
        }
    }
    try transcript.write(to: outputURL, atomically: true, encoding: .utf8)
}

final class SystemAudioWriter {
    private let outputURL: URL
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?

    init(outputURL: URL) {
        self.outputURL = outputURL
        try? FileManager.default.removeItem(at: outputURL)
    }

    func append(_ sampleBuffer: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        if writer == nil {
            do {
                try prepare(using: sampleBuffer)
            } catch {
                fputs("system audio writer error: \(error)\n", stderr)
                return
            }
        }
        guard let writer, let input, writer.status == .writing, input.isReadyForMoreMediaData else {
            return
        }
        if !input.append(sampleBuffer) {
            fputs("system audio append error: \(writer.error?.localizedDescription ?? "unknown")\n", stderr)
        }
    }

    private func prepare(using sampleBuffer: CMSampleBuffer) throws {
        guard let format = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(format) else {
            throw NSError(domain: "LanShotAudio", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "missing system audio format"
            ])
        }
        let description = streamDescription.pointee
        let channels = max(1, min(2, Int(description.mChannelsPerFrame)))
        let sampleRate = max(8_000, description.mSampleRate)
        let writer = try AVAssetWriter(outputURL: outputURL, fileType: .m4a)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: channels,
            AVEncoderBitRateKey: 128_000,
        ]
        let input = AVAssetWriterInput(
            mediaType: .audio,
            outputSettings: settings,
            sourceFormatHint: format
        )
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else {
            throw NSError(domain: "LanShotAudio", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "cannot add system audio track"
            ])
        }
        writer.add(input)
        guard writer.startWriting() else {
            throw writer.error ?? NSError(domain: "LanShotAudio", code: 3)
        }
        writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        self.writer = writer
        self.input = input
    }

    func finish() async {
        guard let writer, let input, writer.status == .writing else { return }
        input.markAsFinished()
        await withCheckedContinuation { continuation in
            writer.finishWriting {
                continuation.resume()
            }
        }
    }
}

final class CaptureOutput: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let systemWriter: SystemAudioWriter
    let transcriber: QwenRealtimeTranscriber

    init(systemWriter: SystemAudioWriter, transcriber: QwenRealtimeTranscriber) {
        self.systemWriter = systemWriter
        self.transcriber = transcriber
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        if outputType == .audio {
            systemWriter.append(sampleBuffer)
            guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
                return
            }
            let format = AVAudioFormat(cmAudioFormatDescription: formatDescription)
            let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
            guard frameCount > 0,
                  let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else {
                return
            }
            buffer.frameLength = frameCount
            let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
                sampleBuffer,
                at: 0,
                frameCount: Int32(frameCount),
                into: buffer.mutableAudioBufferList
            )
            if status == noErr {
                transcriber.append(buffer)
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("system audio capture stopped: \(error.localizedDescription)\n", stderr)
    }
}

final class MicrophoneRecorder: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let outputURL: URL
    private let realtimeTranscriber: QwenRealtimeTranscriber
    private var file: AVAudioFile?

    init(outputURL: URL, realtimeTranscriber: QwenRealtimeTranscriber) {
        self.outputURL = outputURL
        self.realtimeTranscriber = realtimeTranscriber
        try? FileManager.default.removeItem(at: outputURL)
    }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.channelCount > 0 else {
            throw NSError(domain: "LanShotAudio", code: 4, userInfo: [
                NSLocalizedDescriptionKey: "no microphone input"
            ])
        }
        let file = try AVAudioFile(forWriting: outputURL, settings: format.settings)
        self.file = file
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
            do {
                try self?.file?.write(from: buffer)
                self?.realtimeTranscriber.append(buffer)
            } catch {
                fputs("microphone write error: \(error.localizedDescription)\n", stderr)
            }
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        file = nil
    }
}

func waitForStopSignal() async {
    await withCheckedContinuation { continuation in
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        var finished = false
        let stop = {
            guard !finished else { return }
            finished = true
            interrupt.cancel()
            terminate.cancel()
            continuation.resume()
        }
        interrupt.setEventHandler(handler: stop)
        terminate.setEventHandler(handler: stop)
        interrupt.resume()
        terminate.resume()
    }
}

func requestSpeechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
    await withCheckedContinuation { continuation in
        SFSpeechRecognizer.requestAuthorization { status in
            continuation.resume(returning: status)
        }
    }
}

@main
struct LanShotAudioCapture {
    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            fputs("usage: native_audio_capture OUTPUT_DIRECTORY\n", stderr)
            exit(2)
        }
        let outputDirectory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let statusURL = outputDirectory.appendingPathComponent("capture.log")
        let pidURL = outputDirectory.appendingPathComponent("capture.pid")
        do {
            try FileManager.default.createDirectory(
                at: outputDirectory,
                withIntermediateDirectories: true
            )
            try "\(ProcessInfo.processInfo.processIdentifier)\n".write(
                to: pidURL,
                atomically: true,
                encoding: .utf8
            )
            defer { try? FileManager.default.removeItem(at: pidURL) }
            try "starting\n".write(to: statusURL, atomically: true, encoding: .utf8)
            for name in ["me.txt", "me.errors.log"] {
                try? FileManager.default.removeItem(
                    at: outputDirectory.appendingPathComponent(name)
                )
            }
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw NSError(domain: "LanShotAudio", code: 5, userInfo: [
                    NSLocalizedDescriptionKey: "microphone permission denied"
                ])
            }
            guard await requestSpeechAuthorization() == .authorized else {
                throw NSError(domain: "LanShotAudio", code: 9, userInfo: [
                    NSLocalizedDescriptionKey: "speech recognition permission denied"
                ])
            }

            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: true
            )
            guard let display = content.displays.first else {
                throw NSError(domain: "LanShotAudio", code: 6, userInfo: [
                    NSLocalizedDescriptionKey: "no display available for system audio capture"
                ])
            }

            let apiKey = try loadBailianAPIKey()
            let systemWriter = SystemAudioWriter(
                outputURL: outputDirectory.appendingPathComponent("interviewer.m4a")
            )
            let interviewerTranscriber = QwenRealtimeTranscriber(
                outputURL: outputDirectory.appendingPathComponent("interviewer.txt"),
                apiKey: apiKey
            )
            let microphoneTranscriber = QwenRealtimeTranscriber(
                outputURL: outputDirectory.appendingPathComponent("me.txt"),
                apiKey: apiKey
            )
            try await interviewerTranscriber.start()
            try await microphoneTranscriber.start()
            let microphone = MicrophoneRecorder(
                outputURL: outputDirectory.appendingPathComponent("me.wav"),
                realtimeTranscriber: microphoneTranscriber
            )
            let captureOutput = CaptureOutput(
                systemWriter: systemWriter,
                transcriber: interviewerTranscriber
            )
            let configuration = SCStreamConfiguration()
            configuration.width = 2
            configuration.height = 2
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            configuration.queueDepth = 1
            configuration.showsCursor = false
            configuration.capturesAudio = true
            configuration.excludesCurrentProcessAudio = true
            configuration.sampleRate = 48_000
            configuration.channelCount = 2

            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let stream = SCStream(filter: filter, configuration: configuration, delegate: captureOutput)
            let audioQueue = DispatchQueue(label: "lanshot.system-audio")
            try stream.addStreamOutput(captureOutput, type: .audio, sampleHandlerQueue: audioQueue)

            try microphone.start()
            try await stream.startCapture()
            try "running\n".write(to: statusURL, atomically: true, encoding: .utf8)
            print("audio capture started")
            print("interviewer: \(outputDirectory.appendingPathComponent("interviewer.m4a").path)")
            print("me: \(outputDirectory.appendingPathComponent("me.wav").path)")
            print("interviewer transcript: \(outputDirectory.appendingPathComponent("interviewer.txt").path)")
            print("my transcript: \(outputDirectory.appendingPathComponent("me.txt").path)")
            fflush(stdout)

            await waitForStopSignal()
            try await stream.stopCapture()
            microphone.stop()
            async let interviewerFinish: Void = interviewerTranscriber.finish()
            async let microphoneFinish: Void = microphoneTranscriber.finish()
            _ = await (interviewerFinish, microphoneFinish)
            await systemWriter.finish()
            try "stopped\n".write(to: statusURL, atomically: true, encoding: .utf8)
            print("audio capture stopped")
        } catch {
            try? "failed: \(error.localizedDescription)\n".write(
                to: statusURL,
                atomically: true,
                encoding: .utf8
            )
            fputs("audio capture failed: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
