import AppKit

@MainActor
final class OverlayPreferences {
    private enum Key {
        static let width = "overlay.width"
        static let height = "overlay.height"
        static let fontSize = "overlay.fontSize"
        static let textRed = "overlay.text.red"
        static let textGreen = "overlay.text.green"
        static let textBlue = "overlay.text.blue"
        static let textOpacity = "overlay.text.opacity"
    }

    private let defaults: UserDefaults
    var onChange: (() -> Void)?

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        defaults.register(defaults: [
            Key.width: 256.0,
            Key.height: 256.0,
            Key.fontSize: 17.0,
            Key.textRed: 0.0,
            Key.textGreen: 0.0,
            Key.textBlue: 0.0,
            Key.textOpacity: 1.0,
        ])
    }

    var width: CGFloat { CGFloat(defaults.double(forKey: Key.width)) }
    var height: CGFloat { CGFloat(defaults.double(forKey: Key.height)) }
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
        set(value.clamped(to: 160...1200), forKey: Key.width)
    }

    func setHeight(_ value: Double) {
        set(value.clamped(to: 160...1200), forKey: Key.height)
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

@MainActor
final class LatestAnswerMonitor {
    private let url: URL
    private let secondaryURL: URL?
    private let pageCommandURL: URL
    private let stateURL: URL?
    private let displayDirectory: URL
    private var lastViewKey: String?
    private var timer: Timer?
    private var lastAnswer: String?
    private var lastPageCommand: String?
    private var pageCommandInitialized = false
    let isVoiceMode: Bool
    var onChange: ((String) -> Void)?
    var onPageDown: (() -> Void)?
    var onPageUp: (() -> Void)?
    var onMoveToMouse: (() -> Void)?
    var onToggleVisibility: (() -> Void)?

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
        self.isVoiceMode = false
    }

    func requestCapture() {
        guard stateURL != nil else { return }
        writeJSON(["id": UUID().uuidString.lowercased(), "at": Date().timeIntervalSince1970],
                  name: "capture_request.json")
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
        let timer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.reloadAnswer()
                self?.reloadPageCommand()
            }
        }
        timer.tolerance = 0.08
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
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
        let interviewer = readText(from: url)
        let me = secondaryURL.flatMap(readText)
        var sections: [String] = []
        if let interviewer {
            sections.append("面试官\n\(interviewer)")
        }
        if let me {
            sections.append("我\n\(me)")
        }
        let answer = sections.isEmpty ? "正在监听语音..." : sections.joined(separator: "\n\n")
        guard answer != lastAnswer else { return }
        lastAnswer = answer
        onChange?(answer)
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

    private func readText(from url: URL) -> String? {
        guard
            let data = try? Data(contentsOf: url),
            let text = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
            !text.isEmpty
        else { return nil }
        return text
    }
}

@MainActor
final class FloatingPanel: NSPanel {
    private let preferences: OverlayPreferences
    private let followLatest: Bool
    private var answer = "等待 AI 回答..."

    init(preferences: OverlayPreferences, followLatest: Bool = false) {
        self.preferences = preferences
        self.followLatest = followLatest
        let panelSize = NSSize(width: preferences.width, height: preferences.height)
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
        let size = NSSize(width: preferences.width, height: preferences.height)
        let origin = preserveCenter
            ? NSPoint(x: oldCenter.x - size.width / 2, y: oldCenter.y - size.height / 2)
            : frame.origin

        setFrame(NSRect(origin: origin, size: size), display: true)
        sharingType = .none
        contentView = PanelContentView(
            frame: NSRect(origin: .zero, size: size),
            preferences: preferences,
            answer: answer,
            followLatest: followLatest
        )
    }

    func updateAnswer(_ answer: String) {
        self.answer = answer
        (contentView as? PanelContentView)?.updateAnswer(answer)
    }

    func pageDown() {
        (contentView as? PanelContentView)?.pageDown()
    }

    func pageUp() {
        (contentView as? PanelContentView)?.pageUp()
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
        answerTextView?.string = answer
        if followLatest {
            answerTextView?.scrollToEndOfDocument(nil)
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

        widthSlider = makeSlider(min: 160, max: 1200, action: #selector(widthChanged))
        heightSlider = makeSlider(min: 160, max: 1200, action: #selector(heightChanged))
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

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let preferences = OverlayPreferences()
    private let answerMonitor = LatestAnswerMonitor()
    private var panel: FloatingPanel?
    private var settingsController: SettingsWindowController?
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let panel = FloatingPanel(
            preferences: preferences,
            followLatest: answerMonitor.isVoiceMode
        )
        self.panel = panel
        preferences.onChange = { [weak self] in
            self?.panel?.applyPreferences()
        }
        answerMonitor.onChange = { [weak self] answer in
            self?.panel?.updateAnswer(answer)
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
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let symbolName = answerMonitor.isVoiceMode ? "mic.fill" : "camera.viewfinder"
        let icon = NSImage(systemSymbolName: symbolName, accessibilityDescription: "LanShot")
        icon?.isTemplate = true
        item.button?.image = icon
        item.button?.toolTip = answerMonitor.isVoiceMode ? "LanShot 语音模式" : "LanShot 截屏模式"
        let menu = NSMenu()
        if answerMonitor.isVoiceMode {
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
        let quitTitle = answerMonitor.isVoiceMode ? "退出语音显示" : "退出显示"
        menu.addItem(NSMenuItem(title: quitTitle, action: #selector(NSApplication.terminate(_:)), keyEquivalent: ""))
        item.menu = menu
        statusItem = item
        answerMonitor.start()
        panel.orderFrontRegardless()
        if !answerMonitor.isVoiceMode {
            DispatchQueue.main.async { [weak self] in
                self?.showSettings()
            }
        }
    }

    @objc private func manualCapture() { answerMonitor.requestCapture() }
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
