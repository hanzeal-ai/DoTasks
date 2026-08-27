import AppKit
import Foundation

private struct BookmarkStore: Codable {
    var bookmarks: [String: String] = [:]
}

private final class TaskboardHelperDelegate: NSObject, NSApplicationDelegate {
    private let fileManager = FileManager.default
    private let dataHome: URL
    private let requestHome: URL
    private let bookmarkStoreURL: URL
    private let runtimeHome: URL
    private var bookmarkStore = BookmarkStore()
    private var scopedURLs: [String: URL] = [:]
    private var serverProcess: Process?
    private var serverGeneration = 0
    private var terminating = false
    private var ready = false
    private var authorizationOpen = false
    private var queuedURLs: [URL] = []

    override init() {
        let home = fileManager.homeDirectoryForCurrentUser
        dataHome = home
            .appendingPathComponent("Library/Application Support/Codex Taskboard", isDirectory: true)
        requestHome = dataHome.appendingPathComponent("helper-requests", isDirectory: true)
        bookmarkStoreURL = dataHome.appendingPathComponent("authorized-projects.json")
        runtimeHome = Bundle.main.resourceURL!.appendingPathComponent("runtime", isDirectory: true)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            try fileManager.createDirectory(at: dataHome, withIntermediateDirectories: true)
            try fileManager.createDirectory(at: requestHome, withIntermediateDirectories: true)
            try restoreBookmarks()
        } catch {
            presentFatal("无法恢复项目授权：\(error.localizedDescription)")
            return
        }

        let bootstrap = Bundle.main.object(forInfoDictionaryKey: "TaskboardBootstrapProject") as? String
        if let path = bootstrap, !path.isEmpty, scopedURLs[path] == nil {
            requestAuthorization(requestID: nil, requiredPath: path)
        } else {
            startServer()
        }

        ready = true
        let pending = queuedURLs
        queuedURLs.removeAll()
        pending.forEach(handleURL)
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        if ready {
            urls.forEach(handleURL)
        } else {
            queuedURLs.append(contentsOf: urls)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        serverGeneration += 1
        serverProcess?.terminationHandler = nil
        if serverProcess?.isRunning == true {
            serverProcess?.terminate()
        }
        scopedURLs.values.forEach { $0.stopAccessingSecurityScopedResource() }
    }

    private func handleURL(_ url: URL) {
        guard url.scheme == "codex-taskboard-helper" else { return }
        switch url.host {
        case "authorize":
            let requestID = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "request" })?.value
            guard let requestID, isValidRequestID(requestID) else { return }
            requestAuthorization(requestID: requestID, requiredPath: nil)
        case "quit":
            NSApp.terminate(nil)
        default:
            break
        }
    }

    private func requestAuthorization(requestID: String?, requiredPath: String?) {
        guard !authorizationOpen else {
            if let requestID {
                writeResponse(["error": "另一个项目授权窗口正在处理中"], requestID: requestID)
            }
            return
        }
        authorizationOpen = true
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.title = requiredPath == nil ? "授权 Taskboard 项目" : "授权 Taskboard 运行项目"
        panel.message = requiredPath == nil
            ? "请选择要交给 Taskboard 创建、调度和验证任务的项目文件夹。"
            : "首次启动需要授权当前 Taskboard 项目文件夹。"
        panel.prompt = "授权此项目"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        if let requiredPath {
            panel.directoryURL = URL(fileURLWithPath: requiredPath).deletingLastPathComponent()
        }
        panel.begin { [weak self] response in
            guard let self else { return }
            self.authorizationOpen = false
            guard response == .OK, let selected = panel.url else {
                if let requestID {
                    self.writeResponse(["cancelled": true], requestID: requestID)
                } else {
                    NSApp.terminate(nil)
                }
                return
            }
            let path = selected.standardizedFileURL.path
            if let requiredPath,
               path != URL(fileURLWithPath: requiredPath).standardizedFileURL.path {
                self.presentMessage("请选择指定目录：\n\(requiredPath)")
                self.requestAuthorization(requestID: requestID, requiredPath: requiredPath)
                return
            }
            do {
                try self.persistAuthorization(selected.standardizedFileURL)
                if let requestID {
                    self.writeResponse(["project": path], requestID: requestID)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                        self.restartServer()
                    }
                } else {
                    self.startServer()
                }
            } catch {
                if let requestID {
                    self.writeResponse(["error": error.localizedDescription], requestID: requestID)
                } else {
                    self.presentFatal("无法保存项目授权：\(error.localizedDescription)")
                }
            }
        }
    }

    private func restoreBookmarks() throws {
        guard fileManager.fileExists(atPath: bookmarkStoreURL.path) else { return }
        let data = try Data(contentsOf: bookmarkStoreURL)
        bookmarkStore = try JSONDecoder().decode(BookmarkStore.self, from: data)
        for (path, encoded) in Array(bookmarkStore.bookmarks) {
            guard let bookmark = Data(base64Encoded: encoded) else { continue }
            var stale = false
            do {
                let url = try URL(
                    resolvingBookmarkData: bookmark,
                    options: [.withSecurityScope],
                    relativeTo: nil,
                    bookmarkDataIsStale: &stale
                )
                if url.startAccessingSecurityScopedResource() {
                    scopedURLs[path] = url
                }
                if stale {
                    try persistAuthorization(url)
                }
            } catch {
                bookmarkStore.bookmarks.removeValue(forKey: path)
            }
        }
        try saveBookmarkStore()
    }

    private func persistAuthorization(_ url: URL) throws {
        let path = url.standardizedFileURL.path
        let bookmark = try url.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        _ = url.startAccessingSecurityScopedResource()
        scopedURLs[path] = url
        bookmarkStore.bookmarks[path] = bookmark.base64EncodedString()
        try saveBookmarkStore()
    }

    private func saveBookmarkStore() throws {
        let data = try JSONEncoder().encode(bookmarkStore)
        try data.write(to: bookmarkStoreURL, options: .atomic)
        try fileManager.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: bookmarkStoreURL.path
        )
    }

    private func startServer() {
        guard serverProcess?.isRunning != true else { return }
        let startScript = runtimeHome.appendingPathComponent("scripts/start")
        guard fileManager.isExecutableFile(atPath: startScript.path) else {
            presentFatal("Taskboard 运行时不完整：\(startScript.path)")
            return
        }
        do {
            let logHome = dataHome.appendingPathComponent("logs", isDirectory: true)
            try fileManager.createDirectory(at: logHome, withIntermediateDirectories: true)
            let stdout = try appendHandle(logHome.appendingPathComponent("server.out.log"))
            let stderr = try appendHandle(logHome.appendingPathComponent("server.err.log"))
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/sh")
            process.arguments = [startScript.path]
            process.currentDirectoryURL = runtimeHome
            var environment = ProcessInfo.processInfo.environment
            environment["CODEX_TASKBOARD_HOME"] = dataHome.path
            environment["CODEX_TASKBOARD_HELPER_APP"] = Bundle.main.bundlePath
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            let toolDirectories = [
                "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                "/usr/sbin", "/sbin",
            ]
            environment["PATH"] = (toolDirectories + [environment["PATH"] ?? ""])
                .filter { !$0.isEmpty }
                .joined(separator: ":")
            process.environment = environment
            process.standardOutput = stdout
            process.standardError = stderr
            serverGeneration += 1
            let generation = serverGeneration
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self, !self.terminating, self.serverGeneration == generation else { return }
                    self.serverProcess = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                        self.startServer()
                    }
                }
            }
            try process.run()
            serverProcess = process
        } catch {
            presentFatal("无法启动 Taskboard 服务：\(error.localizedDescription)")
        }
    }

    private func restartServer() {
        serverGeneration += 1
        let process = serverProcess
        serverProcess = nil
        process?.terminationHandler = nil
        if process?.isRunning == true {
            process?.terminate()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            self.startServer()
        }
    }

    private func appendHandle(_ url: URL) throws -> FileHandle {
        if !fileManager.fileExists(atPath: url.path) {
            fileManager.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        return handle
    }

    private func writeResponse(_ payload: [String: Any], requestID: String) {
        guard JSONSerialization.isValidJSONObject(payload) else { return }
        do {
            let data = try JSONSerialization.data(withJSONObject: payload)
            let target = requestHome.appendingPathComponent("\(requestID).json")
            try data.write(to: target, options: .atomic)
        } catch {
            presentMessage("无法返回项目授权结果：\(error.localizedDescription)")
        }
    }

    private func isValidRequestID(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z0-9-]{1,80}$", options: .regularExpression) != nil
    }

    private func presentMessage(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Taskboard Helper"
        alert.informativeText = message
        alert.runModal()
    }

    private func presentFatal(_ message: String) {
        presentMessage(message)
        NSApp.terminate(nil)
    }
}

@main
private struct TaskboardHelperMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = TaskboardHelperDelegate()
        application.delegate = delegate
        application.run()
        _ = delegate
    }
}
