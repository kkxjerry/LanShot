import AppKit
import Darwin
import UniformTypeIdentifiers

@MainActor
final class OverlayPreferences {
    private enum Key {
        static let width = "overlay.width"
        static let height = "overlay.height"
        static let voiceWidth = "overlay.voice.width"
        static let voiceHeight = "overlay.voice.height"
        static let fontSize = "overlay.fontSize"
        static let textRed = "overlay.text.red"
        static let textGreen = "overlay.text.green"
        static let textBlue = "overlay.text.blue"
        static let textOpacity = "overlay.text.opacity"
    }

    private let defaults: UserDefaults
    private let isVoiceMode: Bool
    let widthRange: ClosedRange<Double>
    let heightRange: ClosedRange<Double>
    var onChange: (() -> Void)?

    init(defaults: UserDefaults = .standard, isVoiceMode: Bool = false) {
        self.defaults = defaults
        self.isVoiceMode = isVoiceMode
        let screens = NSScreen.screens.map(\.visibleFrame)
        let primaryFrame = NSScreen.main?.visibleFrame
            ?? screens.first
            ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let maximumWidth = max(320, (screens.map(\.width).max() ?? primaryFrame.width) - 20)
        let maximumHeight = max(240, (screens.map(\.height).max() ?? primaryFrame.height) - 20)
        widthRange = (isVoiceMode ? 320 : 160)...Double(maximumWidth)
        heightRange = (isVoiceMode ? 240 : 160)...Double(maximumHeight)
        let voiceDefaultWidth = min(
            maximumWidth,
            max(760, primaryFrame.width * 0.82)
        )
        let voiceDefaultHeight = min(
            maximumHeight,
            max(520, primaryFrame.height * 0.78)
        )
        defaults.register(defaults: [
            Key.width: 256.0,
            Key.height: 256.0,
            Key.voiceWidth: Double(voiceDefaultWidth),
            Key.voiceHeight: Double(voiceDefaultHeight),
            Key.fontSize: 17.0,
            Key.textRed: 0.0,
            Key.textGreen: 0.0,
            Key.textBlue: 0.0,
            Key.textOpacity: 1.0,
        ])
    }

    private var widthKey: String { isVoiceMode ? Key.voiceWidth : Key.width }
    private var heightKey: String { isVoiceMode ? Key.voiceHeight : Key.height }

    var width: CGFloat {
        CGFloat(defaults.double(forKey: widthKey).clamped(to: widthRange))
    }

    var height: CGFloat {
        CGFloat(defaults.double(forKey: heightKey).clamped(to: heightRange))
    }
    var fontSize: CGFloat { CGFloat(defaults.double(forKey: Key.fontSize)) }
    var textOpacity: CGFloat { CGFloat(defaults.double(forKey: Key.textOpacity)) }

    var textColor: NSColor {
        NSColor(
            srgbRed: defaults.double(forKey: Key.textRed),
            green: defaults.double(forKey: Key.textGreen),
            blue: defaults.double(forKey: Key.textBlue),
            alpha: 1
        )
    }

    func setWidth(_ value: Double) {
        set(value.clamped(to: widthRange), forKey: widthKey)
    }

    func setHeight(_ value: Double) {
        set(value.clamped(to: heightRange), forKey: heightKey)
    }

    func setFontSize(_ value: Double) {
        set(value.clamped(to: 10...48), forKey: Key.fontSize)
    }

    func setTextOpacity(_ value: Double) {
        set(value.clamped(to: 0...1), forKey: Key.textOpacity)
    }

    func setTextColor(_ color: NSColor) {
        setColor(color, redKey: Key.textRed, greenKey: Key.textGreen, blueKey: Key.textBlue)
    }

    func applyConfigurationA() {
        setValues([
            Key.textRed: 0.0,
            Key.textGreen: 0.0,
            Key.textBlue: 0.0,
            Key.textOpacity: 0.08,
        ])
    }

    func applyFullyVisible() {
        setValues([
            Key.textRed: 0.0,
            Key.textGreen: 0.0,
            Key.textBlue: 0.0,
            Key.textOpacity: 1.0,
        ])
    }

    private func set(_ value: Any, forKey key: String) {
        defaults.set(value, forKey: key)
        onChange?()
    }

    private func setValues(_ values: [String: Any]) {
        for (key, value) in values {
            defaults.set(value, forKey: key)
        }
        onChange?()
    }

    private func setColor(_ color: NSColor, redKey: String, greenKey: String, blueKey: String) {
        guard let rgb = color.usingColorSpace(.sRGB) else { return }
        defaults.set(rgb.redComponent, forKey: redKey)
        defaults.set(rgb.greenComponent, forKey: greenKey)
        defaults.set(rgb.blueComponent, forKey: blueKey)
        onChange?()
    }
}

private extension Double {
    func clamped(to range: ClosedRange<Double>) -> Double {
        min(max(self, range.lowerBound), range.upperBound)
    }
}

struct VoiceOverlaySnapshot: Equatable {
    let status: String
    let isCapturing: Bool
    let interviewer: String
    let me: String
    let answer: String
}

@MainActor
final class LatestAnswerMonitor {
    private let url: URL
    private let secondaryURL: URL?
    private let pageCommandURL: URL
    private let stateURL: URL?
    private let displayDirectory: URL
    private let voiceAnswerURL: URL?
    private var lastViewKey: String?
    private var timer: Timer?
    private var lastAnswer: String?
    private var lastVoiceSnapshot: VoiceOverlaySnapshot?
    private var lastPageCommand: String?
    private var pageCommandInitialized = false
    let isVoiceMode: Bool
    var onChange: ((String) -> Void)?
    var onVoiceChange: ((VoiceOverlaySnapshot) -> Void)?
    var onPageDown: (() -> Void)?
    var onPageUp: (() -> Void)?
    var onMoveToMouse: (() -> Void)?
    var onToggleVisibility: (() -> Void)?
    var sessionRootURL: URL? {
        isVoiceMode ? displayDirectory.deletingLastPathComponent() : nil
    }

    init(url: URL? = nil, pageCommandURL: URL? = nil) {
        let arguments = ProcessInfo.processInfo.arguments
        let voiceIndex = arguments.firstIndex(of: "--lanshot-voice-dir")
        let voiceDirectory = voiceIndex.flatMap {
            $0 + 1 < arguments.count ? URL(fileURLWithPath: arguments[$0 + 1], isDirectory: true) : nil
        }
        if let voiceDirectory {
            self.displayDirectory = voiceDirectory
            self.url = url ?? voiceDirectory.appendingPathComponent("interviewer.txt")
            self.secondaryURL = voiceDirectory.appendingPathComponent("me.txt")
            self.pageCommandURL = pageCommandURL
                ?? voiceDirectory.appendingPathComponent("voice_overlay_command.txt")
            self.stateURL = nil
            self.voiceAnswerURL = voiceDirectory.appendingPathComponent("answer.txt")
            self.isVoiceMode = true
            return
        }
        let index = arguments.firstIndex(of: "--lanshot-display-dir")
        let configured: String? = index.flatMap { $0 + 1 < arguments.count ? arguments[$0 + 1] : nil }
            ?? ProcessInfo.processInfo.environment["LANSHOT_DISPLAY_DIR"]
        let dataDirectory = configured.map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/lanshot-receiver")
        self.displayDirectory = dataDirectory
        self.url = url ?? dataDirectory.appendingPathComponent("latest.txt")
        self.secondaryURL = nil
        self.pageCommandURL = pageCommandURL ?? dataDirectory.appendingPathComponent("page.txt")
        self.stateURL = configured == nil ? nil : dataDirectory.appendingPathComponent("latest_state.json")
        self.voiceAnswerURL = nil
        self.isVoiceMode = false
    }

    func requestCapture() {
        guard stateURL != nil else { return }
        writeJSON(["id": UUID().uuidString.lowercased(), "at": Date().timeIntervalSince1970],
                  name: "capture_request.json")
    }

    func toggleVoiceCapture() {
        guard isVoiceMode else { return }
        let state = readText(from: displayDirectory.appendingPathComponent("capture.log"))
        guard state != "submitting" && state != "stopping" else { return }
        let command = state == "running" || state == "starting" ? "stop" : "start"
        let requestedState = command == "start" ? "starting\n" : "stopping\n"
        try? requestedState.write(
            to: displayDirectory.appendingPathComponent("capture.log"),
            atomically: true,
            encoding: .utf8
        )
        try? "\(command) \(UUID().uuidString.lowercased())\n".write(
            to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
            atomically: true,
            encoding: .utf8
        )
    }

    func ensureVoiceCaptureStarted() {
        guard isVoiceMode else { return }
        let state = readText(from: displayDirectory.appendingPathComponent("capture.log"))
        if state != "running" && state != "starting" && state != "submitting" {
            toggleVoiceCapture()
        }
    }

    func submitVoiceQuestion() {
        guard isVoiceMode else { return }
        let state = readText(from: displayDirectory.appendingPathComponent("capture.log"))
        guard state != "submitting" && state != "stopping" else { return }
        try? "submitting\n".write(
            to: displayDirectory.appendingPathComponent("capture.log"),
            atomically: true,
            encoding: .utf8
        )
        try? "submit \(UUID().uuidString.lowercased())\n".write(
            to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
            atomically: true,
            encoding: .utf8
        )
    }

    func quitVoiceMode() {
        guard isVoiceMode else { return }
        try? "quitting\n".write(
            to: displayDirectory.appendingPathComponent("capture.log"),
            atomically: true,
            encoding: .utf8
        )
        try? "shutdown \(UUID().uuidString.lowercased())\n".write(
            to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
            atomically: true,
            encoding: .utf8
        )
    }

    func requestModeSwitch(to mode: String) {
        guard mode == "voice" || mode == "screenshot" else { return }
        if isVoiceMode {
            guard mode == "screenshot" else { return }
            try? "switching\n".write(
                to: displayDirectory.appendingPathComponent("capture.log"),
                atomically: true,
                encoding: .utf8
            )
            try? "switch-screenshot \(UUID().uuidString.lowercased())\n".write(
                to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
                atomically: true,
                encoding: .utf8
            )
            return
        }
        guard mode == "voice" else { return }
        writeJSON(
            [
                "id": UUID().uuidString.lowercased(),
                "at": Date().timeIntervalSince1970,
                "mode": "voice",
            ],
            name: "mode_request.json"
        )
    }

    func requestNewSession() {
        guard isVoiceMode else { return }
        try? "new-session \(UUID().uuidString.lowercased())\n".write(
            to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
            atomically: true,
            encoding: .utf8
        )
    }

    func requestActivateSession(_ sessionID: String) {
        guard isVoiceMode, !sessionID.isEmpty, !sessionID.contains(" ") else { return }
        try? "activate-session \(sessionID) \(UUID().uuidString.lowercased())\n".write(
            to: displayDirectory.appendingPathComponent("voice_capture_command.txt"),
            atomically: true,
            encoding: .utf8
        )
    }

    private func writeJSON(_ value: [String: Any], name: String) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        do {
            try FileManager.default.createDirectory(at: displayDirectory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let path = displayDirectory.appendingPathComponent(name)
            try data.write(to: path, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        } catch { NSLog("LanShot: cannot write display metadata") }
    }

    func start() {
        if isVoiceMode {
            try? "\(ProcessInfo.processInfo.processIdentifier)\n".write(
                to: displayDirectory.appendingPathComponent("voice_overlay.pid"),
                atomically: true,
                encoding: .utf8
            )
            writeJSON(
                ["status": "running", "at": Date().timeIntervalSince1970],
                name: "voice_overlay_status.json"
            )
        }
        reloadAnswer()
        lastPageCommand = readText(from: pageCommandURL)
        pageCommandInitialized = true
        let timer = Timer.scheduledTimer(withTimeInterval: 0.10, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.reloadAnswer()
                self?.reloadPageCommand()
            }
        }
        timer.tolerance = 0.02
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        fileCache.removeAll()
        if isVoiceMode {
            try? FileManager.default.removeItem(
                at: displayDirectory.appendingPathComponent("voice_overlay.pid")
            )
            writeJSON(
                ["status": "stopped", "at": Date().timeIntervalSince1970],
                name: "voice_overlay_status.json"
            )
        } else if stateURL != nil {
            writeJSON(["status": "stopped", "at": Date().timeIntervalSince1970], name: "overlay_status.json")
        }
    }

    var isVoiceCapturing: Bool {
        lastVoiceSnapshot?.isCapturing ?? false
    }

    private func reloadAnswer() {
        if isVoiceMode {
            reloadVoiceTranscript()
            return
        }
        if let stateURL = stateURL {
            reloadVersionedState(stateURL)
            return
        }
        guard
            let answer = readText(from: url),
            !answer.isEmpty,
            answer != lastAnswer
        else { return }

        lastAnswer = answer
        onChange?(answer)
    }

    private func reloadVoiceTranscript() {
        let captureState = readText(
            from: displayDirectory.appendingPathComponent("capture.log")
        ) ?? "stopped"
        let isCapturing = captureState == "running"
        let snapshot = VoiceOverlaySnapshot(
            status: voiceStatusText(state: captureState),
            isCapturing: isCapturing,
            interviewer: readText(from: url) ?? "等待系统声音...",
            me: secondaryURL.flatMap { readText(from: $0) } ?? "等待麦克风声音...",
            answer: voiceAnswerURL.flatMap { readText(from: $0) } ?? "等待答案..."
        )
        guard snapshot != lastVoiceSnapshot else { return }
        lastVoiceSnapshot = snapshot
        onVoiceChange?(snapshot)
    }

    private func voiceStatusText(state: String) -> String {
        let hotkeyReady = readText(
            from: displayDirectory.appendingPathComponent("voice_hotkey_status.txt")
        ) == "ready"
        let submitHint = hotkeyReady ? "F22 发送问题 | F23/F24 翻页" : "菜单发送问题"
        switch state {
        case "running":
            let pidURL = displayDirectory.appendingPathComponent("capture.pid")
            let attributes = try? FileManager.default.attributesOfItem(atPath: pidURL.path)
            let startedAt = attributes?[.modificationDate] as? Date ?? Date()
            let components = Calendar.current.dateComponents(
                [.hour, .minute, .second],
                from: startedAt
            )
            let startTime = String(
                format: "%02d:%02d:%02d",
                components.hour ?? 0,
                components.minute ?? 0,
                components.second ?? 0
            )
            let elapsed = max(0, Int(Date().timeIntervalSince(startedAt)))
            return String(
                format: "正在采集 | %@ 开始 | %02d:%02d | %@",
                startTime,
                elapsed / 60,
                elapsed % 60,
                submitHint
            )
        case "starting":
            return "正在启动采集... | \(submitHint)"
        case "stopping":
            return "正在停止并保存本轮..."
        case "submitting":
            return "正在停止识别并发送问题..."
        case "quitting":
            return "正在保存并退出 LanShot..."
        case "switching":
            return "正在切换到截屏模式..."
        case let value where value.hasPrefix("failed:"):
            return "采集异常 | \(submitHint)"
        default:
            return "就绪 | \(submitHint)"
        }
    }

    private func reloadVersionedState(_ path: URL) {
        writeJSON(["status": "running", "at": Date().timeIntervalSince1970,
                   "protocol": 1], name: "overlay_status.json")
        guard let data = try? Data(contentsOf: path), data.count <= 2 * 1024 * 1024,
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              value["schema"] as? Int == 1, let version = value["version"] as? Int,
              let stream = value["stream_id"] as? String, let at = value["at"] as? Double,
              let text = value["text"] as? String else {
            let message = "尚未收到有效任务状态；请检查显示桥接服务。"
            if lastAnswer != message { lastAnswer = message; onChange?(message) }
            return
        }
        let stale = Date().timeIntervalSince1970 - at > 12 || at > Date().timeIntervalSince1970 + 5
        let displayed = stale ? "状态更新已中断；以下是缓存，不代表任务仍在正常处理。\n\n" + text : text
        let key = "\(stream):\(version):\(stale)"
        guard key != lastViewKey || displayed != lastAnswer else { return }
        lastViewKey = key
        lastAnswer = displayed
        guard let onChange = onChange else { return }
        onChange(displayed)
        if !stale, let current = value["current"] as? [String: Any],
           current["state"] as? String == "complete", let task = current["id"] as? String {
            // A main-loop turn after applying text: view application, not proof of human reading.
            DispatchQueue.main.async { [weak self] in
                guard self?.lastViewKey == key else { return }
                self?.writeJSON(["stream_id": stream, "task_id": task, "version": version,
                                 "consumer": "native-overlay", "at": Date().timeIntervalSince1970],
                                name: "display_ack.json")
            }
        }
    }

    private func reloadPageCommand() {
        let command = readText(from: pageCommandURL)
        guard pageCommandInitialized, command != nil, command != lastPageCommand else { return }
        lastPageCommand = command
        if command?.hasPrefix("up ") == true {
            onPageUp?()
        } else if command?.hasPrefix("mouse ") == true {
            onMoveToMouse?()
        } else if command?.hasPrefix("toggle ") == true {
            onToggleVisibility?()
        } else if command?.hasPrefix("down ") == true {
            onPageDown?()
        } else if (stateURL != nil || isVoiceMode) && command?.hasPrefix("quit ") == true {
            NSApp.terminate(nil)
        }
    }

    private var fileCache: [URL: (sec: Int, nsec: Int, size: Int64, text: String)] = [:]

    private func readText(from url: URL) -> String? {
        var statBuf = stat()
        guard stat(url.path, &statBuf) == 0 else {
            fileCache.removeValue(forKey: url)
            return nil
        }
        let sec = Int(statBuf.st_mtimespec.tv_sec)
        let nsec = Int(statBuf.st_mtimespec.tv_nsec)
        let size = Int64(statBuf.st_size)
        if let cached = fileCache[url], cached.sec == sec, cached.nsec == nsec, cached.size == size {
            return cached.text
        }
        guard
            let data = try? Data(contentsOf: url),
            let text = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
            !text.isEmpty
        else {
            fileCache.removeValue(forKey: url)
            return nil
        }
        fileCache[url] = (sec, nsec, size, text)
        return text
    }
}

@MainActor
final class FloatingPanel: NSPanel {
    private let preferences: OverlayPreferences
    private let followLatest: Bool
    private let onToggleCapture: () -> Void
    private var answer = "等待 AI 回答..."
    private var voiceSnapshot = VoiceOverlaySnapshot(
        status: "尚未采集 | 点击开始采集",
        isCapturing: false,
        interviewer: "等待系统声音...",
        me: "等待麦克风声音...",
        answer: "等待答案..."
    )

    init(
        preferences: OverlayPreferences,
        followLatest: Bool = false,
        onToggleCapture: @escaping () -> Void = {}
    ) {
        self.preferences = preferences
        self.followLatest = followLatest
        self.onToggleCapture = onToggleCapture
        let visibleFrame = NSScreen.main?.visibleFrame ?? .zero
        let panelSize = Self.panelSize(preferences: preferences, fitting: visibleFrame)
        super.init(
            contentRect: NSRect(origin: .zero, size: panelSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        isMovableByWindowBackground = true
        isReleasedWhenClosed = false
        hidesOnDeactivate = false
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        applyPreferences(preserveCenter: false)
        centerNearTop()
    }

    func applyPreferences(preserveCenter: Bool = true) {
        let oldCenter = NSPoint(x: frame.midX, y: frame.midY)
        let visibleFrame = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        let size = Self.panelSize(preferences: preferences, fitting: visibleFrame)
        let requestedOrigin = preserveCenter
            ? NSPoint(x: oldCenter.x - size.width / 2, y: oldCenter.y - size.height / 2)
            : frame.origin
        let origin = Self.clampedOrigin(requestedOrigin, size: size, visibleFrame: visibleFrame)

        setFrame(NSRect(origin: origin, size: size), display: true)
        sharingType = .none
        if followLatest {
            contentView = VoicePanelContentView(
                frame: NSRect(origin: .zero, size: size),
                preferences: preferences,
                snapshot: voiceSnapshot,
                onToggleCapture: onToggleCapture
            )
        } else {
            contentView = PanelContentView(
                frame: NSRect(origin: .zero, size: size),
                preferences: preferences,
                answer: answer,
                followLatest: false
            )
        }
    }

    func updateAnswer(_ answer: String) {
        self.answer = answer
        (contentView as? PanelContentView)?.updateAnswer(answer)
    }

    func updateVoice(_ snapshot: VoiceOverlaySnapshot) {
        voiceSnapshot = snapshot
        (contentView as? VoicePanelContentView)?.update(snapshot)
    }

    func pageDown() {
        if let content = contentView as? PanelContentView {
            content.pageDown()
        } else {
            (contentView as? VoicePanelContentView)?.pageDown()
        }
    }

    func pageUp() {
        if let content = contentView as? PanelContentView {
            content.pageUp()
        } else {
            (contentView as? VoicePanelContentView)?.pageUp()
        }
    }

    func moveToMouse() {
        let mouseLocation = NSEvent.mouseLocation
        let visibleFrame = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) })?
            .visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        let requestedOrigin = NSPoint(
            x: mouseLocation.x - frame.width / 2,
            y: mouseLocation.y - frame.height / 2
        )
        let origin = NSPoint(
            x: min(max(requestedOrigin.x, visibleFrame.minX), visibleFrame.maxX - frame.width),
            y: min(max(requestedOrigin.y, visibleFrame.minY), visibleFrame.maxY - frame.height)
        )
        setFrameOrigin(origin)
    }

    func toggleVisibility() {
        if isVisible {
            orderOut(nil)
        } else {
            orderFrontRegardless()
        }
    }

    func centerNearTop() {
        let visibleFrame = NSScreen.main?.visibleFrame ?? .zero
        let origin = NSPoint(
            x: visibleFrame.midX - frame.width / 2,
            y: visibleFrame.maxY - frame.height - 18
        )
        setFrameOrigin(origin)
    }

    private static func panelSize(
        preferences: OverlayPreferences,
        fitting visibleFrame: NSRect
    ) -> NSSize {
        guard visibleFrame.width > 0, visibleFrame.height > 0 else {
            return NSSize(width: preferences.width, height: preferences.height)
        }
        return NSSize(
            width: min(preferences.width, max(160, visibleFrame.width - 20)),
            height: min(preferences.height, max(160, visibleFrame.height - 20))
        )
    }

    private static func clampedOrigin(
        _ requested: NSPoint,
        size: NSSize,
        visibleFrame: NSRect
    ) -> NSPoint {
        guard visibleFrame.width > 0, visibleFrame.height > 0 else { return requested }
        return NSPoint(
            x: min(max(requested.x, visibleFrame.minX), visibleFrame.maxX - size.width),
            y: min(max(requested.y, visibleFrame.minY), visibleFrame.maxY - size.height)
        )
    }
}

@MainActor
final class PanelContentView: NSView {
    private let followLatest: Bool
    private var hoverTrackingArea: NSTrackingArea?
    private var baseTextColor = NSColor.black
    private var baseTextOpacity: CGFloat = 1
    private weak var answerTextView: NSTextView?
    private weak var answerScrollView: NSScrollView?

    init(
        frame frameRect: NSRect,
        preferences: OverlayPreferences,
        answer: String,
        followLatest: Bool
    ) {
        self.followLatest = followLatest
        super.init(frame: frameRect)

        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        baseTextColor = preferences.textColor
        baseTextOpacity = preferences.textOpacity

        let scrollView = NSScrollView(frame: bounds)
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.drawsBackground = false
        scrollView.borderType = .noBorder
        scrollView.hasVerticalScroller = false

        let textView = DraggableAnswerTextView(frame: bounds)
        textView.string = answer
        textView.font = .systemFont(ofSize: preferences.fontSize, weight: .semibold)
        textView.textColor = preferences.textColor.withAlphaComponent(preferences.textOpacity)
        textView.drawsBackground = false
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.isHorizontallyResizable = false
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainerInset = NSSize(width: 8, height: 8)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        answerTextView = textView
        answerScrollView = scrollView
        scrollView.documentView = textView
        addSubview(scrollView)

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            scrollView.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
            scrollView.topAnchor.constraint(equalTo: topAnchor, constant: 6),
            scrollView.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -6),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let hoverTrackingArea {
            removeTrackingArea(hoverTrackingArea)
        }
        let trackingArea = NSTrackingArea(
            rect: .zero,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(trackingArea)
        hoverTrackingArea = trackingArea
    }

    override func mouseEntered(with event: NSEvent) {
        applyHoverHighlight(true)
    }

    override func mouseExited(with event: NSEvent) {
        applyHoverHighlight(false)
    }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            openSettings()
        } else {
            window?.performDrag(with: event)
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        openSettings()
    }

    private func applyHoverHighlight(_ highlighted: Bool) {
        let minimumOpacity: CGFloat = highlighted ? 0.15 : 0
        let textOpacity = max(baseTextOpacity, minimumOpacity)
        answerTextView?.textColor = baseTextColor.withAlphaComponent(textOpacity)
    }

    func updateAnswer(_ answer: String) {
        guard answerTextView?.string != answer else { return }
        let wasNearBottom: Bool
        if let scrollView = answerScrollView, let documentView = scrollView.documentView {
            let clipView = scrollView.contentView
            let maximumOffset = max(0, documentView.bounds.height - clipView.bounds.height)
            wasNearBottom = clipView.bounds.origin.y >= maximumOffset - 48
        } else {
            wasNearBottom = true
        }
        let previousLength = answerTextView?.string.count ?? 0
        let isReset = answer.count < previousLength / 2 || answer.hasPrefix("等待") || answer.hasPrefix("正在生成")
        answerTextView?.string = answer
        if followLatest {
            if wasNearBottom || isReset {
                answerTextView?.scrollToEndOfDocument(nil)
            }
        } else {
            answerTextView?.scrollToBeginningOfDocument(nil)
        }
    }

    func pageDown() {
        guard let scrollView = answerScrollView, let documentView = scrollView.documentView else {
            return
        }
        let clipView = scrollView.contentView
        let maximumOffset = max(0, documentView.bounds.height - clipView.bounds.height)
        let currentOffset = clipView.bounds.origin.y
        let nextOffset: CGFloat
        if maximumOffset == 0 || currentOffset >= maximumOffset - 1 {
            nextOffset = 0
        } else {
            nextOffset = min(maximumOffset, currentOffset + clipView.bounds.height * 0.5)
        }
        animateScroll(scrollView, to: nextOffset)
    }

    func pageUp() {
        guard let scrollView = answerScrollView, let documentView = scrollView.documentView else {
            return
        }
        let clipView = scrollView.contentView
        let maximumOffset = max(0, documentView.bounds.height - clipView.bounds.height)
        let currentOffset = clipView.bounds.origin.y
        let previousOffset: CGFloat
        if maximumOffset == 0 || currentOffset <= 1 {
            previousOffset = maximumOffset
        } else {
            previousOffset = max(0, currentOffset - clipView.bounds.height * 0.5)
        }
        animateScroll(scrollView, to: previousOffset)
    }

    private func animateScroll(_ scrollView: NSScrollView, to offset: CGFloat) {
        let clipView = scrollView.contentView
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            context.allowsImplicitAnimation = true
            clipView.animator().setBoundsOrigin(NSPoint(x: 0, y: offset))
        } completionHandler: {
            scrollView.reflectScrolledClipView(clipView)
        }
    }

    @objc private func openSettings() {
        (NSApp.delegate as? AppDelegate)?.showSettings()
    }

}

@MainActor
final class VoicePanelContentView: NSView {
    private let onToggleCapture: () -> Void
    private let textColor: NSColor
    private let textOpacity: CGFloat
    private var statusLabel: NSTextField!
    private var systemTitle: NSTextField!
    private var microphoneTitle: NSTextField!
    private var answerTitle: NSTextField!
    private var systemScrollView: NSScrollView!
    private var microphoneScrollView: NSScrollView!
    private var answerScrollView: NSScrollView!
    private var systemTextView: NSTextView!
    private var microphoneTextView: NSTextView!
    private var answerTextView: NSTextView!

    init(
        frame frameRect: NSRect,
        preferences: OverlayPreferences,
        snapshot: VoiceOverlaySnapshot,
        onToggleCapture: @escaping () -> Void
    ) {
        self.onToggleCapture = onToggleCapture
        self.textColor = preferences.textColor
        self.textOpacity = preferences.textOpacity
        super.init(frame: frameRect)

        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
        statusLabel = makeLabel(size: 13, weight: .semibold)
        systemTitle = makeLabel(text: "系统声音", size: 14, weight: .bold)
        microphoneTitle = makeLabel(text: "麦克风", size: 14, weight: .bold)
        answerTitle = makeLabel(text: "答案", size: 14, weight: .bold)
        (systemScrollView, systemTextView) = makeTextArea(fontSize: preferences.fontSize)
        (microphoneScrollView, microphoneTextView) = makeTextArea(fontSize: preferences.fontSize)
        (answerScrollView, answerTextView) = makeTextArea(fontSize: preferences.fontSize)

        for view in [
            statusLabel,
            systemTitle,
            microphoneTitle,
            answerTitle,
            systemScrollView,
            microphoneScrollView,
            answerScrollView,
        ] {
            if let view { addSubview(view) }
        }
        update(snapshot)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layout() {
        super.layout()
        let inset: CGFloat = 12
        let gap: CGFloat = 12
        let titleHeight: CGFloat = 22
        let headerHeight: CGFloat = 28
        let contentWidth = max(0, bounds.width - inset * 2)
        let headerY = max(inset, bounds.height - inset - headerHeight)
        statusLabel.frame = NSRect(
            x: inset,
            y: headerY,
            width: contentWidth,
            height: headerHeight
        )

        let bodyHeight = max(0, headerY - inset - gap)
        let transcriptHeight = bodyHeight * 0.35
        let answerHeight = max(0, bodyHeight - transcriptHeight - gap)
        let answerY = inset
        let transcriptY = answerY + answerHeight + gap
        let columnWidth = max(0, (contentWidth - gap) / 2)

        systemTitle.frame = NSRect(
            x: inset,
            y: transcriptY + transcriptHeight - titleHeight,
            width: columnWidth,
            height: titleHeight
        )
        microphoneTitle.frame = NSRect(
            x: inset + columnWidth + gap,
            y: transcriptY + transcriptHeight - titleHeight,
            width: columnWidth,
            height: titleHeight
        )
        systemScrollView.frame = NSRect(
            x: inset,
            y: transcriptY,
            width: columnWidth,
            height: max(0, transcriptHeight - titleHeight)
        )
        microphoneScrollView.frame = NSRect(
            x: inset + columnWidth + gap,
            y: transcriptY,
            width: columnWidth,
            height: max(0, transcriptHeight - titleHeight)
        )
        answerTitle.frame = NSRect(
            x: inset,
            y: answerY + answerHeight - titleHeight,
            width: contentWidth,
            height: titleHeight
        )
        answerScrollView.frame = NSRect(
            x: inset,
            y: answerY,
            width: contentWidth,
            height: max(0, answerHeight - titleHeight)
        )
    }

    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }

    func update(_ snapshot: VoiceOverlaySnapshot) {
        if statusLabel.stringValue != snapshot.status {
            statusLabel.stringValue = snapshot.status
        }
        let targetColor = (snapshot.isCapturing ? NSColor.systemGreen : textColor)
            .withAlphaComponent(max(textOpacity, 0.55))
        if statusLabel.textColor != targetColor {
            statusLabel.textColor = targetColor
        }
        updateTextView(systemTextView, in: systemScrollView, with: snapshot.interviewer)
        updateTextView(microphoneTextView, in: microphoneScrollView, with: snapshot.me)
        updateTextView(answerTextView, in: answerScrollView, with: snapshot.answer)
    }

    private func updateTextView(_ textView: NSTextView, in scrollView: NSScrollView, with newText: String) {
        guard textView.string != newText else { return }
        let wasNearBottom = isScrolledNearBottom(scrollView: scrollView)
        let isReset = newText.count < textView.string.count / 2 || newText.hasPrefix("等待") || newText.hasPrefix("正在生成")
        textView.string = newText
        if wasNearBottom || isReset {
            textView.scrollToEndOfDocument(nil)
        }
    }

    private func isScrolledNearBottom(scrollView: NSScrollView, threshold: CGFloat = 48) -> Bool {
        guard let documentView = scrollView.documentView else { return true }
        let clipView = scrollView.contentView
        let maximumOffset = max(0, documentView.bounds.height - clipView.bounds.height)
        return clipView.bounds.origin.y >= maximumOffset - threshold
    }

    func pageDown() {
        scrollAnswer(down: true)
    }

    func pageUp() {
        scrollAnswer(down: false)
    }

    private func scrollAnswer(down: Bool) {
        guard
            let scrollView = answerScrollView,
            let documentView = scrollView.documentView
        else { return }
        let clipView = scrollView.contentView
        let maximumOffset = max(0, documentView.bounds.height - clipView.bounds.height)
        let currentOffset = clipView.bounds.origin.y
        let offset: CGFloat
        if down {
            offset = maximumOffset == 0 || currentOffset >= maximumOffset - 1
                ? 0
                : min(maximumOffset, currentOffset + clipView.bounds.height * 0.5)
        } else {
            offset = maximumOffset == 0 || currentOffset <= 1
                ? maximumOffset
                : max(0, currentOffset - clipView.bounds.height * 0.5)
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            context.allowsImplicitAnimation = true
            clipView.animator().setBoundsOrigin(NSPoint(x: 0, y: offset))
        } completionHandler: {
            scrollView.reflectScrolledClipView(clipView)
        }
    }

    private func makeLabel(
        text: String = "",
        size: CGFloat,
        weight: NSFont.Weight
    ) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = textColor.withAlphaComponent(max(textOpacity, 0.55))
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    private func makeTextArea(fontSize: CGFloat) -> (NSScrollView, NSTextView) {
        let scrollView = NSScrollView(frame: .zero)
        scrollView.drawsBackground = false
        scrollView.borderType = .noBorder
        scrollView.hasVerticalScroller = false
        let textView = DraggableAnswerTextView(frame: .zero)
        textView.font = .systemFont(ofSize: fontSize, weight: .semibold)
        textView.textColor = textColor.withAlphaComponent(textOpacity)
        textView.drawsBackground = false
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.isHorizontallyResizable = false
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainerInset = NSSize(width: 6, height: 6)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        scrollView.documentView = textView
        return (scrollView, textView)
    }

    @objc private func toggleCapture() {
        onToggleCapture()
    }
}

@MainActor
final class DraggableAnswerTextView: NSTextView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            openSettings()
        } else {
            window?.performDrag(with: event)
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        openSettings()
    }

    @objc private func openSettings() {
        (NSApp.delegate as? AppDelegate)?.showSettings()
    }
}

@MainActor
final class SettingsViewController: NSViewController {
    private let preferences: OverlayPreferences
    private let onCenter: () -> Void

    private var widthSlider: NSSlider!
    private var heightSlider: NSSlider!
    private var fontSlider: NSSlider!
    private var textOpacitySlider: NSSlider!
    private var widthField: NSTextField!
    private var heightField: NSTextField!
    private var fontField: NSTextField!
    private var textOpacityValue: NSTextField!
    private var textColorWell: NSColorWell!

    init(preferences: OverlayPreferences, onCenter: @escaping () -> Void) {
        self.preferences = preferences
        self.onCenter = onCenter
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 560, height: 570))
    }

    override func viewDidLoad() {
        super.viewDidLoad()

        widthSlider = makeSlider(
            min: preferences.widthRange.lowerBound,
            max: preferences.widthRange.upperBound,
            action: #selector(widthChanged)
        )
        heightSlider = makeSlider(
            min: preferences.heightRange.lowerBound,
            max: preferences.heightRange.upperBound,
            action: #selector(heightChanged)
        )
        fontSlider = makeSlider(min: 10, max: 48, action: #selector(fontChanged))
        textOpacitySlider = makeSlider(min: 0, max: 1, action: #selector(textOpacityChanged))

        widthField = makeNumberField(action: #selector(widthFieldChanged))
        heightField = makeNumberField(action: #selector(heightFieldChanged))
        fontField = makeNumberField(action: #selector(fontFieldChanged))
        textOpacityValue = valueLabel()

        textColorWell = makeColorWell(action: #selector(textColorChanged))

        let grid = NSGridView(views: [
            [rowLabel("Window width"), widthSlider, widthField],
            [rowLabel("Window height"), heightSlider, heightField],
            [rowLabel("Font size"), fontSlider, fontField],
            [rowLabel("Text opacity"), textOpacitySlider, textOpacityValue],
            [rowLabel("Text color"), textColorWell, filler()],
        ])
        grid.columnSpacing = 12
        grid.rowSpacing = 10
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 1).xPlacement = .leading
        grid.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Overlay Settings")
        title.font = .systemFont(ofSize: 20, weight: .semibold)

        let presetAButton = NSButton(title: "Configuration A", target: self, action: #selector(applyConfigurationA))
        let visibleButton = NSButton(title: "Fully visible", target: self, action: #selector(applyFullyVisible))
        let presets = NSStackView(views: [presetAButton, visibleButton])
        presets.orientation = .horizontal
        presets.distribution = .fillEqually
        presets.spacing = 10

        let centerButton = NSButton(title: "Center overlay", target: self, action: #selector(centerPressed))
        let quitButton = NSButton(title: "Quit app", target: self, action: #selector(quitPressed))
        let actions = NSStackView(views: [centerButton, quitButton])
        actions.orientation = .horizontal
        actions.distribution = .fillEqually
        actions.spacing = 10

        let stack = NSStackView(views: [title, presets, grid, actions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -22),
            presets.widthAnchor.constraint(equalTo: stack.widthAnchor),
            grid.widthAnchor.constraint(equalTo: stack.widthAnchor),
            actions.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])

        refresh()
    }

    func refresh() {
        guard isViewLoaded else { return }
        widthSlider.doubleValue = Double(preferences.width)
        heightSlider.doubleValue = Double(preferences.height)
        fontSlider.doubleValue = Double(preferences.fontSize)
        textOpacitySlider.doubleValue = Double(preferences.textOpacity)
        widthField.stringValue = String(Int(preferences.width.rounded()))
        heightField.stringValue = String(Int(preferences.height.rounded()))
        fontField.stringValue = String(Int(preferences.fontSize.rounded()))
        textOpacityValue.stringValue = percent(preferences.textOpacity)
        textColorWell.color = preferences.textColor
    }

    private func makeSlider(min: Double, max: Double, action: Selector) -> NSSlider {
        let slider = NSSlider(value: min, minValue: min, maxValue: max, target: self, action: action)
        slider.isContinuous = true
        slider.translatesAutoresizingMaskIntoConstraints = false
        slider.widthAnchor.constraint(equalToConstant: 270).isActive = true
        return slider
    }

    private func makeNumberField(action: Selector) -> NSTextField {
        let field = NSTextField(string: "")
        field.alignment = .right
        field.target = self
        field.action = action
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(equalToConstant: 62).isActive = true
        return field
    }

    private func makeColorWell(action: Selector) -> NSColorWell {
        let colorWell = NSColorWell(frame: .zero)
        colorWell.target = self
        colorWell.action = action
        colorWell.translatesAutoresizingMaskIntoConstraints = false
        colorWell.widthAnchor.constraint(equalToConstant: 64).isActive = true
        return colorWell
    }

    private func rowLabel(_ text: String) -> NSTextField {
        NSTextField(labelWithString: text)
    }

    private func valueLabel() -> NSTextField {
        let label = NSTextField(labelWithString: "")
        label.alignment = .right
        label.translatesAutoresizingMaskIntoConstraints = false
        label.widthAnchor.constraint(equalToConstant: 62).isActive = true
        return label
    }

    private func filler() -> NSView {
        NSView(frame: .zero)
    }

    private func percent(_ value: CGFloat) -> String {
        "\(Int((value * 100).rounded()))%"
    }

    @objc private func widthChanged() {
        preferences.setWidth(widthSlider.doubleValue)
        widthField.stringValue = String(Int(preferences.width.rounded()))
    }

    @objc private func heightChanged() {
        preferences.setHeight(heightSlider.doubleValue)
        heightField.stringValue = String(Int(preferences.height.rounded()))
    }

    @objc private func fontChanged() {
        preferences.setFontSize(fontSlider.doubleValue)
        fontField.stringValue = String(Int(preferences.fontSize.rounded()))
    }

    @objc private func textOpacityChanged() {
        preferences.setTextOpacity(textOpacitySlider.doubleValue)
        textOpacityValue.stringValue = percent(preferences.textOpacity)
    }

    @objc private func widthFieldChanged() {
        preferences.setWidth(widthField.doubleValue)
        refresh()
    }

    @objc private func heightFieldChanged() {
        preferences.setHeight(heightField.doubleValue)
        refresh()
    }

    @objc private func fontFieldChanged() {
        preferences.setFontSize(fontField.doubleValue)
        refresh()
    }

    @objc private func textColorChanged() {
        preferences.setTextColor(textColorWell.color)
    }

    @objc private func applyConfigurationA() {
        preferences.applyConfigurationA()
        refresh()
    }

    @objc private func applyFullyVisible() {
        preferences.applyFullyVisible()
        refresh()
    }

    @objc private func centerPressed() {
        onCenter()
    }

    @objc private func quitPressed() {
        NSApp.terminate(nil)
    }
}

@MainActor
final class SettingsWindowController: NSWindowController {
    private let settingsViewController: SettingsViewController

    init(preferences: OverlayPreferences, onCenter: @escaping () -> Void) {
        settingsViewController = SettingsViewController(preferences: preferences, onCenter: onCenter)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 570),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Overlay Settings"
        window.contentViewController = settingsViewController
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.sharingType = .none
        super.init(window: window)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func showWindow(_ sender: Any?) {
        settingsViewController.refresh()
        window?.center()
        super.showWindow(sender)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(sender)
    }
}

private struct ConversationMessage {
    let at: Date
    let input: String
    let answer: String
}

private struct ConversationSession {
    let id: String
    var title: String
    var createdAt: Date
    var messages: [ConversationMessage]
    var isCurrent: Bool
}

@MainActor
final class SessionManagerViewController: NSViewController, NSTableViewDataSource, NSTableViewDelegate {
    private let rootURL: URL
    private let onNewSession: () -> Void
    private let onActivateSession: (String) -> Void
    private var sessions: [ConversationSession] = []
    private var tableView: NSTableView!
    private var detailTextView: NSTextView!

    init(
        rootURL: URL,
        onNewSession: @escaping () -> Void,
        onActivateSession: @escaping (String) -> Void
    ) {
        self.rootURL = rootURL
        self.onNewSession = onNewSession
        self.onActivateSession = onActivateSession
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 820, height: 560))
    }

    override func viewDidLoad() {
        super.viewDidLoad()

        let newButton = NSButton(title: "新建会话", target: self, action: #selector(createSession))
        let activateButton = NSButton(
            title: "继续所选会话",
            target: self,
            action: #selector(activateSelectedSession)
        )
        let exportButton = NSButton(
            title: "导出 TXT",
            target: self,
            action: #selector(exportSelectedSession)
        )
        let refreshButton = NSButton(title: "刷新", target: self, action: #selector(reloadSessions))
        let folderButton = NSButton(title: "打开历史目录", target: self, action: #selector(openHistory))
        let toolbar = NSStackView(views: [newButton, activateButton, exportButton, refreshButton, folderButton])
        toolbar.orientation = .horizontal
        toolbar.spacing = 8
        toolbar.translatesAutoresizingMaskIntoConstraints = false

        tableView = NSTableView(frame: .zero)
        tableView.headerView = nil
        tableView.rowHeight = 46
        tableView.delegate = self
        tableView.dataSource = self
        tableView.addTableColumn(NSTableColumn(identifier: NSUserInterfaceItemIdentifier("session")))
        let tableScroll = NSScrollView(frame: .zero)
        tableScroll.documentView = tableView
        tableScroll.hasVerticalScroller = true
        tableScroll.borderType = .bezelBorder

        detailTextView = NSTextView(frame: NSRect(x: 0, y: 0, width: 520, height: 520))
        detailTextView.isEditable = false
        detailTextView.isSelectable = true
        detailTextView.isRichText = false
        detailTextView.isHorizontallyResizable = false
        detailTextView.isVerticallyResizable = true
        detailTextView.autoresizingMask = [.width]
        detailTextView.font = .systemFont(ofSize: 14)
        detailTextView.textContainerInset = NSSize(width: 12, height: 12)
        detailTextView.textContainer?.widthTracksTextView = true
        detailTextView.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        let detailScroll = NSScrollView(frame: .zero)
        detailScroll.documentView = detailTextView
        detailScroll.hasVerticalScroller = true
        detailScroll.borderType = .bezelBorder

        let splitView = NSSplitView(frame: .zero)
        splitView.isVertical = true
        splitView.dividerStyle = .thin
        splitView.translatesAutoresizingMaskIntoConstraints = false
        splitView.addArrangedSubview(tableScroll)
        splitView.addArrangedSubview(detailScroll)
        tableScroll.widthAnchor.constraint(equalToConstant: 260).isActive = true

        view.addSubview(toolbar)
        view.addSubview(splitView)
        NSLayoutConstraint.activate([
            toolbar.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            toolbar.topAnchor.constraint(equalTo: view.topAnchor, constant: 12),
            splitView.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            splitView.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            splitView.topAnchor.constraint(equalTo: toolbar.bottomAnchor, constant: 10),
            splitView.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -14),
        ])
        reloadSessions()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        sessions.count
    }

    func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        guard sessions.indices.contains(row) else { return nil }
        let session = sessions[row]
        let cell = NSTableCellView(frame: .zero)
        let current = session.isCurrent ? "当前 · " : ""
        let label = NSTextField(
            labelWithString: "\(current)\(session.title)\n\(session.messages.count) 个问题"
        )
        label.maximumNumberOfLines = 2
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(label)
        cell.textField = label
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        showSelectedSession()
    }

    @objc func reloadSessions() {
        let previousID = selectedSession?.id
        sessions = loadSessions()
        tableView?.reloadData()
        let selectedIndex = sessions.firstIndex { $0.id == previousID }
            ?? sessions.firstIndex { $0.isCurrent }
            ?? (sessions.isEmpty ? nil : 0)
        if let selectedIndex {
            tableView.selectRowIndexes(IndexSet(integer: selectedIndex), byExtendingSelection: false)
        } else {
            detailTextView?.string = "尚无会话。"
        }
        showSelectedSession()
    }

    private var selectedSession: ConversationSession? {
        guard tableView != nil, sessions.indices.contains(tableView.selectedRow) else { return nil }
        return sessions[tableView.selectedRow]
    }

    private func loadSessions() -> [ConversationSession] {
        var records: [String: ConversationSession] = [:]
        let currentID = readCurrentSessionID()
        for value in readJSONLines(rootURL.appendingPathComponent("sessions.jsonl")) {
            guard let id = value["id"] as? String else { continue }
            let created = value["created_at"] as? Double ?? 0
            records[id] = ConversationSession(
                id: id,
                title: value["title"] as? String ?? id,
                createdAt: Date(timeIntervalSince1970: created),
                messages: [],
                isCurrent: id == currentID
            )
        }
        for value in readJSONLines(rootURL.appendingPathComponent("conversation_history.jsonl")) {
            let id = value["conversation_id"] as? String
                ?? value["session_id"] as? String
                ?? "legacy"
            let created = value["at"] as? Double ?? 0
            var session = records[id] ?? ConversationSession(
                id: id,
                title: id == "legacy" ? "旧版历史" : id,
                createdAt: Date(timeIntervalSince1970: created),
                messages: [],
                isCurrent: id == currentID
            )
            session.messages.append(
                ConversationMessage(
                    at: Date(timeIntervalSince1970: created),
                    input: value["input"] as? String ?? "",
                    answer: value["answer"] as? String ?? ""
                )
            )
            records[id] = session
        }
        return records.values.sorted { left, right in
            if left.isCurrent != right.isCurrent { return left.isCurrent }
            return left.createdAt > right.createdAt
        }
    }

    private func readCurrentSessionID() -> String {
        let path = rootURL.appendingPathComponent("current_session.json")
        guard let data = try? Data(contentsOf: path),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ""
        }
        return value["id"] as? String ?? ""
    }

    private func readJSONLines(_ url: URL) -> [[String: Any]] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            guard let data = String(line).data(using: .utf8) else { return nil }
            return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
    }

    private func formattedSessionText(for session: ConversationSession) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        var sections = [
            "============================================================",
            "面试会话：\(session.title)",
            "会话标识：\(session.id)",
            "创建时间：\(formatter.string(from: session.createdAt))",
            "问题数量：\(session.messages.count)",
            "============================================================",
        ]
        for (index, message) in session.messages.enumerated() {
            let input = message.input.trimmingCharacters(in: .whitespacesAndNewlines)
            let answer = message.answer.trimmingCharacters(in: .whitespacesAndNewlines)
            sections.append(
                """
                【第 \(index + 1) 题 · \(formatter.string(from: message.at))】

                --- 问题输入 ---
                \(input)

                --- 模型回答 ---
                \(answer)
                """
            )
        }
        if session.messages.isEmpty {
            sections.append("（当前会话尚无问题记录。）")
        }
        return sections.joined(separator: "\n\n------------------------------------------------------------\n\n")
    }

    private func showSelectedSession() {
        guard let session = selectedSession else {
            detailTextView.string = "尚无选中的会话。"
            return
        }
        detailTextView.string = formattedSessionText(for: session)
        detailTextView.scrollToBeginningOfDocument(nil)
    }

    @objc private func exportSelectedSession() {
        guard let session = selectedSession else {
            let alert = NSAlert()
            alert.messageText = "请先选择一个会话"
            alert.informativeText = "左侧列表中未选中任何会话。"
            alert.runModal()
            return
        }
        let savePanel = NSSavePanel()
        savePanel.title = "导出面试会话为 TXT"
        let cleanTitle = session.title
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: " ", with: "_")
            .replacingOccurrences(of: "/", with: "-")
        savePanel.nameFieldStringValue = "\(cleanTitle).txt"
        savePanel.canCreateDirectories = true
        if #available(macOS 11.0, *) {
            savePanel.allowedContentTypes = [.plainText]
        } else {
            savePanel.allowedFileTypes = ["txt"]
        }

        let content = formattedSessionText(for: session)
        let handler: (NSApplication.ModalResponse) -> Void = { response in
            guard response == .OK, let url = savePanel.url else { return }
            do {
                try content.write(to: url, atomically: true, encoding: .utf8)
                let alert = NSAlert()
                alert.messageText = "导出成功"
                alert.informativeText = "会话已成功导出至：\n\(url.path)"
                alert.addButton(withTitle: "确定")
                alert.addButton(withTitle: "在访达中显示")
                let choice = alert.runModal()
                if choice == .alertSecondButtonReturn {
                    NSWorkspace.shared.activateFileViewerSelecting([url])
                }
            } catch {
                let alert = NSAlert()
                alert.messageText = "导出失败"
                alert.informativeText = error.localizedDescription
                alert.runModal()
            }
        }

        if let window = view.window {
            savePanel.beginSheetModal(for: window, completionHandler: handler)
        } else {
            handler(savePanel.runModal())
        }
    }

    @objc private func createSession() {
        onNewSession()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
            self?.reloadSessions()
        }
    }

    @objc private func activateSelectedSession() {
        guard let session = selectedSession, session.id != "legacy" else { return }
        onActivateSession(session.id)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
            self?.reloadSessions()
        }
    }

    @objc private func openHistory() {
        let history = rootURL.appendingPathComponent("questions", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: history,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(history)
    }
}

@MainActor
final class SessionManagerWindowController: NSWindowController {
    private let contentController: SessionManagerViewController

    init(
        rootURL: URL,
        onNewSession: @escaping () -> Void,
        onActivateSession: @escaping (String) -> Void
    ) {
        contentController = SessionManagerViewController(
            rootURL: rootURL,
            onNewSession: onNewSession,
            onActivateSession: onActivateSession
        )
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 560),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "LanShot 会话管理"
        window.contentViewController = contentController
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.sharingType = .none
        super.init(window: window)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func showWindow(_ sender: Any?) {
        contentController.reloadSessions()
        window?.center()
        super.showWindow(sender)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(sender)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let answerMonitor = LatestAnswerMonitor()
    private lazy var preferences = OverlayPreferences(isVoiceMode: answerMonitor.isVoiceMode)
    private var panel: FloatingPanel?
    private var settingsController: SettingsWindowController?
    private var sessionController: SessionManagerWindowController?
    private var statusItem: NSStatusItem?
    private var captureToggleItem: NSMenuItem?
    private var autoHideMenuItem: NSMenuItem?
    private var lastCapturingState: Bool? = nil

    private var autoHideWhenIdle: Bool {
        get {
            if UserDefaults.standard.object(forKey: "overlay.voice.autoHideWhenIdle") == nil {
                return true
            }
            return UserDefaults.standard.bool(forKey: "overlay.voice.autoHideWhenIdle")
        }
        set {
            UserDefaults.standard.set(newValue, forKey: "overlay.voice.autoHideWhenIdle")
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let panel = FloatingPanel(
            preferences: preferences,
            followLatest: answerMonitor.isVoiceMode,
            onToggleCapture: { [weak self] in
                self?.answerMonitor.toggleVoiceCapture()
            }
        )
        self.panel = panel
        preferences.onChange = { [weak self] in
            self?.panel?.applyPreferences()
        }
        answerMonitor.onChange = { [weak self] answer in
            self?.panel?.updateAnswer(answer)
        }
        answerMonitor.onVoiceChange = { [weak self] snapshot in
            self?.panel?.updateVoice(snapshot)
            self?.updateVoiceStatus(snapshot)
        }
        answerMonitor.onPageDown = { [weak self] in
            self?.panel?.pageDown()
        }
        answerMonitor.onPageUp = { [weak self] in
            self?.panel?.pageUp()
        }
        answerMonitor.onMoveToMouse = { [weak self] in
            self?.panel?.moveToMouse()
        }
        answerMonitor.onToggleVisibility = { [weak self] in
            self?.panel?.toggleVisibility()
        }
        if answerMonitor.isVoiceMode {
            answerMonitor.ensureVoiceCaptureStarted()
        }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let symbolName = answerMonitor.isVoiceMode ? "mic.fill" : "camera.viewfinder"
        let icon = NSImage(systemSymbolName: symbolName, accessibilityDescription: "LanShot")
        icon?.isTemplate = true
        item.button?.image = icon
        item.button?.toolTip = answerMonitor.isVoiceMode ? "LanShot 语音模式" : "LanShot 截屏模式"
        let menu = NSMenu()
        let modeItem = NSMenuItem(title: "模式", action: nil, keyEquivalent: "")
        let modeMenu = NSMenu(title: "模式")
        let screenshotModeItem = NSMenuItem(
            title: "截屏模式",
            action: #selector(switchToScreenshotMode),
            keyEquivalent: ""
        )
        screenshotModeItem.target = self
        screenshotModeItem.state = answerMonitor.isVoiceMode ? .off : .on
        screenshotModeItem.isEnabled = answerMonitor.isVoiceMode
        modeMenu.addItem(screenshotModeItem)
        let voiceModeItem = NSMenuItem(
            title: "面试模式",
            action: #selector(switchToVoiceMode),
            keyEquivalent: ""
        )
        voiceModeItem.target = self
        voiceModeItem.state = answerMonitor.isVoiceMode ? .on : .off
        voiceModeItem.isEnabled = !answerMonitor.isVoiceMode
        modeMenu.addItem(voiceModeItem)
        menu.addItem(modeItem)
        menu.setSubmenu(modeMenu, for: modeItem)
        menu.addItem(NSMenuItem.separator())
        if answerMonitor.isVoiceMode {
            let sessionsItem = NSMenuItem(
                title: "会话管理...",
                action: #selector(showSessionManager),
                keyEquivalent: ""
            )
            sessionsItem.target = self
            menu.addItem(sessionsItem)
            menu.addItem(NSMenuItem.separator())
            let captureItem = NSMenuItem(
                title: "开始采集",
                action: #selector(toggleVoiceCapture),
                keyEquivalent: ""
            )
            captureItem.target = self
            menu.addItem(captureItem)
            captureToggleItem = captureItem
            let submitItem = NSMenuItem(
                title: "发送问题（F22）",
                action: #selector(submitVoiceQuestion),
                keyEquivalent: ""
            )
            submitItem.target = self
            menu.addItem(submitItem)
            menu.addItem(NSMenuItem.separator())
            let toggleItem = NSMenuItem(
                title: "显示或隐藏字幕",
                action: #selector(togglePanel),
                keyEquivalent: ""
            )
            toggleItem.target = self
            menu.addItem(toggleItem)
            let centerItem = NSMenuItem(
                title: "字幕移到屏幕上方",
                action: #selector(centerPanel),
                keyEquivalent: ""
            )
            centerItem.target = self
            menu.addItem(centerItem)
            let autoHideItem = NSMenuItem(
                title: "无采集时自动隐藏悬浮窗",
                action: #selector(toggleAutoHideWhenIdle),
                keyEquivalent: ""
            )
            autoHideItem.target = self
            autoHideItem.state = autoHideWhenIdle ? .on : .off
            autoHideMenuItem = autoHideItem
            menu.addItem(autoHideItem)
        } else {
            let captureItem = NSMenuItem(
                title: "截图并分析",
                action: #selector(manualCapture),
                keyEquivalent: ""
            )
            captureItem.target = self
            menu.addItem(captureItem)
        }
        let settingsItem = NSMenuItem(title: "显示设置", action: #selector(openSettingsMenu), keyEquivalent: "")
        settingsItem.target = self
        menu.addItem(settingsItem)
        menu.addItem(NSMenuItem.separator())
        if answerMonitor.isVoiceMode {
            let quitItem = NSMenuItem(
                title: "退出 LanShot",
                action: #selector(quitVoiceMode),
                keyEquivalent: ""
            )
            quitItem.target = self
            menu.addItem(quitItem)
        } else {
            menu.addItem(
                NSMenuItem(
                    title: "退出显示",
                    action: #selector(NSApplication.terminate(_:)),
                    keyEquivalent: ""
                )
            )
        }
        item.menu = menu
        statusItem = item
        answerMonitor.start()
        if answerMonitor.isVoiceMode {
            if !autoHideWhenIdle || answerMonitor.isVoiceCapturing {
                panel.orderFrontRegardless()
            }
        } else {
            panel.orderFrontRegardless()
            DispatchQueue.main.async { [weak self] in
                self?.showSettings()
            }
        }
    }

    @objc private func manualCapture() { answerMonitor.requestCapture() }
    @objc private func switchToScreenshotMode() { answerMonitor.requestModeSwitch(to: "screenshot") }
    @objc private func switchToVoiceMode() { answerMonitor.requestModeSwitch(to: "voice") }
    @objc private func showSessionManager() {
        guard let rootURL = answerMonitor.sessionRootURL else { return }
        if sessionController == nil {
            sessionController = SessionManagerWindowController(
                rootURL: rootURL,
                onNewSession: { [weak self] in
                    self?.answerMonitor.requestNewSession()
                },
                onActivateSession: { [weak self] sessionID in
                    self?.answerMonitor.requestActivateSession(sessionID)
                }
            )
        }
        sessionController?.showWindow(nil)
    }
    @objc private func toggleVoiceCapture() {
        let willStart = captureToggleItem?.title == "开始采集"
        answerMonitor.toggleVoiceCapture()
        if autoHideWhenIdle && willStart {
            panel?.orderFrontRegardless()
        }
    }
    @objc private func submitVoiceQuestion() {
        panel?.orderFrontRegardless()
        answerMonitor.submitVoiceQuestion()
    }
    @objc private func toggleAutoHideWhenIdle() {
        autoHideWhenIdle.toggle()
        autoHideMenuItem?.state = autoHideWhenIdle ? .on : .off
        if autoHideWhenIdle {
            if !answerMonitor.isVoiceCapturing {
                panel?.orderOut(nil)
            } else {
                panel?.orderFrontRegardless()
            }
        } else {
            panel?.orderFrontRegardless()
        }
    }
    @objc private func quitVoiceMode() { answerMonitor.quitVoiceMode() }
    @objc private func togglePanel() { panel?.toggleVisibility() }
    @objc private func centerPanel() { panel?.centerNearTop() }
    @objc private func openSettingsMenu() { showSettings() }

    func showSettings() {
        if settingsController == nil {
            settingsController = SettingsWindowController(preferences: preferences) { [weak self] in
                self?.centerOverlay()
            }
        }
        settingsController?.showWindow(nil)
    }

    func centerOverlay() {
        panel?.centerNearTop()
    }

    private func updateVoiceStatus(_ snapshot: VoiceOverlaySnapshot) {
        captureToggleItem?.title = snapshot.isCapturing ? "停止采集" : "开始采集"
        let symbolName = snapshot.isCapturing ? "mic.fill" : "mic.slash.fill"
        let icon = NSImage(systemSymbolName: symbolName, accessibilityDescription: "LanShot")
        icon?.isTemplate = true
        statusItem?.button?.image = icon
        statusItem?.button?.toolTip = snapshot.status

        if autoHideWhenIdle {
            if lastCapturingState != snapshot.isCapturing {
                lastCapturingState = snapshot.isCapturing
                if snapshot.isCapturing {
                    panel?.orderFrontRegardless()
                } else {
                    panel?.orderOut(nil)
                }
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        answerMonitor.stop()
    }
}

@main
@MainActor
struct CaptureExclusionDemo {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
