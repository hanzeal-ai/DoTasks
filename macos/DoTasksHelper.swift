import AppKit
import Foundation

private final class DoTasksHelperDelegate: NSObject, NSApplicationDelegate {
    private let fileManager = FileManager.default
    private let dataHome: URL
    private let runtimeHome: URL
    private var serverProcess: Process?
    private var agentProcess: Process?
    private var serverGeneration = 0
    private var agentGeneration = 0
    private var terminating = false

    override init() {
        let home = fileManager.homeDirectoryForCurrentUser
        let environment = ProcessInfo.processInfo.environment
        if let configured = environment["DOTASKS_HOME"],
           !configured.isEmpty {
            dataHome = URL(fileURLWithPath: configured, isDirectory: true)
        } else {
            dataHome = home.appendingPathComponent("Library/Application Support/DoTasks", isDirectory: true)
        }
        runtimeHome = Bundle.main.resourceURL!.appendingPathComponent("runtime", isDirectory: true)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            try fileManager.createDirectory(at: dataHome, withIntermediateDirectories: true)
        } catch {
            presentFatal("无法初始化 DoTasks 数据目录：\(error.localizedDescription)")
            return
        }
        startServer()
        startAgent()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        urls.forEach(handleURL)
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        serverGeneration += 1
        agentGeneration += 1
        serverProcess?.terminationHandler = nil
        agentProcess?.terminationHandler = nil
        if serverProcess?.isRunning == true {
            serverProcess?.terminate()
        }
        if agentProcess?.isRunning == true {
            agentProcess?.terminate()
        }
    }

    private func handleURL(_ url: URL) {
        guard url.scheme == "dotasks-helper" else { return }
        if url.host == "quit" {
            NSApp.terminate(nil)
        }
    }

    private func startServer() {
        guard serverProcess?.isRunning != true else { return }
        let startScript = runtimeHome.appendingPathComponent("scripts/start")
        guard fileManager.isExecutableFile(atPath: startScript.path) else {
            presentFatal("DoTasks 运行时不完整：\(startScript.path)")
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
            environment["DOTASKS_HOME"] = dataHome.path
            environment["DOTASKS_HELPER_APP"] = Bundle.main.bundlePath
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
            presentFatal("无法启动 DoTasks 服务：\(error.localizedDescription)")
        }
    }

    private func startAgent() {
        guard agentProcess?.isRunning != true else { return }
        let startScript = runtimeHome.appendingPathComponent("scripts/start-agent")
        guard fileManager.isExecutableFile(atPath: startScript.path) else {
            presentFatal("DoTasks 运行时不完整：\(startScript.path)")
            return
        }
        do {
            let logHome = dataHome.appendingPathComponent("logs", isDirectory: true)
            try fileManager.createDirectory(at: logHome, withIntermediateDirectories: true)
            let stdout = try appendHandle(logHome.appendingPathComponent("agent.out.log"))
            let stderr = try appendHandle(logHome.appendingPathComponent("agent.err.log"))
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/sh")
            process.arguments = [startScript.path, "--wait-for-config"]
            process.currentDirectoryURL = runtimeHome
            var environment = ProcessInfo.processInfo.environment
            environment["DOTASKS_HOME"] = dataHome.path
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
            agentGeneration += 1
            let generation = agentGeneration
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self, !self.terminating, self.agentGeneration == generation else { return }
                    self.agentProcess = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                        self.startAgent()
                    }
                }
            }
            try process.run()
            agentProcess = process
        } catch {
            presentFatal("无法启动 DoTasks 云端连接：\(error.localizedDescription)")
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

    private func presentMessage(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "DoTasks Helper"
        alert.informativeText = message
        alert.runModal()
    }

    private func presentFatal(_ message: String) {
        presentMessage(message)
        NSApp.terminate(nil)
    }
}

@main
private struct DoTasksHelperMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = DoTasksHelperDelegate()
        application.delegate = delegate
        application.run()
        _ = delegate
    }
}
