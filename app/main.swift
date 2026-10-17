// katet.app — the board in a native window, coordinator status in the menu bar, macOS
// notifications when something needs you. Built by app/build.sh; see app/README.md.
//
// - Opens a workspace like an editor opens a folder: the last one, one dropped on the icon or passed
//   to `open -a katet DIR`, or chosen in the Workspace menu; remembered in the app's preferences.
// - Uses a running board server on the workspace's port (katet.conf), or starts one (stopped again on quit).
// - Starts the coordinator if none runs, detached: it keeps running after the app quits.
// - Polls /api/tasks and /api/coordinator; notifies on agent replies, new questions or steps for
//   you, stuck tasks, a possible worker loop, and a coordinator.tools it cannot read.

import Cocoa
import UserNotifications
import WebKit

// baked in by app/build.sh: where the framework (board, coordinator) lives
let framework = (Bundle.main.object(forInfoDictionaryKey: "KatetFramework") as? String) ?? NSHomeDirectory() + "/src/katet"
var workspace = ""                      // the open workspace, chosen at runtime (see activate)
var repo: String { workspace }
let defaults = UserDefaults.standard
func isWorkspace(_ path: String) -> Bool { FileManager.default.fileExists(atPath: path + "/katet.conf") }

/// One value from the workspace's katet.conf (`key value` lines).
func confValue(_ key: String) -> String? {
    guard let text = try? String(contentsOfFile: workspace + "/katet.conf", encoding: .utf8) else { return nil }
    for line in text.split(separator: "\n") where line.hasPrefix(key + " ") {
        let v = line.dropFirst(key.count + 1).components(separatedBy: "  #")[0].trimmingCharacters(in: .whitespaces)
        return v.isEmpty ? nil : v
    }
    return nil
}
var port = 8765
var base = URL(string: "http://127.0.0.1:8765/")!

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var statusItem: NSStatusItem!
    var server: Process?            // the board server, if this app started it
    var userPATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    var stopRequested: Date? = nil
    var pendingFragment: String? = nil   // "#task=…" to open once the board of a freshly activated workspace is up
    var coord: [String: Any] = [:]
    var tasks: [[String: Any]] = []
    var previous: [String: TaskMark]? = nil   // nil until the first poll: no notifications for what already exists
    var prevStuck = Set<String>()
    var prevLoop = Set<String>()
    var prevConfigError: String? = nil
    var prevPaused: String? = nil
    var tasksETag: String? = nil
    var loadedOnce = false
    var ready = false                 // menus and window exist
    var pendingOpen: String? = nil    // a folder handed over before launch finished
    var workspaceMenu: NSMenu!
    var settingsWindow: NSWindow?
    var settingsStatus: NSTextField!
    var settingsLog: NSTextField!
    var settingsInfo: NSTextField!

    struct TaskMark { var reply: String; var questions: Int; var steps: Int }

    // MARK: launch

    var termSource: DispatchSourceSignal?

    func applicationDidFinishLaunching(_ note: Notification) {
        userPATH = loginShellPATH() ?? userPATH
        // a SIGTERM (the build script, kill) becomes a normal quit instead of an abrupt end: the window
        // frame is saved, the board gets its pagehide, the server we started is stopped
        signal(SIGTERM, SIG_IGN)
        termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        termSource?.setEventHandler { NSApp.terminate(nil) }
        termSource?.resume()
        buildMainMenu()
        buildWindow()
        buildStatusItem()
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, err in
            if let err = err { NSLog("katet: notifications unavailable: \(err.localizedDescription)") }
        }
        Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in self?.poll(nil) }
        ready = true
        let remembered = defaults.string(forKey: "workspace")
        if let w = pendingOpen ?? remembered, isWorkspace(w) { activate(w) }
        else {
            if let w = pendingOpen { fail("\(w) is not a katet workspace: no katet.conf there. Create one with `katet init DIR`, or pick another folder.") }
            chooseWorkspace()
        }
    }

    // MARK: workspace

    /// `open -a katet DIR`, or a folder dropped on the icon
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let dir = urls.first(where: { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false }) else { return }
        if ready { activate(dir.path) } else { pendingOpen = dir.path }
    }

    /// Point the app at a workspace: its own port, server, coordinator and board; remember it.
    func activate(_ path: String) {
        guard isWorkspace(path) else { fail("\(path) is not a katet workspace (no katet.conf). Create one with: katet init DIR"); return }
        if let s = server { s.interrupt(); server = nil }   // a board server we started for the previous workspace
        workspace = path
        port = confValue("port").flatMap(Int.init) ?? 8765
        base = URL(string: "http://127.0.0.1:\(port)/")!
        coord = [:]; tasks = []; previous = nil; prevStuck = []; prevLoop = []; prevConfigError = nil; tasksETag = nil; loadedOnce = false
        window.title = "katet — \((path as NSString).lastPathComponent)"
        rebuildWorkspaceMenu()
        web.load(URLRequest(url: URL(string: "about:blank")!))
        ensureServer { [weak self] in
            guard let self = self, workspace == path else { return }
            var recent = defaults.stringArray(forKey: "recentWorkspaces") ?? []   // remembered only once its board answered
            recent.removeAll { $0 == path }; recent.insert(path, at: 0)
            defaults.set(Array(recent.prefix(8)), forKey: "recentWorkspaces"); defaults.set(path, forKey: "workspace")
            self.rebuildWorkspaceMenu()
            let target = self.pendingFragment.map { URL(string: base.absoluteString + "#" + $0) ?? base } ?? base
            self.pendingFragment = nil
            self.web.load(URLRequest(url: target))
            self.loadedOnce = true
            self.poll { if !self.coordinatorAlive { self.startCoordinator() } }
        }
        updateStatusItem()
    }

    /// Leave a workspace that cannot be used (its port serves another one): nothing is polled, nothing
    /// remembered, the picker comes back.
    func disconnect(_ why: String) {
        workspace = ""; coord = [:]; tasks = []; previous = nil
        defaults.removeObject(forKey: "workspace")
        window.title = "katet"
        web.load(URLRequest(url: URL(string: "about:blank")!))
        updateStatusItem(); rebuildWorkspaceMenu()
        fail(why)
        chooseWorkspace()
    }

    @objc func chooseWorkspace() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
        panel.prompt = "Open"; panel.message = "Choose a katet workspace — a folder with katet.conf. An empty folder becomes one."
        panel.directoryURL = URL(fileURLWithPath: workspace.isEmpty ? NSHomeDirectory() : workspace)
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else {
            if workspace.isEmpty { NSApp.terminate(nil) }   // nothing to show without a workspace
            return
        }
        let path = url.path
        if isWorkspace(path) { activate(path); return }
        let a = NSAlert(); a.messageText = "Not a katet workspace yet"
        a.informativeText = "\(path) has no katet.conf. Create a workspace there? (katet init: tasks/, CLAUDE.md, katet.conf, coordinator.tools)"
        a.addButton(withTitle: "Create Workspace"); a.addButton(withTitle: "Choose Another"); a.addButton(withTitle: "Cancel")
        switch a.runModal() {
        case .alertFirstButtonReturn:
            let p = process([framework + "/bin/katet", "init", path], log: nil)
            do { try p.run() } catch { fail("Could not run katet init: \(error.localizedDescription)"); return }
            p.waitUntilExit()
            if isWorkspace(path) { activate(path) } else { fail("katet init did not create \(path)/katet.conf") }
        case .alertSecondButtonReturn: chooseWorkspace()
        default: if workspace.isEmpty { NSApp.terminate(nil) }
        }
    }

    @objc func switchWorkspace(_ sender: NSMenuItem) {
        if let path = sender.representedObject as? String, path != workspace { activate(path) }
    }

    @objc func revealWorkspace() {
        if !workspace.isEmpty { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: workspace) }
    }

    func rebuildWorkspaceMenu() {
        let m = workspaceMenu!; m.removeAllItems()
        let open = NSMenuItem(title: "Open Workspace…", action: #selector(chooseWorkspace), keyEquivalent: "o"); open.target = self
        m.addItem(open); m.addItem(.separator())
        for path in defaults.stringArray(forKey: "recentWorkspaces") ?? [] {
            let i = NSMenuItem(title: (path as NSString).lastPathComponent, action: #selector(switchWorkspace(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = path; i.toolTip = path; i.state = path == workspace ? .on : .off
            m.addItem(i)
        }
        m.addItem(.separator())
        let r = NSMenuItem(title: "Reveal in Finder", action: #selector(revealWorkspace), keyEquivalent: ""); r.target = self
        m.addItem(r)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow(nil); return true
    }

    func applicationWillTerminate(_ note: Notification) {
        server?.interrupt()   // our board server goes; the coordinator stays (it was started detached)
    }

    // MARK: processes

    /// PATH as the login shell sees it (Homebrew, mise, the agent CLI) — apps started from Finder get a bare one.
    func loginShellPATH() -> String? {
        let p = Process(), out = Pipe()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "printf %s \"$PATH\""]
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        p.waitUntilExit()
        let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return s.isEmpty ? nil : s
    }

    func process(_ args: [String], log: String?) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = args
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = userPATH
        env["KATET_WORKSPACE"] = workspace
        p.environment = env
        if let log = log {
            if !FileManager.default.fileExists(atPath: log) { FileManager.default.createFile(atPath: log, contents: nil) }   // append, keep history
            let h = FileHandle(forWritingAtPath: log)
            h?.seekToEndOfFile()
            p.standardOutput = h; p.standardError = h
        } else {
            p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        }
        p.standardInput = FileHandle.nullDevice
        return p
    }

    /// A path as the server reports it (resolved, no symlinks), for comparing workspaces.
    func canonical(_ path: String) -> String { URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().path }

    func ensureServer(_ then: @escaping () -> Void) {
        let ws = workspace
        get("api/tasks") { [weak self] data, _, _ in
            guard let self = self, ws == workspace else { return }
            if let d = data {
                // a board is already listening: it must be this workspace's, not another one's
                let served = ((try? JSONSerialization.jsonObject(with: d) as? [String: Any])?["repo"] as? String) ?? ""
                if served.isEmpty || self.canonical(served) == self.canonical(ws) { then() }
                else { self.disconnect("Port \(port) is already used by a board for another workspace:\n\(served)\n\nSet another port in \(ws)/katet.conf, then open the workspace again.") }
                return
            }
            try? FileManager.default.createDirectory(atPath: repo + "/.coordinator", withIntermediateDirectories: true)
            let p = self.process(["python3", framework + "/board/serve.py", "--no-open", "--port", "\(port)"], log: repo + "/.coordinator/board.out")
            do { try p.run(); self.server = p } catch { self.fail("Could not start the board server: \(error.localizedDescription)"); return }
            self.waitForServer(tries: 40, then)
        }
    }

    func waitForServer(tries: Int, _ then: @escaping () -> Void) {
        let ws = workspace
        get("api/coordinator") { [weak self] data, _, _ in
            guard ws == workspace else { return }   // switched away while waiting
            if data != nil { then(); return }
            if tries <= 0 { self?.fail("The board server did not come up — see .coordinator/board.out"); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self?.waitForServer(tries: tries - 1, then) }
        }
    }

    var coordinatorAlive: Bool { (coord["alive"] as? Bool) ?? false }

    @objc func startCoordinator() {
        guard !coordinatorAlive else { return }
        try? FileManager.default.createDirectory(atPath: repo + "/.coordinator", withIntermediateDirectories: true)
        // nohup: survives the app quitting (no SIGHUP); its own log next to the coordinator's state
        let p = process(["nohup", framework + "/bin/katet", "coordinator"], log: repo + "/.coordinator/coordinator.out")
        do { try p.run() } catch { fail("Could not start the coordinator: \(error.localizedDescription)") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.poll(nil) }
    }

    @objc func stopCoordinator() {
        guard let c = coord["coordinator"] as? [String: Any], let pid = c["pid"] as? Int else { return }
        if (c["state"] as? String) == "stopping" { return }   // a second SIGINT kills the worker: that is killAndStop, asked for explicitly
        if let t = stopRequested, Date().timeIntervalSince(t) < 10 { return }   // clicked twice before the state caught up
        stopRequested = Date()
        kill(pid_t(pid), SIGINT)   // like ctrl-c once: finishes a running worker first
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.poll(nil) }
    }

    @objc func killAndStop() {
        guard let c = coord["coordinator"] as? [String: Any], let pid = c["pid"] as? Int else { return }
        let a = NSAlert(); a.messageText = "Kill the running worker?"
        a.informativeText = "Its task may be left in tasks/in-progress/; the board then shows how to move it on."
        a.addButton(withTitle: "Kill and Stop"); a.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        kill(pid_t(pid), SIGINT)   // the second ctrl-c
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.poll(nil) }
    }

    func fail(_ msg: String) {
        let a = NSAlert(); a.messageText = "katet"; a.informativeText = msg; a.runModal()
    }

    // MARK: polling + notifications

    /// GET from the board server: `done` gets the body on 200 (nil otherwise), the status and the ETag.
    func get(_ path: String, etag: String? = nil, _ done: @escaping (Data?, Int, String?) -> Void) {
        var req = URLRequest(url: base.appendingPathComponent(path)); req.timeoutInterval = 2
        if let e = etag { req.setValue(e, forHTTPHeaderField: "If-None-Match") }
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let h = resp as? HTTPURLResponse, code = h?.statusCode ?? 0
            DispatchQueue.main.async { done(code == 200 ? data : nil, code, h?.value(forHTTPHeaderField: "ETag")) }
        }.resume()
    }

    func poll(_ then: (() -> Void)?) {
        if workspace.isEmpty { return }
        let ws = workspace   // answers for a workspace that was switched away meanwhile are dropped
        get("api/coordinator") { [weak self] data, _, _ in
            guard let self = self, ws == workspace else { return }
            if let d = data, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { self.coord = j } else { self.coord = [:] }
            // the task list travels only when it changed (ETag → 304); the coordinator state is small
            self.get("api/tasks", etag: self.tasksETag) { data, code, etag in
                guard ws == workspace else { return }
                if code == 304 { self.notifyChanges() }
                else if let d = data, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   let t = j["tasks"] as? [[String: Any]] { self.tasks = t; self.tasksETag = etag; self.notifyChanges() }
                self.updateStatusItem()
                then?()
            }
        }
    }

    func notifyChanges() {
        var now: [String: TaskMark] = [:]
        for t in tasks {
            guard let slug = t["slug"] as? String, (t["folder"] as? String) != "done" else { continue }
            let chat = (t["chat"] as? [[String: Any]]) ?? []
            let lastAgent = chat.last(where: { ($0["who"] as? String) == "agent" })
            let reply = lastAgent.map { "\($0["at"] ?? "")|\(($0["text"] as? String)?.count ?? 0)" } ?? ""
            let qs = ((t["questions"] as? [[String: Any]]) ?? []).filter {
                !(($0["done"] as? Bool) ?? false) && ((($0["answer"] as? String) ?? "").isEmpty) }
            let steps = ((t["nextSteps"] as? [[String: Any]]) ?? []).filter { !(($0["done"] as? Bool) ?? false) }
            now[slug] = TaskMark(reply: reply, questions: qs.count, steps: steps.count)
            guard let before = previous?[slug] else { continue }
            let title = (t["title"] as? String) ?? slug
            if !reply.isEmpty && reply != before.reply, let text = lastAgent?["text"] as? String {
                notify("Agent replied — \(title)", String(text.prefix(180)), slug: slug)
            }
            if qs.count > before.questions, let q = qs.last {
                notify("Question for you — \(title)", "\(q["id"] ?? ""): \(q["text"] ?? "")", slug: slug)
            } else if steps.count > before.steps {
                notify("New step for you — \(title)", stripIds((steps.last?["text"] as? String) ?? ""), slug: slug)
            }
        }
        let stuck = Set(((coord["stuck"] as? [String: Any]) ?? [:]).keys)
        let loop = Set(((coord["loop"] as? [String: Any]) ?? [:]).keys)
        let configError = (coord["coordinator"] as? [String: Any])?["configError"] as? String
        let paused = (coord["coordinator"] as? [String: Any])?["paused"] as? [String: Any]
        let pausedKey = paused.map { "\($0["until"] ?? "")" }
        if previous != nil {
            if let p = paused, pausedKey != prevPaused {
                let until = String(("\(p["until"] ?? "")").dropFirst(11).prefix(5))
                notify("Workers paused until \(until)", "The last run hit a limit: \(p["reason"] ?? ""). Queued runs continue after the pause.", slug: p["slug"] as? String)
            }
            for s in stuck.subtracting(prevStuck) { notify("Task stuck in in-progress", "\(s): a worker ended without moving it on", slug: s) }
            for s in loop.subtracting(prevLoop) { notify("Possible loop — task held", "\(s): runs keep starting from changes made while its worker ran. Check its Chat, then act on it to continue.", slug: s) }
            if let e = configError, e != prevConfigError { notify("Settings not read", e + " — the last good coordinator.tools stays in use", slug: nil) }
        }
        prevStuck = stuck; prevLoop = loop; prevConfigError = configError; prevPaused = pausedKey
        previous = now
    }

    func stripIds(_ s: String) -> String {
        s.replacingOccurrences(of: #"^\*\*[SA]\d+\*\*\s*"#, with: "", options: .regularExpression)
         .replacingOccurrences(of: "`", with: "").replacingOccurrences(of: "**", with: "")
    }

    func notify(_ title: String, _ body: String, slug: String?) {
        let c = UNMutableNotificationContent()
        c.title = title; c.body = body; c.sound = .default
        c.userInfo = ["workspace": workspace]
        if let slug = slug { c.userInfo["slug"] = slug }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)) { [weak self] err in
            guard let err = err else { return }
            NSLog("katet: notification not delivered: \(err.localizedDescription)")
            DispatchQueue.main.async { self?.settingsLog?.stringValue = "Last delivery failed: \(err.localizedDescription)" }
        }
    }

    // MARK: settings window — what macOS thinks about our notifications, and a test button

    @objc func showSettings() {
        if settingsWindow == nil { buildSettingsWindow() }
        refreshSettings()
        settingsWindow?.center(); settingsWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func buildSettingsWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 100), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "katet Settings"; w.isReleasedWhenClosed = false
        func label(_ s: String, bold: Bool = false, dim: Bool = false) -> NSTextField {
            let l = NSTextField(wrappingLabelWithString: s)
            if bold { l.font = .boldSystemFont(ofSize: 13) }
            if dim { l.textColor = .secondaryLabelColor; l.font = .systemFont(ofSize: 12) }
            l.preferredMaxLayoutWidth = 520
            return l
        }
        func button(_ t: String, _ a: Selector) -> NSButton { let b = NSButton(title: t, target: self, action: a); b.bezelStyle = .rounded; return b }
        settingsStatus = label("…")
        settingsLog = label(" ", dim: true)
        settingsInfo = label(" ", dim: true)
        let buttons = NSStackView(views: [button("Send Test Notification", #selector(testNotification)),
                                          button("Ask for Permission", #selector(askPermission)),
                                          button("Open System Settings", #selector(openNotificationSettings))])
        buttons.orientation = .horizontal; buttons.spacing = 8
        let stack = NSStackView(views: [label("Notifications", bold: true), settingsStatus, buttons, settingsLog,
                                        label("About this app", bold: true), settingsInfo])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 18, left: 20, bottom: 18, right: 20)
        stack.setCustomSpacing(18, after: settingsLog)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView(); w.contentView = content; content.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: content.leadingAnchor), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                                     stack.topAnchor.constraint(equalTo: content.topAnchor), stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
                                     content.widthAnchor.constraint(equalToConstant: 560)])
        settingsWindow = w
    }

    func refreshSettings() {
        settingsInfo?.stringValue = "Bundle id: \(Bundle.main.bundleIdentifier ?? "?")\nApp: \(Bundle.main.bundlePath)\nFramework: \(framework)\nWorkspace: \(workspace.isEmpty ? "none" : workspace)\nBoard: \(base.absoluteString)  ·  coordinator \(coordinatorAlive ? "running" : "not running")"
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] st in
            let auth: String
            switch st.authorizationStatus {
            case .authorized: auth = "allowed"
            case .denied: auth = "DENIED — turn katet on under System Settings › Notifications"
            case .notDetermined: auth = "not asked yet — click Ask for Permission"
            case .provisional: auth = "provisional (delivered quietly)"
            default: auth = "unknown"
            }
            let detail = "alerts \(st.alertSetting == .enabled ? "on" : "off") · sound \(st.soundSetting == .enabled ? "on" : "off") · badge \(st.badgeSetting == .enabled ? "on" : "off")"
            DispatchQueue.main.async { self?.settingsStatus?.stringValue = "Permission: \(auth)\n\(detail)" }
        }
    }

    @objc func testNotification() {
        let c = UNMutableNotificationContent()
        c.title = "katet test"; c.body = "Notifications reach you. If you read this on screen, they work."; c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)) { [weak self] err in
            DispatchQueue.main.async {
                let time = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
                self?.settingsLog?.stringValue = err.map { "Delivery failed: \($0.localizedDescription)" }
                    ?? "Test sent at \(time). Nothing on screen? Check a Focus mode (Do Not Disturb) and the alert style for katet in System Settings."
                self?.refreshSettings()
            }
        }
    }

    @objc func askPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] ok, err in
            DispatchQueue.main.async {
                self?.settingsLog?.stringValue = err.map { "Request failed: \($0.localizedDescription)" } ?? (ok ? "Permission granted." : "Permission not granted — macOS asks only once; use Open System Settings.")
                self?.refreshSettings()
            }
        }
    }

    @objc func openNotificationSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])   // also while the app is in front
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let slug = info["slug"] as? String, ws = info["workspace"] as? String
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if let ws = ws, ws != workspace, isWorkspace(ws) {   // it came from another workspace: go there first
                self.pendingFragment = slug.map { "task=\($0)" }   // applied once that board is loaded
                self.activate(ws)
            } else {
                self.open(fragment: slug.map { "task=\($0)" })
            }
        }
        done()
    }

    // MARK: menu bar

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(named: "MenuBarIdleTemplate")   // the k mark; "…Template" makes it adapt to the menu bar
        statusItem.button?.imagePosition = .imageLeading
        updateStatusItem()
    }

    func updateStatusItem() {
        let c = coord["coordinator"] as? [String: Any]
        let worker = coord["worker"] as? [String: Any]
        let alive = coordinatorAlive
        let stopping = (c?["state"] as? String) == "stopping"
        let configError = c?["configError"] as? String
        let paused = c?["paused"] as? [String: Any]
        let stuck = ((coord["stuck"] as? [String: Any]) ?? [:]).count
        let loop = ((coord["loop"] as? [String: Any]) ?? [:]).count
        let replies = tasks.filter { t in
            guard (t["folder"] as? String) != "done", let chat = t["chat"] as? [[String: Any]], let last = chat.last else { return false }
            return (last["who"] as? String) == "you"
        }.count

        let state: String
        if coord.isEmpty { state = "Board server not reachable" }
        else if !alive { state = "Coordinator stopped" }
        else if let w = worker { state = (stopping ? "Stopping after the worker on " : "Worker on ") + "\(w["slug"] ?? "?")" }
        else if stopping { state = "Coordinator stopping" }
        else if let p = paused { state = "Workers paused until \(String(("\(p["until"] ?? "")").dropFirst(11).prefix(5))) — limit hit" }
        else { state = (c?["mode"] as? String) == "dry" ? "Coordinator watching (dry mode)" : "Coordinator watching" }

        let attention = stuck + loop + (configError == nil ? 0 : 1) + (paused == nil ? 0 : 1)   // things that need you, shown as a number
        // the k mark; the dot version when something needs you; dimmed while no coordinator runs; … while a worker runs
        let img = NSImage(named: attention > 0 ? "MenuBarAttentionTemplate" : "MenuBarIdleTemplate")
        img?.accessibilityDescription = state
        statusItem.button?.image = img
        statusItem.button?.appearsDisabled = !alive
        statusItem.button?.title = attention > 0 ? " \(attention)" : worker != nil ? " …" : ""
        statusItem.button?.toolTip = "katet — \(state)"

        let m = NSMenu()
        m.addItem(withTitle: workspace.isEmpty ? "No workspace open" : "Workspace: " + (workspace as NSString).lastPathComponent, action: nil, keyEquivalent: "").isEnabled = false
        m.addItem(withTitle: state, action: nil, keyEquivalent: "").isEnabled = false
        if replies > 0 { m.addItem(withTitle: "\(replies) chat message\(replies == 1 ? "" : "s") waiting for an agent", action: nil, keyEquivalent: "").isEnabled = false }
        if stuck > 0 { m.addItem(withTitle: "\(stuck) task\(stuck == 1 ? "" : "s") stuck in in-progress", action: #selector(openCoordinator), keyEquivalent: "").target = self }
        if loop > 0 { m.addItem(withTitle: "\(loop) task\(loop == 1 ? "" : "s") held — possible loop", action: #selector(openCoordinator), keyEquivalent: "").target = self }
        if let e = configError { m.addItem(withTitle: "Settings not read: \(e.prefix(70))", action: #selector(openCoordinator), keyEquivalent: "").target = self }
        m.addItem(.separator())
        m.addItem(withTitle: "Open Board", action: #selector(showWindow(_:)), keyEquivalent: "").target = self
        m.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: "").target = self
        m.addItem(withTitle: "Your Turn", action: #selector(openInbox), keyEquivalent: "").target = self
        m.addItem(withTitle: "Coordinator", action: #selector(openCoordinator), keyEquivalent: "").target = self
        m.addItem(withTitle: "Worker Log", action: #selector(openWorker), keyEquivalent: "").target = self
        m.addItem(.separator())
        if alive && stopping && worker != nil {
            m.addItem(withTitle: "Stopping after this worker…", action: nil, keyEquivalent: "").isEnabled = false
            m.addItem(withTitle: "Kill the Worker and Stop Now", action: #selector(killAndStop), keyEquivalent: "").target = self
        } else if alive {
            m.addItem(withTitle: worker != nil ? "Stop Coordinator (after this worker)" : "Stop Coordinator", action: #selector(stopCoordinator), keyEquivalent: "").target = self
        } else {
            m.addItem(withTitle: "Start Coordinator", action: #selector(startCoordinator), keyEquivalent: "").target = self
        }
        m.addItem(.separator())
        m.addItem(withTitle: "Quit katet (coordinator keeps running)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = m
    }

    @objc func openBoard() { open(fragment: "") }
    @objc func openInbox() { open(fragment: "view=inbox") }
    @objc func openCoordinator() { open(fragment: "view=coord") }
    @objc func openWorker() { open(fragment: "view=worker") }

    func open(fragment: String?) {
        showWindow(nil)
        if let f = fragment {
            web.evaluateJavaScript("location.hash = \(String(reflecting: f))", completionHandler: nil)
        }
    }

    // MARK: window + web view

    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        // the board's copy buttons use navigator.clipboard; route them to the macOS pasteboard
        cfg.userContentController.add(self, name: "copy")
        cfg.userContentController.addUserScript(WKUserScript(source: """
            navigator.clipboard.writeText = t => { window.webkit.messageHandlers.copy.postMessage(String(t)); return Promise.resolve(); };
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "katet"
        window.titlebarAppearsTransparent = false
        window.contentView = web
        window.delegate = self
        window.setFrameAutosaveName("katet.main")
        if window.frame.origin == .zero { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func showWindow(_ sender: Any?) {
        if loadedOnce && web.url == nil { web.load(URLRequest(url: base)) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reload() { web.reload() }

    // closing the window only hides it: the app stays in the menu bar and keeps notifying
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "copy", let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    // board pages stay inside; web pages and the editor schemes open in their own apps. Anything else
    // a task file could name (file:, shortcuts:, x-apple-…) is not handed to the system.
    func openOutside(_ url: URL) {
        let allowed: Set<String> = ["http", "https", "mailto", "zed", "vscode", "cursor"]
        if let s = url.scheme?.lowercased(), allowed.contains(s) { NSWorkspace.shared.open(url) }
        else { NSLog("katet: link with scheme \(url.scheme ?? "?") not opened") }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.allow); return }
        if (url.host == "127.0.0.1" || url.host == "localhost") && url.port == port { decisionHandler(.allow); return }
        if url.scheme == "about" { decisionHandler(.allow); return }
        openOutside(url)
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { openOutside(url) }   // target=_blank links
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        // server restarting (it re-execs on code changes): try again shortly — for the open workspace only
        let ws = workspace
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { if !ws.isEmpty && ws == workspace { webView.load(URLRequest(url: base)) } }
    }

    // MARK: main menu — Edit is what makes copy/paste work in the board's text fields

    func buildMainMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            item.submenu = m; main.addItem(item)
        }
        func it(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; i.target = target; return i
        }
        sub("katet", [
            it("About katet", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            it("Settings…", #selector(showSettings), ",", target: self),
            .separator(),
            it("Hide katet", #selector(NSApplication.hide(_:)), "h"),
            it("Quit katet", #selector(NSApplication.terminate(_:)), "q"),
        ])
        sub("Edit", [
            it("Undo", Selector(("undo:")), "z"), it("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            it("Cut", #selector(NSText.cut(_:)), "x"), it("Copy", #selector(NSText.copy(_:)), "c"),
            it("Paste", #selector(NSText.paste(_:)), "v"), it("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        workspaceMenu = NSMenu(title: "Workspace")
        let wsItem = NSMenuItem(); wsItem.submenu = workspaceMenu; main.addItem(wsItem)
        rebuildWorkspaceMenu()
        sub("View", [
            it("Board", #selector(openBoard), "1", target: self),
            it("Your Turn", #selector(openInbox), "2", target: self),
            it("Coordinator", #selector(openCoordinator), "3", target: self),
            it("Worker Log", #selector(openWorker), "4", target: self),
            .separator(),
            it("Reload", #selector(reload), "r", target: self),
        ])
        sub("Window", [
            it("Close", #selector(NSWindow.performClose(_:)), "w"),
            it("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
