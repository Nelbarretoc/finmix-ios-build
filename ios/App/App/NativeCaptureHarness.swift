#if DEBUG && targetEnvironment(simulator)
import Foundation
import CryptoKit
import WebKit

// Never compiled into a device or Release binary. This is an authenticated
// screenshot driver, not a fixture API or a replacement for native sign-in tests.
@MainActor
final class NativeCaptureHarness: NSObject, URLSessionTaskDelegate {
    private static let sentinel = "FINMIX_DEBUG_CAPTURE_HARNESS_V1"
    private let origin = "https://finmix.th3rdgroup.com"
    private let directory: URL
    private weak var webView: WKWebView?
    private var timer: Timer?
    private var busy = false
    private var lastCommand = ""
    private var session: URLSession!
    private var expectedActor = ""
    private var expectedFixture = ""
    private var taxYear = 0

    init(webView: WKWebView) {
        self.webView = webView
        directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("FinMixCapture", isDirectory: true)
        super.init()
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = HTTPCookieStorage.shared
        config.httpShouldSetCookies = true
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 20
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask,
                               willPerformHTTPRedirection response: HTTPURLResponse,
                               newRequest request: URLRequest,
                               completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func start() {
        let credentials = directory.appendingPathComponent("private.json")
        guard FileManager.default.fileExists(atPath: credentials.path) else { return }
        Task {
            do {
                let bytes = try Data(contentsOf: credentials)
                // Delete before parsing, authentication or any failure report.
                try FileManager.default.removeItem(at: credentials)
                guard let input = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                      let reviewer = input["reviewer"] as? [String: Any],
                      let email = reviewer["email"] as? String,
                      let password = reviewer["password"] as? String,
                      let purpose = reviewer["purpose"] as? String,
                      purpose.lowercased().contains("fictional"), purpose.lowercased().contains("review"),
                      email.lowercased().hasSuffix(".invalid"), email.lowercased().contains("review"),
                      let actor = input["expectedActorId"] as? String,
                      let fixture = input["expectedFixtureSha256"] as? String,
                      fixture.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                      let year = input["taxYear"] as? Int, (2020...2100).contains(year) else { throw CaptureError.invalid }
                expectedActor = actor
                expectedFixture = fixture
                taxYear = year
                let login = try await request("/auth/password", body: ["email": email, "password": password])
                guard let login = login as? [String: Any], login["mustChangePassword"] as? Bool == false,
                      let identity = login["actor"] as? [String: Any],
                      identity["email"] as? String == email,
                      identity["id"] as? String == expectedActor,
                      identity["role"] as? String == "customer",
                      identity["roles"] as? [String] == ["customer"] else { throw CaptureError.invalid }
                try await verifyFixture()
                try await waitForDocument()
                writeStatus(["state": "authenticated", "harness": Self.sentinel])
                timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                    Task { @MainActor in self?.poll() }
                }
            } catch {
                _ = try? await request("/auth/logout", body: [:])
                writeStatus(["state": "failed", "phase": "authentication"])
            }
        }
    }

    private func request(_ path: String, body: [String: Any]? = nil) async throws -> Any {
        let allowed = ["/auth/password", "/auth/logout", "/session", "/state", "/money", "/taxes/\(taxYear)"]
        guard allowed.contains(path), body == nil || ["/auth/password", "/auth/logout"].contains(path) else { throw CaptureError.invalid }
        var request = URLRequest(url: URL(string: "\(origin)/api\(path)")!)
        request.setValue(origin, forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("FinMixNativeCapture/1.0", forHTTPHeaderField: "User-Agent")
        if let body = body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              response.url == request.url,
              response.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("application/json") == true else { throw CaptureError.invalid }
        return try JSONSerialization.jsonObject(with: data)
    }

    private func verifyFixture() async throws {
        guard let identity = try await request("/session") as? [String: Any],
              let actor = identity["actor"] as? [String: Any],
              actor["id"] as? String == expectedActor, actor["role"] as? String == "customer",
              var state = try await request("/state") as? [String: Any],
              let profile = state["profile"] as? [String: Any],
              profile["id"] as? String == expectedActor,
              profile["phone"] as? String == "",
              profile["messagingConsent"] as? Bool == false,
              profile["contactMethod"] as? String == "inapp",
              let items = state["items"] as? [[String: Any]], !items.isEmpty,
              items.allSatisfy({ $0["ownerId"] as? String == expectedActor && $0["sourceDocument"] == nil }),
              (state["connections"] as? [Any])?.isEmpty == true,
              (state["bankLinks"] as? [Any])?.isEmpty == true,
              (state["grants"] as? [Any])?.isEmpty == true else { throw CaptureError.invalid }
        state.removeValue(forKey: "serverTime")
        let fixture: [String: Any] = ["state": state, "money": try await request("/money"), "taxes": try await request("/taxes/\(taxYear)")]
        let bytes = try JSONSerialization.data(withJSONObject: fixture, options: [.sortedKeys, .withoutEscapingSlashes])
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        guard digest == expectedFixture else { throw CaptureError.invalid }
    }

    private func poll() {
        guard !busy, let bytes = try? Data(contentsOf: directory.appendingPathComponent("command.json")),
              let input = try? JSONSerialization.jsonObject(with: bytes) as? [String: String],
              let id = input["id"], id != lastCommand,
              id.range(of: "^[a-z0-9-]{1,60}$", options: .regularExpression) != nil,
              let route = input["route"], ["home", "bills", "calendar", "tell", "money", "taxes", "finish"].contains(route),
              let language = input["language"], ["en", "es"].contains(language) else { return }
        busy = true
        lastCommand = id
        Task {
            defer { busy = false }
            var phase = "fixture"
            do {
                if route == "finish" {
                    _ = try await request("/auth/logout", body: [:])
                    timer?.invalidate()
                    writeStatus(["state": "finished", "id": id])
                    return
                }
                try await verifyFixture()
                guard let webView = webView else { throw CaptureError.invalid }
                phase = "document"
                try await waitForDocument()
                phase = "preferences"
                let saved = try await webView.evaluateJavaScript("(()=>{localStorage.setItem('finmix-language','\(language)');localStorage.setItem('finmix-appearance',JSON.stringify({mode:'light'}));return true;})()")
                guard saved as? Bool == true else { throw CaptureError.invalid }
                phase = "navigation"
                webView.load(URLRequest(url: URL(string: "capacitor://localhost/\(route)?lang=\(language)")!))
                phase = "page"
                var stable = 0
                for _ in 0..<120 {
                    try await Task.sleep(nanoseconds: 500_000_000)
                    let ready = try? await webView.evaluateJavaScript("""
                    (()=>{const d=document;return location.pathname==='/\(route)'&&d.documentElement.lang==='\(language)'&&d.readyState==='complete'&&d.fonts.status==='loaded'&&!!d.querySelector('main h1')&&!d.querySelector('[role=alert],.error-notice,[aria-busy=true]')&&!/Getting everything ready|Estamos preparando todo/.test(d.body.innerText)&&d.documentElement.scrollWidth<=innerWidth})()
                    """)
                    stable = ready as? Bool == true ? stable + 1 : 0
                    if stable >= 3 {
                        writeStatus(["state": "ready", "id": id, "route": route, "language": language])
                        return
                    }
                }
                throw CaptureError.invalid
            } catch {
                writeStatus(["state": "failed", "phase": phase, "id": id])
            }
        }
    }

    private func waitForDocument() async throws {
        // capacitorDidLoad() runs in loadView(), before viewDidLoad() starts
        // the first navigation. Authentication can finish before WebKit has a
        // local origin; never touch localStorage in that provisional document.
        guard let webView = webView else { throw CaptureError.invalid }
        for _ in 0..<120 {
            let ready = try? await webView.evaluateJavaScript("(()=>location.protocol==='capacitor:'&&location.hostname==='localhost'&&document.readyState==='complete')()")
            if ready as? Bool == true { return }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw CaptureError.invalid
    }

    private func writeStatus(_ value: [String: String]) {
        guard let bytes = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        try? bytes.write(to: directory.appendingPathComponent("status.json"), options: .atomic)
    }

    private enum CaptureError: Error { case invalid }
}
#endif
