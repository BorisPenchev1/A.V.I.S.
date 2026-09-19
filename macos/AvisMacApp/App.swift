import SwiftUI
import AppKit
import CoreImage

@main
struct AvisMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AvisViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 1100, minHeight: 760)
        }
    }
}

/// Without a proper .app bundle, a `swift run` executable launches as a background
/// accessory process: the window appears but the app never becomes active, so the
/// keyboard never reaches any text field. Forcing `.regular` activation policy makes
/// it a normal foreground app so typing works.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        NSApp.windows.first?.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

// MARK: - Models

enum MessageRole: String, Codable {
    case user, assistant
}

struct ChatMessage: Identifiable, Codable {
    var id = UUID()
    let role: MessageRole
    var text: String
}

struct SavedChat: Identifiable, Codable {
    var id = UUID()
    var title: String
    var messages: [ChatMessage]
    var grants: [String] = []
    var grantAll: Bool = false
    var updated: Date = Date()
}

struct CustomTool: Identifiable, Codable {
    var id = UUID()
    var name: String
    var instruction: String
}

enum ScheduleKind: String, Codable, CaseIterable {
    case interval = "Every"
    case dailyAt = "Daily at"
}

enum AutomationTargetKind: String, Codable {
    case builtIn, custom
}

struct Automation: Identifiable, Codable {
    var id = UUID()
    var name: String
    var targetKind: AutomationTargetKind
    var target: String            // built-in tool name, or a custom instruction
    var scheduleKind: ScheduleKind
    var intervalMinutes: Int = 60
    var hour: Int = 9
    var minute: Int = 0
    var enabled: Bool = true
    var lastRun: Date?
    var lastResult: String = ""

    func nextRun(from reference: Date = Date()) -> Date {
        let calendar = Calendar.current
        switch scheduleKind {
        case .interval:
            let base = lastRun ?? reference.addingTimeInterval(-Double(max(1, intervalMinutes)) * 60)
            return base.addingTimeInterval(Double(max(1, intervalMinutes)) * 60)
        case .dailyAt:
            var components = calendar.dateComponents([.year, .month, .day], from: reference)
            components.hour = hour
            components.minute = minute
            let today = calendar.date(from: components) ?? reference
            if let last = lastRun, calendar.isDate(last, inSameDayAs: today), last >= today {
                return calendar.date(byAdding: .day, value: 1, to: today) ?? today
            }
            return today > reference ? today : (calendar.date(byAdding: .day, value: 1, to: today) ?? today)
        }
    }
}

struct ContextProfile: Codable {
    let user: String
    let assistant: String
    let workingOn: [String]
    let helpsWith: [String]

    enum CodingKeys: String, CodingKey {
        case user, assistant, workingOn = "working_on", helpsWith = "helps_with"
    }

    // working_on may be a string (legacy) or an array of projects.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        user = (try? container.decode(String.self, forKey: .user)) ?? ""
        assistant = (try? container.decode(String.self, forKey: .assistant)) ?? ""
        helpsWith = (try? container.decode([String].self, forKey: .helpsWith)) ?? []
        if let list = try? container.decode([String].self, forKey: .workingOn) {
            workingOn = list
        } else if let single = try? container.decode(String.self, forKey: .workingOn) {
            workingOn = single.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        } else {
            workingOn = []
        }
    }

    init(user: String, assistant: String, workingOn: [String], helpsWith: [String]) {
        self.user = user
        self.assistant = assistant
        self.workingOn = workingOn
        self.helpsWith = helpsWith
    }
}

enum SidebarPage: String, CaseIterable {
    case chat = "Chat"
    case context = "Context"
    case tools = "Tools"
    case automation = "Automation"
    case mirror = "Mirror"
}

/// What the voice pipeline is doing right now, surfaced in the chat composer.
enum VoiceActivity: Equatable {
    case idle          // nothing going on
    case listening     // the microphone is capturing
    case transcribing  // turning the recording into text
    case speaking      // reading a reply aloud
}

// MARK: - View model

@MainActor
final class AvisViewModel: ObservableObject {
    @Published var page: SidebarPage = .chat
    @Published var prompt = ""
    @Published var messages: [ChatMessage] = [
        ChatMessage(role: .assistant, text: "Hello. I can inspect your Mac, run safe tools, and help you stay in control.")
    ]
    @Published var savedChats: [SavedChat] = []
    @Published var customTools: [CustomTool] = []
    @Published var newToolName = ""
    @Published var newToolInstruction = ""
    @Published var editingToolID: UUID?
    @Published var isSending = false
    @Published var automations: [Automation] = []

    // Voice interaction
    @Published var voiceActivity: VoiceActivity = .idle
    @Published var isRecording = false        // push-to-talk capture in progress
    @Published var conversationMode = false    // hands-free back-and-forth
    @Published var voiceError: String?
    private var recorder: VoiceProcess?        // mic capture subprocess
    private var speaker: VoiceProcess?         // streaming speech playback subprocess

    // Mirror (iPhone) server + notifications
    @Published var mirrorRunning = false
    @Published var mirrorPort = 8765
    @Published var mirrorToken = ""
    @Published var mirrorNtfyServer = "https://ntfy.sh"
    @Published var mirrorNtfyTopic = ""
    @Published var mirrorAllowTools = false
    @Published var mirrorError: String?
    private var mirrorProcess: Process?
    private var mirrorStopping = false

    // Context
    @Published var contextUser = ""
    @Published var contextAssistant = "AVIS, a local assistant running on this Mac."
    @Published var contextProjects: [String] = []
    @Published var contextHelpsWith = "OS checks\nSafe Mac actions\nLocal automation\nVoice interaction"
    @Published var contextSaved = false

    // Active-chat permission state
    private var currentChatID = UUID()
    private var grants: [String] = []
    private var grantAll = false
    private var pendingAgent = false   // force Qwen's agent loop on the next send

    let builtInTools = [
        "get_time_date", "get_battery_status", "get_bluetooth_devices",
        "list_notifications", "run_mac_diagnostics", "media_play_pause",
        "lock_device", "get_lock_status", "read_calendar_for_date"
    ]

    private let projectRoot = "/Users/bobi/Documents/GitHub/A.V.I.S."
    private var contextPath: String { projectRoot + "/context/constant_context.json" }
    private let chatsKey = "avis.savedChats"
    private let customToolsKey = "avis.customTools"
    private let automationsKey = "avis.automations"

    private var scheduler: Timer?
    private var runningAutomations: Set<UUID> = []

    init() {
        loadChats()
        loadCustomTools()
        loadAutomations()
        loadContext()
        loadMirrorConfig()
        startScheduler()
        // The mirror server is a child process; macOS does not reap it when this
        // app quits. Terminate it on quit so it does not linger and hold the port,
        // which would make the next "Start server" fail to bind.
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.stopMirror(); self?.stopAllVoice() }
        }
    }

    // MARK: Chats

    func newChat() {
        stopAllVoice()
        saveCurrentChat()
        currentChatID = UUID()
        grants = []
        grantAll = false
        messages = [ChatMessage(role: .assistant, text: "Started a fresh conversation. What do you want to check or do next?")]
        prompt = ""
        page = .chat
    }

    func openChat(_ chat: SavedChat) {
        stopAllVoice()
        saveCurrentChat()
        currentChatID = chat.id
        grants = chat.grants
        grantAll = chat.grantAll
        messages = chat.messages
        prompt = ""
        page = .chat
    }

    func deleteChat(_ chat: SavedChat) {
        savedChats.removeAll { $0.id == chat.id }
        persistChats()
        if chat.id == currentChatID { newChat() }
    }

    func renameChat(_ chat: SavedChat, to title: String) {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let index = savedChats.firstIndex(where: { $0.id == chat.id }) else { return }
        savedChats[index].title = trimmed
        persistChats()
    }

    func sendPrompt(speakReply: Bool = false) {
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        captureGrant(from: text)
        messages.append(ChatMessage(role: .user, text: text))
        prompt = ""
        isSending = true

        // Placeholder assistant message that streaming fills in gradually.
        let replyID = UUID()
        messages.append(ChatMessage(id: replyID, role: .assistant, text: ""))

        // For a voice turn, start the streaming speaker now and feed it tokens
        // as they arrive, so AVIS begins talking after the first chunk while the
        // rest of the reply is still being generated and synthesized.
        let speaker: VoiceProcess? = speakReply ? startStreamingSpeaker() : nil

        let history = Array(messages.dropLast(2)).map { ["role": $0.role.rawValue, "text": $0.text] }
        let request: [String: Any] = [
            "prompt": text,
            "history": history,
            "grants": grants,
            "grant_all": grantAll,
            "agent": pendingAgent,
        ]
        pendingAgent = false

        runBridge(request: request,
                  onToken: { [weak self] token in
                      self?.appendToken(token, messageID: replyID)
                      speaker?.write(token)
                  },
                  onFinal: { [weak self] full in
                      self?.setMessage(full, id: replyID)
                      speaker?.write(full)   // non-streamed (tool) turns arrive whole
                  },
                  onDone: { [weak self] in
                      self?.finishReply(id: replyID, speaker: speaker)
                  })
    }

    private func captureGrant(from text: String) {
        let lower = text.lowercased()
        let patterns = [
            #"you have permission(?: to)?(.*)"#,
            #"you are permitted(?: to)?(.*)"#,
            #"i (?:give|grant) you permission(?: to)?(.*)"#,
        ]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern),
                  let match = regex.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
                  let range = Range(match.range(at: 1), in: lower) else { continue }
            let phrase = lower[range].trimmingCharacters(in: CharacterSet(charactersIn: " .!,:;"))
            let blanket: Set<String> = ["", "everything", "anything", "do anything", "all", "do whatever you want", "whatever you want"]
            if blanket.contains(phrase) {
                grantAll = true
            } else {
                grants.append(phrase)
            }
            return
        }
    }

    private func appendToken(_ token: String, messageID: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }) else { return }
        messages[index].text += token
    }

    private func setMessage(_ text: String, id: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].text = text
    }

    private func finishReply(id: UUID, speaker: VoiceProcess?) {
        if let index = messages.firstIndex(where: { $0.id == id }), messages[index].text.isEmpty {
            messages[index].text = "The backend returned no response."
        }
        isSending = false
        saveCurrentChat()

        // A voice turn keeps its streaming speaker: close its input so it speaks
        // the tail and exits, which (in conversation mode) hands the turn back to
        // the microphone. A non-voice turn just resumes listening if in a call.
        if let speaker {
            speaker.closeInput()
        } else if conversationMode {
            startConverseListen()
        }
    }

    // MARK: Voice

    /// The chat send button. A live recording is transcribed and sent (and its
    /// reply spoken); otherwise whatever is typed is sent normally.
    func handleSend() {
        if isRecording {
            stopRecordingAndSend()
        } else {
            sendPrompt()
        }
    }

    /// Microphone button: start push-to-talk, or cancel a recording in progress.
    func toggleMicrophone() {
        guard !conversationMode else { return }
        if isRecording {
            cancelRecording()
        } else {
            startRecording()
        }
    }

    private func startRecording() {
        guard !isSending, recorder == nil, speaker == nil else { return }
        voiceError = nil
        voiceActivity = .listening
        isRecording = true
        let process = VoiceProcess()
        recorder = process
        process.start(projectRoot: projectRoot, arguments: ["listen-ptt"],
                      onEvent: { [weak self] event in self?.handlePTTEvent(event) },
                      onExit: { [weak self] in
                          guard let self, self.recorder === process else { return }
                          self.recorder = nil
                      })
    }

    func cancelRecording() {
        recorder?.terminate()
        recorder = nil
        isRecording = false
        if voiceActivity == .listening { voiceActivity = .idle }
    }

    /// Send pressed while recording: finalize the capture; transcription then
    /// fills the prompt and sends with the reply spoken.
    private func stopRecordingAndSend() {
        guard let process = recorder else {
            isRecording = false
            return
        }
        isRecording = false
        voiceActivity = .transcribing
        process.stopRecording()
    }

    private func handlePTTEvent(_ event: VoiceEvent) {
        switch event {
        case .listening:
            if isRecording { voiceActivity = .listening }
        case .text(let said):
            let trimmed = said.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else {
                voiceError = "I didn't catch anything — try again."
                voiceActivity = .idle
                return
            }
            prompt = trimmed
            // If Send was already pressed we're transcribing: deliver and send.
            // Otherwise the recording ended early — leave the text for the user.
            if voiceActivity == .transcribing {
                sendPrompt(speakReply: true)
            } else {
                isRecording = false
                voiceActivity = .idle
            }
        case .silence:
            voiceError = "I didn't catch anything — try again."
            voiceActivity = .idle
        case .error(let message):
            voiceError = message
            voiceActivity = .idle
        case .spoke:
            break
        }
    }

    // MARK: Conversation mode

    func toggleConversationMode() {
        if conversationMode { exitConversationMode() } else { enterConversationMode() }
    }

    private func enterConversationMode() {
        guard !isSending else { return }
        cancelRecording()
        voiceError = nil
        conversationMode = true
        startConverseListen()
    }

    func exitConversationMode() {
        conversationMode = false
        recorder?.terminate(); recorder = nil
        speaker?.terminate(); speaker = nil
        isRecording = false
        voiceActivity = .idle
    }

    /// Listen for the next spoken turn. The recorder stops on its own once the
    /// speaker pauses, so the user never signals "done".
    private func startConverseListen() {
        guard conversationMode, !isSending, recorder == nil, speaker == nil else { return }
        voiceError = nil
        voiceActivity = .listening
        let process = VoiceProcess()
        recorder = process
        process.start(projectRoot: projectRoot, arguments: ["converse"],
                      onEvent: { [weak self] event in self?.handleConverseEvent(event, process: process) },
                      onExit: { [weak self] in
                          guard let self, self.recorder === process else { return }
                          self.recorder = nil
                      })
    }

    private func handleConverseEvent(_ event: VoiceEvent, process: VoiceProcess) {
        guard conversationMode else { return }
        switch event {
        case .listening:
            voiceActivity = .listening
        case .text(let said):
            let trimmed = said.trimmingCharacters(in: .whitespacesAndNewlines)
            if recorder === process { recorder = nil }
            guard !trimmed.isEmpty else {
                startConverseListen()
                return
            }
            prompt = trimmed
            sendPrompt(speakReply: true)   // reply is spoken, then we listen again
        case .silence:
            if recorder === process { recorder = nil }
            startConverseListen()          // nothing heard yet; keep listening
        case .error(let message):
            voiceError = message
            exitConversationMode()
        case .spoke:
            break
        }
    }

    // MARK: Speech playback

    /// Spawn the streaming speaker for a voice turn. Tokens are written to it as
    /// they stream; `finishReply` closes its input when the reply is complete.
    private func startStreamingSpeaker() -> VoiceProcess {
        speaker?.terminate()
        voiceActivity = .speaking
        let process = VoiceProcess()
        speaker = process
        process.start(projectRoot: projectRoot, arguments: ["speak-stream"],
                      onEvent: { [weak self] event in
                          if case .error(let message) = event { self?.voiceError = message }
                      },
                      onExit: { [weak self] in self?.onSpeakingFinished(process) })
        return process
    }

    private func onSpeakingFinished(_ process: VoiceProcess) {
        guard speaker === process else { return }
        speaker = nil
        if conversationMode {
            startConverseListen()
        } else if voiceActivity == .speaking {
            voiceActivity = .idle
        }
    }

    func stopSpeaking() {
        speaker?.terminate()
        speaker = nil
        if !conversationMode { voiceActivity = .idle }
    }

    private func stopAllVoice() {
        conversationMode = false
        recorder?.terminate(); recorder = nil
        speaker?.terminate(); speaker = nil
        isRecording = false
        voiceActivity = .idle
    }

    // MARK: Tools

    func useBuiltInTool(_ name: String) {
        page = .chat
        prompt = name.replacingOccurrences(of: "_", with: " ")
    }

    func useCustomTool(_ tool: CustomTool) {
        page = .chat
        prompt = tool.instruction
        pendingAgent = true   // custom tools always let Qwen drive the tools
    }

    func startEditingTool(_ tool: CustomTool) {
        editingToolID = tool.id
        newToolName = tool.name
        newToolInstruction = tool.instruction
    }

    func cancelEditingTool() {
        editingToolID = nil
        newToolName = ""
        newToolInstruction = ""
    }

    func saveTool() {
        let name = newToolName.trimmingCharacters(in: .whitespacesAndNewlines)
        let instruction = newToolInstruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !instruction.isEmpty else { return }
        if let id = editingToolID, let index = customTools.firstIndex(where: { $0.id == id }) {
            customTools[index].name = name
            customTools[index].instruction = instruction
        } else {
            customTools.insert(CustomTool(name: name, instruction: instruction), at: 0)
        }
        persistCustomTools()
        cancelEditingTool()
    }

    func deleteTool(_ tool: CustomTool) {
        customTools.removeAll { $0.id == tool.id }
        persistCustomTools()
        if editingToolID == tool.id { cancelEditingTool() }
    }

    // MARK: Context

    func addProject() { contextProjects.append("") }
    func removeProject(at index: Int) {
        guard contextProjects.indices.contains(index) else { return }
        contextProjects.remove(at: index)
    }

    func saveContext() {
        let profile = ContextProfile(
            user: contextUser,
            assistant: contextAssistant,
            workingOn: contextProjects.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty },
            helpsWith: contextHelpsWith.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
        )
        do {
            let data = try JSONEncoder.pretty.encode(profile)
            try data.write(to: URL(fileURLWithPath: contextPath), options: .atomic)
            contextSaved = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.contextSaved = false }
        } catch {
            messages.append(ChatMessage(role: .assistant, text: "Could not save context: \(error.localizedDescription)"))
        }
    }

    // MARK: Automation

    func addAutomation(name: String, targetKind: AutomationTargetKind, target: String,
                       scheduleKind: ScheduleKind, intervalMinutes: Int, hour: Int, minute: Int) {
        let automation = Automation(name: name, targetKind: targetKind, target: target,
                                    scheduleKind: scheduleKind, intervalMinutes: intervalMinutes,
                                    hour: hour, minute: minute)
        automations.insert(automation, at: 0)
        persistAutomations()
    }

    func updateAutomation(_ automation: Automation) {
        guard let index = automations.firstIndex(where: { $0.id == automation.id }) else { return }
        automations[index] = automation
        persistAutomations()
    }

    func deleteAutomation(_ automation: Automation) {
        automations.removeAll { $0.id == automation.id }
        persistAutomations()
    }

    func toggleAutomation(_ automation: Automation) {
        guard let index = automations.firstIndex(where: { $0.id == automation.id }) else { return }
        automations[index].enabled.toggle()
        persistAutomations()
    }

    func runAutomationNow(_ automation: Automation) {
        runAutomation(automation)
    }

    private func startScheduler() {
        scheduler = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tickScheduler() }
        }
    }

    private func tickScheduler() {
        let now = Date()
        for automation in automations where automation.enabled {
            guard !runningAutomations.contains(automation.id) else { continue }
            if automation.nextRun() <= now {
                runAutomation(automation)
            }
        }
    }

    private func runAutomation(_ automation: Automation) {
        guard !runningAutomations.contains(automation.id) else { return }
        runningAutomations.insert(automation.id)
        let prompt = automation.targetKind == .builtIn
            ? automation.target.replacingOccurrences(of: "_", with: " ")
            : automation.target
        // Automations are pre-authorized by the user who created them. Custom
        // instructions run through Qwen's agent loop; built-in tools run directly.
        let request: [String: Any] = [
            "prompt": prompt,
            "history": [],
            "grants": [],
            "grant_all": true,
            "agent": automation.targetKind == .custom,
        ]
        var collected = ""
        runBridge(request: request,
                  onToken: { collected += $0 },
                  onFinal: { collected = $0 },
                  onDone: { [weak self] in
                      self?.completeAutomation(automation.id, name: automation.name, result: collected)
                  })
    }

    private func completeAutomation(_ id: UUID, name: String, result: String) {
        runningAutomations.remove(id)
        let output = result.isEmpty ? "Completed with no output." : result
        if let index = automations.firstIndex(where: { $0.id == id }) {
            automations[index].lastRun = Date()
            automations[index].lastResult = output
            persistAutomations()
        }
        let short = String(output.prefix(240))
        Notifier.show(title: "AVIS · \(name)", body: short)   // Mac banner
        recordAutomationToMirror(name: name, body: short)     // iPhone push + live feed
    }

    // MARK: Mirror

    private var mirrorConfigPath: String { projectRoot + "/mirror_config.json" }
    private var mirrorActivityPath: String { projectRoot + "/mirror_activity.jsonl" }

    var mirrorPhoneURL: String {
        "http://\(mirrorLanIP()):\(mirrorPort)/?token=\(mirrorToken)"
    }

    private func loadMirrorConfig() {
        if let data = try? Data(contentsOf: URL(fileURLWithPath: mirrorConfigPath)),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            mirrorPort = object["port"] as? Int ?? 8765
            mirrorToken = object["token"] as? String ?? ""
            mirrorNtfyServer = object["ntfy_server"] as? String ?? "https://ntfy.sh"
            mirrorNtfyTopic = object["ntfy_topic"] as? String ?? ""
            mirrorAllowTools = object["allow_tools"] as? Bool ?? false
        }
        if mirrorToken.isEmpty {
            mirrorToken = UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(16).lowercased()
            saveMirrorConfig()
        }
    }

    func saveMirrorConfig() {
        let object: [String: Any] = [
            "port": mirrorPort,
            "token": mirrorToken,
            "ntfy_server": mirrorNtfyServer,
            "ntfy_topic": mirrorNtfyTopic.trimmingCharacters(in: .whitespaces),
            "allow_tools": mirrorAllowTools,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted]) {
            try? data.write(to: URL(fileURLWithPath: mirrorConfigPath), options: .atomic)
        }
    }

    func startMirror() {
        guard !mirrorRunning else { return }
        saveMirrorConfig()
        mirrorError = nil
        let candidates = [projectRoot + "/.venv/bin/python", "/opt/homebrew/bin/python3", "/usr/bin/python3"]
        guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            mirrorError = "No Python interpreter found. Create the project .venv first."
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "server.mirror"]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = projectRoot
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        // Capture stderr so a startup failure (e.g. the port already in use) is
        // surfaced to the user instead of the toggle silently flipping back.
        let errPipe = Pipe()
        process.standardError = errPipe

        mirrorStopping = false
        process.terminationHandler = { [weak self] proc in
            let stderr = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            Task { @MainActor in
                guard let self else { return }
                self.mirrorRunning = false
                self.mirrorProcess = nil
                if !self.mirrorStopping && proc.terminationStatus != 0 {
                    self.mirrorError = Self.describeMirrorFailure(status: proc.terminationStatus, stderr: stderr)
                }
                self.mirrorStopping = false
            }
        }
        do {
            try process.run()
            mirrorProcess = process
            mirrorRunning = true
        } catch {
            mirrorRunning = false
            mirrorError = "Could not launch the server: \(error.localizedDescription)"
        }
    }

    func stopMirror() {
        mirrorStopping = true
        mirrorProcess?.terminate()
        mirrorProcess = nil
        mirrorRunning = false
    }

    private static func describeMirrorFailure(status: Int32, stderr: String) -> String {
        if stderr.contains("Address already in use") {
            return "Port is already in use — a mirror server is still running (often left over after quitting without stopping it). Wait a moment and try again, or free the port, then start again."
        }
        let tail = String(stderr.trimmingCharacters(in: .whitespacesAndNewlines).suffix(300))
        return tail.isEmpty ? "The server exited unexpectedly (code \(status))." : "The server stopped:\n\(tail)"
    }

    private func recordAutomationToMirror(name: String, body: String) {
        appendMirrorActivity(kind: "automation", title: name, body: body)
        pushNtfy(title: "AVIS · \(name)", body: body)
    }

    private func appendMirrorActivity(kind: String, title: String, body: String) {
        let event: [String: Any] = ["kind": kind, "title": title, "body": body, "time": Date().timeIntervalSince1970]
        guard var line = try? JSONSerialization.data(withJSONObject: event) else { return }
        line.append(0x0A)
        let url = URL(fileURLWithPath: mirrorActivityPath)
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(line)
            try? handle.close()
        } else {
            try? line.write(to: url, options: .atomic)
        }
    }

    private func pushNtfy(title: String, body: String) {
        let topic = mirrorNtfyTopic.trimmingCharacters(in: .whitespaces)
        guard !topic.isEmpty else { return }
        let server = mirrorNtfyServer.hasSuffix("/") ? String(mirrorNtfyServer.dropLast()) : mirrorNtfyServer
        DispatchQueue.global(qos: .utility).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
            process.arguments = ["-s", "-H", "Title: \(title)", "-H", "Tags: robot",
                                 "-d", body.isEmpty ? " " : body, "\(server)/\(topic)"]
            try? process.run()
        }
    }

    func sendMirrorTestNotification() {
        appendMirrorActivity(kind: "test", title: "Test", body: "AVIS mirror is connected.")
        pushNtfy(title: "AVIS test", body: "If you see this on your iPhone, notifications work.")
    }

    private func mirrorLanIP() -> String {
        var address = "127.0.0.1"
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return address }
        var pointer: UnsafeMutablePointer<ifaddrs>? = first
        while let current = pointer {
            let interface = current.pointee
            if let sockaddr = interface.ifa_addr, sockaddr.pointee.sa_family == UInt8(AF_INET) {
                let name = String(cString: interface.ifa_name)
                if name == "en0" || name == "en1" {
                    var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                    getnameinfo(sockaddr, socklen_t(sockaddr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST)
                    let candidate = String(cString: host)
                    if !candidate.isEmpty { address = candidate }
                }
            }
            pointer = interface.ifa_next
        }
        freeifaddrs(ifaddr)
        return address
    }

    func mirrorQRImage() -> NSImage? {
        guard let data = mirrorPhoneURL.data(using: .utf8),
              let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }

    // MARK: Bridge

    private func runBridge(request: [String: Any],
                           onToken: @escaping (String) -> Void,
                           onFinal: @escaping (String) -> Void,
                           onDone: @escaping () -> Void) {
        guard let data = try? JSONSerialization.data(withJSONObject: request),
              let json = String(data: data, encoding: .utf8) else {
            onFinal("Could not encode the request.")
            onDone()
            return
        }
        BridgeRunner.run(projectRoot: projectRoot, requestJSON: json) { event in
            Task { @MainActor in
                switch event {
                case .token(let text): onToken(text)
                case .final(let text): onFinal(text)
                case .error(let text): onFinal(text)
                case .done: onDone()
                }
            }
        }
    }

    // MARK: Persistence

    private func loadChats() {
        guard let data = UserDefaults.standard.data(forKey: chatsKey), let value = try? JSONDecoder().decode([SavedChat].self, from: data) else { return }
        savedChats = value
    }

    private func loadCustomTools() {
        guard let data = UserDefaults.standard.data(forKey: customToolsKey), let value = try? JSONDecoder().decode([CustomTool].self, from: data) else { return }
        customTools = value
    }

    private func loadAutomations() {
        guard let data = UserDefaults.standard.data(forKey: automationsKey), let value = try? JSONDecoder().decode([Automation].self, from: data) else { return }
        automations = value
    }

    private func loadContext() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: contextPath)), let value = try? JSONDecoder().decode(ContextProfile.self, from: data) else { return }
        contextUser = value.user
        contextAssistant = value.assistant
        contextProjects = value.workingOn
        contextHelpsWith = value.helpsWith.joined(separator: "\n")
    }

    private func persistChats() {
        UserDefaults.standard.set(try? JSONEncoder().encode(savedChats), forKey: chatsKey)
    }

    private func persistCustomTools() {
        UserDefaults.standard.set(try? JSONEncoder().encode(customTools), forKey: customToolsKey)
    }

    private func persistAutomations() {
        UserDefaults.standard.set(try? JSONEncoder().encode(automations), forKey: automationsKey)
    }

    private func saveCurrentChat() {
        guard messages.contains(where: { $0.role == .user }) else { return }
        let title = String((messages.first { $0.role == .user }?.text ?? "New chat").prefix(42))
        if let index = savedChats.firstIndex(where: { $0.id == currentChatID }) {
            savedChats[index].messages = messages
            savedChats[index].grants = grants
            savedChats[index].grantAll = grantAll
            savedChats[index].updated = Date()
            if savedChats[index].title.isEmpty { savedChats[index].title = title }
        } else {
            let chat = SavedChat(id: currentChatID, title: title, messages: messages, grants: grants, grantAll: grantAll)
            savedChats.insert(chat, at: 0)
        }
        savedChats.sort { $0.updated > $1.updated }
        savedChats = Array(savedChats.prefix(50))
        persistChats()
    }
}

// MARK: - Bridge runner (streams JSON-lines from the Python backend)

enum BridgeEvent {
    case token(String)
    case final(String)
    case error(String)
    case done
}

enum BridgeRunner {
    static func run(projectRoot: String, requestJSON: String, emit: @escaping (BridgeEvent) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let candidates = [
                projectRoot + "/.venv/bin/python",
                "/opt/homebrew/bin/python3",
                "/usr/bin/python3",
            ]
            guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
                emit(.error("No usable Python interpreter was found."))
                emit(.done)
                return
            }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-m", "main.bridge", requestJSON]
            process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONPATH"] = projectRoot
            environment["PYTHONUNBUFFERED"] = "1"
            process.environment = environment

            let outPipe = Pipe()
            let errPipe = Pipe()
            process.standardOutput = outPipe
            process.standardError = errPipe

            var buffer = Data()
            let handle = outPipe.fileHandleForReading
            handle.readabilityHandler = { fileHandle in
                let chunk = fileHandle.availableData
                guard !chunk.isEmpty else { return }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let lineData = buffer.subdata(in: buffer.startIndex..<newline)
                    buffer.removeSubrange(buffer.startIndex...newline)
                    handleLine(lineData, emit: emit)
                }
            }

            do {
                try process.run()
            } catch {
                emit(.error("Unable to start the AVIS backend: \(error.localizedDescription)"))
                emit(.done)
                return
            }
            process.waitUntilExit()
            handle.readabilityHandler = nil
            if !buffer.isEmpty { handleLine(buffer, emit: emit) }
            if process.terminationStatus != 0 {
                let errText = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                let trimmed = errText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { emit(.error("Backend error: \(trimmed.suffix(400))")) }
            }
            emit(.done)
        }
    }

    private static func handleLine(_ lineData: Data, emit: (BridgeEvent) -> Void) {
        guard !lineData.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
              let kind = object["t"] as? String else { return }
        let text = object["x"] as? String ?? ""
        switch kind {
        case "tok": emit(.token(text))
        case "final": emit(.final(text))
        case "err": emit(.error(text))
        case "done": break
        default: break
        }
    }
}

// MARK: - Voice bridge (mic capture + speech playback)

enum VoiceEvent {
    case listening
    case text(String)
    case silence
    case spoke
    case error(String)
}

/// A long-lived `python -m voice.bridge` subprocess. Unlike the one-shot chat
/// bridge, the app keeps the handle so it can stop a recording (write to stdin),
/// hand text over to be spoken, or interrupt playback (terminate).
final class VoiceProcess {
    private let process = Process()
    private let inPipe = Pipe()
    private let outPipe = Pipe()
    private var buffer = Data()

    func start(projectRoot: String,
               arguments: [String],
               onEvent: @escaping (VoiceEvent) -> Void,
               onExit: @escaping () -> Void) {
        let candidates = [
            projectRoot + "/.venv/bin/python",
            "/opt/homebrew/bin/python3",
            "/usr/bin/python3",
        ]
        guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            onEvent(.error("No usable Python interpreter was found."))
            onExit()
            return
        }
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "voice.bridge"] + arguments
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = projectRoot
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardInput = inPipe
        process.standardOutput = outPipe
        process.standardError = Pipe()   // swallow stderr; errors arrive as JSON lines

        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty, let self else { return }
            self.buffer.append(chunk)
            while let newline = self.buffer.firstIndex(of: 0x0A) {
                let lineData = self.buffer.subdata(in: self.buffer.startIndex..<newline)
                self.buffer.removeSubrange(self.buffer.startIndex...newline)
                if let event = Self.parse(lineData) {
                    DispatchQueue.main.async { onEvent(event) }
                }
            }
        }
        process.terminationHandler = { [weak self] _ in
            self?.outPipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async { onExit() }
        }
        do {
            try process.run()
        } catch {
            onEvent(.error("Unable to start the voice bridge: \(error.localizedDescription)"))
            onExit()
        }
    }

    /// Feed text to the subprocess (the reply to speak). Pair with `closeInput()`.
    func write(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        try? inPipe.fileHandleForWriting.write(contentsOf: data)
    }

    func closeInput() {
        try? inPipe.fileHandleForWriting.close()
    }

    /// Finalize a push-to-talk recording: any byte tells the recorder to stop.
    func stopRecording() {
        try? inPipe.fileHandleForWriting.write(contentsOf: Data([0x0A]))
        try? inPipe.fileHandleForWriting.close()
    }

    func terminate() {
        if process.isRunning { process.terminate() }
    }

    private static func parse(_ lineData: Data) -> VoiceEvent? {
        guard !lineData.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
              let kind = object["t"] as? String else { return nil }
        let text = object["x"] as? String ?? ""
        switch kind {
        case "listening": return .listening
        case "text": return .text(text)
        case "silence": return .silence
        case "spoke": return .spoke
        case "error": return .error(text)
        default: return nil
        }
    }
}

// MARK: - Notifications

/// A `swift run` executable has no bundle identifier, so UNUserNotificationCenter
/// is unavailable. `osascript` posts a real Notification Center banner from any process.
enum Notifier {
    static func show(title: String, body: String) {
        DispatchQueue.global(qos: .utility).async {
            func escape(_ value: String) -> String {
                value.replacingOccurrences(of: "\\", with: "\\\\")
                     .replacingOccurrences(of: "\"", with: "\\\"")
                     .replacingOccurrences(of: "\n", with: " ")
            }
            let script = "display notification \"\(escape(body))\" with title \"\(escape(title))\""
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = ["-e", script]
            try? process.run()
        }
    }
}

// MARK: - Encoding helpers

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

// MARK: - Root views

struct ContentView: View {
    var body: some View {
        HStack(spacing: 0) {
            SidebarView()
            MainPanel()
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }
}

struct SidebarView: View {
    @EnvironmentObject private var model: AvisViewModel
    @State private var renamingChat: SavedChat?
    @State private var renameText = ""

    private func icon(for page: SidebarPage) -> String {
        switch page {
        case .chat: return "bubble.left.fill"
        case .context: return "text.badge.checkmark"
        case .tools: return "wrench.and.screwdriver.fill"
        case .automation: return "clock.arrow.circlepath"
        case .mirror: return "iphone.gen3"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: "sparkles").font(.title2).foregroundStyle(.blue)
                Text("AVIS").font(.system(size: 25, weight: .semibold, design: .rounded))
            }
            .padding(.horizontal, 18)
            .padding(.top, 18)

            Button { model.newChat() } label: {
                Label("New chat", systemImage: "square.and.pencil")
                    .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                    .padding(.horizontal, 14)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .background(Color.accentColor.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .padding(.horizontal, 10)

            ForEach(SidebarPage.allCases, id: \.self) { page in
                Button { model.page = page } label: {
                    Label(page.rawValue, systemImage: icon(for: page))
                        .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                        .padding(.horizontal, 14)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(model.page == page ? Color.accentColor.opacity(0.18) : .clear)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .padding(.horizontal, 10)
            }

            Divider().padding(.horizontal, 16).padding(.vertical, 6)
            Text("Previous chats")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 18)

            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    if model.savedChats.isEmpty {
                        Text("Your conversations will appear here.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                            .padding(.horizontal, 18)
                    }
                    ForEach(model.savedChats) { chat in
                        Button { model.openChat(chat) } label: {
                            Text(chat.title)
                                .lineLimit(2)
                                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                                .padding(.horizontal, 14)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 10)
                        .contextMenu {
                            Button("Rename") {
                                renameText = chat.title
                                renamingChat = chat
                            }
                            Button("Delete", role: .destructive) { model.deleteChat(chat) }
                        }
                    }
                }
            }
        }
        .frame(width: 230)
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(item: $renamingChat) { chat in
            RenameSheet(title: $renameText) { newTitle in
                model.renameChat(chat, to: newTitle)
                renamingChat = nil
            } onCancel: {
                renamingChat = nil
            }
        }
    }
}

struct RenameSheet: View {
    @Binding var title: String
    let onSave: (String) -> Void
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename chat").font(.headline)
            TextField("Chat name", text: $title)
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .onSubmit { onSave(title) }
            HStack {
                Spacer()
                Button("Cancel", action: onCancel)
                Button("Save") { onSave(title) }.buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
    }
}

struct MainPanel: View {
    @EnvironmentObject private var model: AvisViewModel

    var body: some View {
        Group {
            switch model.page {
            case .chat: ChatPanelView()
            case .context: ContextPanelView()
            case .tools: ToolsPanelView()
            case .automation: AutomationPanelView()
            case .mirror: MirrorPanelView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Chat

struct ChatPanelView: View {
    @EnvironmentObject private var model: AvisViewModel

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("AVIS").font(.system(size: 20, weight: .semibold, design: .rounded))
                    Text("Local assistant").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 18)

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(model.messages) { message in
                            ChatGPTMessageView(message: message, isSending: model.isSending).id(message.id)
                        }
                    }
                    .frame(maxWidth: 780)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 28)
                    .padding(.vertical, 24)
                }
                .onChange(of: model.messages.last?.text) { _, _ in
                    if let last = model.messages.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
                }
            }

            VStack(spacing: 8) {
                if model.conversationMode || model.voiceActivity != .idle || model.voiceError != nil {
                    VoiceStatusBar()
                }
                HStack(alignment: .bottom, spacing: 8) {
                    TextField("Ask AVIS anything...", text: $model.prompt, axis: .vertical)
                        .textFieldStyle(.plain)
                        .font(.system(size: 15))
                        .lineLimit(1...6)
                        .padding(.horizontal, 8)
                        .frame(minHeight: 38)
                        .onSubmit(model.handleSend)
                        .disabled(model.conversationMode)

                    // Conversation mode: hands-free back-and-forth.
                    Button { model.toggleConversationMode() } label: {
                        Image(systemName: "waveform")
                            .frame(width: 38, height: 38)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(model.conversationMode ? Color.white : Color.secondary)
                    .background(model.conversationMode ? Color.accentColor : Color.clear)
                    .clipShape(Circle())
                    .disabled(model.isSending && !model.conversationMode)
                    .help("Conversation mode — talk back and forth with AVIS, hands-free")

                    // Microphone: push-to-talk. Speak, then press send.
                    Button { model.toggleMicrophone() } label: {
                        Image(systemName: model.isRecording ? "mic.fill" : "mic")
                            .frame(width: 38, height: 38)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(model.isRecording ? Color.white : Color.secondary)
                    .background(model.isRecording ? Color.red : Color.clear)
                    .clipShape(Circle())
                    .disabled(model.conversationMode || model.isSending)
                    .help("Speak, then press send to transcribe and send")

                    Button { model.handleSend() } label: {
                        if model.isSending { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up") }
                    }
                    .buttonStyle(.borderedProminent)
                    .frame(width: 38, height: 38)
                    .disabled(model.isSending || model.conversationMode)
                }
            }
            .padding(6)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color.primary.opacity(0.14)))
            .frame(maxWidth: 780)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 28)
            .padding(.bottom, 20)
        }
    }
}

struct ChatGPTMessageView: View {
    let message: ChatMessage
    let isSending: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if message.role == .assistant {
                Image(systemName: "sparkles")
                    .foregroundStyle(.blue)
                    .frame(width: 24, height: 24)
                    .background(Color.blue.opacity(0.1))
                    .clipShape(Circle())
            } else {
                Spacer(minLength: 40)
            }
            Group {
                if message.role == .assistant && message.text.isEmpty && isSending {
                    Text("Thinking…").foregroundStyle(.secondary).italic()
                } else {
                    Text(message.text)
                        .textSelection(.enabled)
                        .font(.system(size: 15))
                        .lineSpacing(3)
                }
            }
            .frame(maxWidth: .infinity, alignment: message.role == .user ? .trailing : .leading)
            if message.role == .user { Spacer(minLength: 40) }
        }
        .padding(.vertical, 13)
    }
}

/// Thin strip above the composer showing what voice is doing, with controls to
/// stop speaking or leave conversation mode.
struct VoiceStatusBar: View {
    @EnvironmentObject private var model: AvisViewModel

    private var icon: String {
        if model.voiceError != nil { return "exclamationmark.triangle.fill" }
        switch model.voiceActivity {
        case .listening: return "waveform"
        case .transcribing: return "hourglass"
        case .speaking: return "speaker.wave.2.fill"
        case .idle: return "waveform"
        }
    }

    private var label: String {
        if let error = model.voiceError { return error }
        switch model.voiceActivity {
        case .listening:
            return model.conversationMode ? "Listening… just start talking" : "Listening… press send when you're done"
        case .transcribing:
            return "Transcribing…"
        case .speaking:
            return "AVIS is speaking…"
        case .idle:
            return model.conversationMode ? "Conversation mode on" : ""
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(model.voiceError != nil ? .orange : .blue)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer()
            if model.voiceActivity == .speaking {
                Button("Stop") { model.stopSpeaking() }.controlSize(.small)
            }
            if model.conversationMode {
                Button("End conversation") { model.exitConversationMode() }.controlSize(.small)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.primary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

// MARK: - Context

struct ContextPanelView: View {
    @EnvironmentObject private var model: AvisViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Context").font(.system(size: 24, weight: .semibold))
                Text("Shape the information AVIS keeps in mind across conversations.").foregroundStyle(.secondary)
                ContextSection(title: "About you", subtitle: "A short description helps AVIS tailor its responses.") {
                    ContextField(title: "Your name or preference", text: $model.contextUser, placeholder: "Optional")
                }
                ContextSection(title: "About AVIS", subtitle: "Describe the assistant's role in your workspace.") {
                    ContextField(title: "Assistant description", text: $model.contextAssistant, placeholder: "What is AVIS?")
                }
                ContextSection(title: "What you are working on", subtitle: "Add each project on its own line.") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(model.contextProjects.indices, id: \.self) { index in
                            HStack(spacing: 8) {
                                TextField("Project \(index + 1)", text: $model.contextProjects[index])
                                    .textFieldStyle(.roundedBorder)
                                Button {
                                    model.removeProject(at: index)
                                } label: {
                                    Image(systemName: "minus.circle.fill").foregroundStyle(.secondary)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        Button {
                            model.addProject()
                        } label: {
                            Label("Add project", systemImage: "plus.circle.fill")
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.blue)
                        .padding(.top, 2)
                    }
                }
                ContextSection(title: "AVIS helps with", subtitle: "Put one capability on each line.") {
                    TextEditor(text: $model.contextHelpsWith)
                        .frame(minHeight: 110)
                        .padding(8)
                        .background(Color(nsColor: .textBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                HStack {
                    Image(systemName: "lock.fill").foregroundStyle(.secondary)
                    Text("This context stays local in your AVIS project.").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if model.contextSaved {
                        Label("Saved", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.callout)
                    }
                    Button("Save context") { model.saveContext() }.buttonStyle(.borderedProminent)
                }
            }
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(28)
        }
    }
}

struct ContextSection<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.headline)
            Text(subtitle).font(.caption).foregroundStyle(.secondary)
            content
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

struct ContextField: View {
    let title: String
    @Binding var text: String
    let placeholder: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            TextField(placeholder, text: $text).textFieldStyle(.roundedBorder)
        }
    }
}

// MARK: - Tools

struct ToolsPanelView: View {
    @EnvironmentObject private var model: AvisViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Tools").font(.system(size: 24, weight: .semibold))
                Text("Create reusable instructions and use them from the chat.").foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 14) {
                    Label(model.editingToolID == nil ? "Create a tool" : "Edit tool", systemImage: "wand.and.stars").font(.headline)
                    TextField("Tool name", text: $model.newToolName).textFieldStyle(.roundedBorder)
                    TextEditor(text: $model.newToolInstruction)
                        .frame(minHeight: 105)
                        .scrollContentBackground(.hidden)
                        .padding(8)
                        .background(Color(nsColor: .textBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay(alignment: .topLeading) {
                            if model.newToolInstruction.isEmpty {
                                Text("What should AVIS do when this tool is used?")
                                    .foregroundStyle(.secondary)
                                    .padding(.top, 13)
                                    .padding(.leading, 13)
                                    .allowsHitTesting(false)
                            }
                        }
                    HStack {
                        Text("Saved tools are handed to chat for execution.").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        if model.editingToolID != nil {
                            Button("Cancel") { model.cancelEditingTool() }
                        }
                        Button(model.editingToolID == nil ? "Save tool" : "Update tool") { model.saveTool() }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.newToolName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.newToolInstruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
                .padding(20)
                .background(Color(nsColor: .windowBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                ToolGroup(title: "Your tools") {
                    if model.customTools.isEmpty {
                        Text("Your saved tools will appear here.").foregroundStyle(.secondary)
                    } else {
                        ForEach(model.customTools) { tool in
                            EditableToolRow(tool: tool)
                        }
                    }
                }
                ToolGroup(title: "Built-in tools") {
                    Text("Select one to continue in Chat. AVIS runs it there and shows the result in the conversation.")
                        .font(.caption).foregroundStyle(.secondary)
                    ForEach(model.builtInTools, id: \.self) { tool in
                        ToolRow(title: tool.replacingOccurrences(of: "_", with: " ").capitalized, detail: tool) { model.useBuiltInTool(tool) }
                    }
                }
            }
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(28)
        }
    }
}

struct ToolGroup<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content
        }
    }
}

struct EditableToolRow: View {
    @EnvironmentObject private var model: AvisViewModel
    let tool: CustomTool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "bolt.circle.fill").font(.title3).foregroundStyle(.blue)
            VStack(alignment: .leading, spacing: 3) {
                Text(tool.name).font(.body.weight(.medium))
                Text(tool.instruction).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
            Button { model.useCustomTool(tool) } label: { Image(systemName: "arrow.up.right") }
                .buttonStyle(.plain).foregroundStyle(.secondary).help("Use in chat")
            Button { model.startEditingTool(tool) } label: { Image(systemName: "pencil") }
                .buttonStyle(.plain).foregroundStyle(.secondary).help("Edit")
            Button { model.deleteTool(tool) } label: { Image(systemName: "trash") }
                .buttonStyle(.plain).foregroundStyle(.secondary).help("Delete")
        }
        .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
        .padding(.horizontal, 14)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

struct ToolRow: View {
    let title: String
    let detail: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: "bolt.circle.fill").font(.title3).foregroundStyle(.blue)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.body.weight(.medium))
                    Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                Image(systemName: "arrow.up.right").foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
            .padding(.horizontal, 14)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

// MARK: - Automation

struct AutomationPanelView: View {
    @EnvironmentObject private var model: AvisViewModel

    @State private var editingID: UUID?
    @State private var name = ""
    @State private var targetKind: AutomationTargetKind = .builtIn
    @State private var builtInTarget = "get_battery_status"
    @State private var customInstruction = ""
    @State private var scheduleKind: ScheduleKind = .interval
    @State private var intervalMinutes = 60
    @State private var hour = 9
    @State private var minute = 0

    private var canSave: Bool {
        guard !name.trimmingCharacters(in: .whitespaces).isEmpty else { return false }
        return targetKind == .builtIn ? true : !customInstruction.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private func resetForm() {
        editingID = nil
        name = ""; customInstruction = ""
        targetKind = .builtIn; builtInTarget = "get_battery_status"
        scheduleKind = .interval; intervalMinutes = 60; hour = 9; minute = 0
    }

    private func beginEdit(_ automation: Automation) {
        editingID = automation.id
        name = automation.name
        targetKind = automation.targetKind
        if automation.targetKind == .builtIn {
            builtInTarget = automation.target
            customInstruction = ""
        } else {
            customInstruction = automation.target
        }
        scheduleKind = automation.scheduleKind
        intervalMinutes = automation.intervalMinutes
        hour = automation.hour
        minute = automation.minute
    }

    private func commit() {
        let trimmedName = name.trimmingCharacters(in: .whitespaces)
        let target = targetKind == .builtIn ? builtInTarget : customInstruction.trimmingCharacters(in: .whitespaces)
        if let id = editingID, var existing = model.automations.first(where: { $0.id == id }) {
            existing.name = trimmedName
            existing.targetKind = targetKind
            existing.target = target
            existing.scheduleKind = scheduleKind
            existing.intervalMinutes = intervalMinutes
            existing.hour = hour
            existing.minute = minute
            model.updateAutomation(existing)
        } else {
            model.addAutomation(name: trimmedName, targetKind: targetKind, target: target,
                                scheduleKind: scheduleKind, intervalMinutes: intervalMinutes,
                                hour: hour, minute: minute)
        }
        resetForm()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Automation").font(.system(size: 24, weight: .semibold))
                Text("Pick a tool and a schedule. AVIS runs it automatically while the app is open.")
                    .foregroundStyle(.secondary)

                VStack(alignment: .leading, spacing: 14) {
                    Label(editingID == nil ? "New automation" : "Edit automation", systemImage: "clock.badge.checkmark").font(.headline)
                    TextField("Automation name", text: $name).textFieldStyle(.roundedBorder)

                    Picker("Runs", selection: $targetKind) {
                        Text("Built-in tool").tag(AutomationTargetKind.builtIn)
                        Text("Custom instruction").tag(AutomationTargetKind.custom)
                    }
                    .pickerStyle(.segmented)

                    if targetKind == .builtIn {
                        Picker("Tool", selection: $builtInTarget) {
                            ForEach(model.builtInTools, id: \.self) { tool in
                                Text(tool.replacingOccurrences(of: "_", with: " ").capitalized).tag(tool)
                            }
                        }
                    } else {
                        TextField("What should AVIS do?", text: $customInstruction).textFieldStyle(.roundedBorder)
                    }

                    Picker("Schedule", selection: $scheduleKind) {
                        ForEach(ScheduleKind.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)

                    if scheduleKind == .interval {
                        HStack {
                            Text("Every")
                            Stepper(value: $intervalMinutes, in: 1...1440, step: 5) {
                                Text("\(intervalMinutes) min")
                            }
                            .frame(width: 200)
                        }
                    } else {
                        HStack {
                            Text("At")
                            Picker("", selection: $hour) {
                                ForEach(0..<24, id: \.self) { Text(String(format: "%02d", $0)).tag($0) }
                            }.frame(width: 70).labelsHidden()
                            Text(":")
                            Picker("", selection: $minute) {
                                ForEach([0, 15, 30, 45], id: \.self) { Text(String(format: "%02d", $0)).tag($0) }
                            }.frame(width: 70).labelsHidden()
                        }
                    }

                    HStack {
                        Text("Automations run with permission pre-granted.").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        if editingID != nil {
                            Button("Cancel") { resetForm() }
                        }
                        Button(editingID == nil ? "Add automation" : "Update automation") { commit() }
                            .buttonStyle(.borderedProminent)
                            .disabled(!canSave)
                    }
                }
                .padding(20)
                .background(Color(nsColor: .windowBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                VStack(alignment: .leading, spacing: 10) {
                    Text("Your automations").font(.headline)
                    if model.automations.isEmpty {
                        Text("Scheduled automations will appear here.").foregroundStyle(.secondary)
                    } else {
                        ForEach(model.automations) { automation in
                            AutomationRow(automation: automation, onEdit: { beginEdit(automation) })
                        }
                    }
                }
            }
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(28)
        }
    }
}

struct AutomationRow: View {
    @EnvironmentObject private var model: AvisViewModel
    let automation: Automation
    let onEdit: () -> Void

    private var scheduleText: String {
        switch automation.scheduleKind {
        case .interval: return "Every \(automation.intervalMinutes) min"
        case .dailyAt: return String(format: "Daily at %02d:%02d", automation.hour, automation.minute)
        }
    }

    private var targetText: String {
        automation.targetKind == .builtIn
            ? automation.target.replacingOccurrences(of: "_", with: " ").capitalized
            : automation.target
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Image(systemName: "clock.arrow.circlepath").font(.title3).foregroundStyle(.blue)
                VStack(alignment: .leading, spacing: 3) {
                    Text(automation.name).font(.body.weight(.medium))
                    Text("\(targetText) · \(scheduleText)").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: Binding(
                    get: { automation.enabled },
                    set: { _ in model.toggleAutomation(automation) }
                )).labelsHidden().help("Enable")
                Button { model.runAutomationNow(automation) } label: { Image(systemName: "play.fill") }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Run now")
                Button { onEdit() } label: { Image(systemName: "pencil") }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Edit")
                Button { model.deleteAutomation(automation) } label: { Image(systemName: "trash") }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Delete")
            }
            if let last = automation.lastRun {
                Text("Last run \(last.formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption2).foregroundStyle(.tertiary)
                if !automation.lastResult.isEmpty {
                    Text(automation.lastResult)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(4)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(nsColor: .textBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .windowBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

// MARK: - Mirror (iPhone)

struct MirrorPanelView: View {
    @EnvironmentObject private var model: AvisViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("Mirror").font(.system(size: 24, weight: .semibold))
                Text("View and drive AVIS from your iPhone on the same Wi-Fi. All computation and actions run here on the Mac.")
                    .foregroundStyle(.secondary)

                // Server control
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 10) {
                        Circle().fill(model.mirrorRunning ? Color.green : Color.secondary).frame(width: 10, height: 10)
                        Text(model.mirrorRunning ? "Server running" : "Server stopped").font(.headline)
                        Spacer()
                        if model.mirrorRunning {
                            Button("Stop") { model.stopMirror() }
                        } else {
                            Button("Start server") { model.startMirror() }.buttonStyle(.borderedProminent)
                        }
                    }

                    if let error = model.mirrorError {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                            Text(error).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.orange.opacity(0.12))
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }

                    if model.mirrorRunning {
                        HStack(alignment: .top, spacing: 20) {
                            if let qr = model.mirrorQRImage() {
                                VStack(spacing: 6) {
                                    Image(nsImage: qr)
                                        .interpolation(.none)
                                        .resizable()
                                        .frame(width: 160, height: 160)
                                        .background(Color.white)
                                        .clipShape(RoundedRectangle(cornerRadius: 10))
                                    Text("Scan with your iPhone camera").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            VStack(alignment: .leading, spacing: 10) {
                                LabeledCopy(label: "Phone URL", value: model.mirrorPhoneURL)
                                LabeledCopy(label: "Access token", value: model.mirrorToken)
                                Text("On the phone: scan the code, or open the URL and enter the token once. Then Share → Add to Home Screen for an app icon.")
                                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    } else {
                        Text("Start the server, then scan the QR code from your iPhone. Keep this Mac awake to stay connected.")
                            .font(.caption).foregroundStyle(.secondary)
                    }

                    Divider()
                    Toggle(isOn: Binding(
                        get: { model.mirrorAllowTools },
                        set: { model.mirrorAllowTools = $0; model.saveMirrorConfig() }
                    )) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Let the phone run actions").font(.callout.weight(.medium))
                            Text("When on, chats from the phone can execute tools (open apps, lock, volume, reminders…) without asking each time. When off, the phone is read-only unless you grant permission in the chat.")
                                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .toggleStyle(.switch)
                }
                .padding(20)
                .background(Color(nsColor: .windowBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                // Notifications
                VStack(alignment: .leading, spacing: 12) {
                    Label("iPhone notifications", systemImage: "bell.badge").font(.headline)
                    Text("Automation results are pushed to your iPhone through ntfy — a free app, no account needed. Install \"ntfy\" from the App Store, then subscribe to the topic below.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("ntfy topic (keep it private, like a password)").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        HStack {
                            TextField("e.g. avis-boris-9f2a", text: $model.mirrorNtfyTopic)
                                .textFieldStyle(.roundedBorder)
                            Button("Save") { model.saveMirrorConfig() }
                        }
                    }
                    HStack {
                        Text(model.mirrorNtfyTopic.trimmingCharacters(in: .whitespaces).isEmpty
                             ? "No topic set — phone push is off."
                             : "Subscribe in the ntfy app to: \(model.mirrorNtfyTopic)")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button("Send test") { model.sendMirrorTestNotification() }
                            .disabled(model.mirrorNtfyTopic.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                .padding(20)
                .background(Color(nsColor: .windowBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                HStack(spacing: 8) {
                    Image(systemName: "lock.fill").foregroundStyle(.secondary)
                    Text("The mirror is protected by the access token and stays on your local network. Anyone with the token can control this Mac, so keep it private.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: 780, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(28)
        }
    }
}

struct LabeledCopy: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            HStack {
                Text(value).font(.system(.callout, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(value, forType: .string)
                } label: { Image(systemName: "doc.on.doc") }
                .buttonStyle(.plain).foregroundStyle(.blue).help("Copy")
            }
            .padding(8)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }
}
