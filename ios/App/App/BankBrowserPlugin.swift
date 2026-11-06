import UIKit
import Capacitor
import AuthenticationServices
import Vision
import ImageIO

class AlDiaBridgeViewController: CAPBridgeViewController {
#if DEBUG && targetEnvironment(simulator)
    private var nativeCapture: NativeCaptureHarness?
#endif
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(BankBrowserPlugin())
        bridge?.registerPluginInstance(ChecklistExportPlugin())
        bridge?.registerPluginInstance(AppointmentReaderPlugin())
#if DEBUG && targetEnvironment(simulator)
        if let webView = bridge?.webView {
            nativeCapture = NativeCaptureHarness(webView: webView)
            nativeCapture?.start()
        }
#endif
    }
}

@objc(AppointmentReaderPlugin)
public class AppointmentReaderPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "AppointmentReaderPlugin"
    public let jsName = "AppointmentReader"
    public let pluginMethods: [CAPPluginMethod] = [CAPPluginMethod(name: "recognize", returnType: CAPPluginReturnPromise)]
    private var reading = false

    @objc func recognize(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, !self.reading,
                  let encoded = call.getString("data"), encoded.utf8.count <= 11184812,
                  let bytes = Data(base64Encoded: encoded), !bytes.isEmpty, bytes.count <= 8388608,
                  let source = CGImageSourceCreateWithData(bytes as CFData, nil),
                  let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
                  let height = properties[kCGImagePropertyPixelHeight] as? NSNumber,
                  width.intValue > 0, height.intValue > 0,
                  width.intValue * height.intValue <= 40000000 else {
                call.reject("PHOTO_NOT_READABLE")
                return
            }
            self.reading = true
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let request = VNRecognizeTextRequest()
                    request.recognitionLevel = .accurate
                    request.recognitionLanguages = ["en-US", "es-ES"]
                    request.usesLanguageCorrection = true
                    try VNImageRequestHandler(data: bytes, options: [:]).perform([request])
                    let lines = (request.results ?? []).compactMap { observation -> String? in
                        guard let candidate = observation.topCandidates(1).first, candidate.confidence >= 0.5 else { return nil }
                        return candidate.string
                    }
                    let text = String(lines.joined(separator: "\n").prefix(12000))
                    DispatchQueue.main.async { self.reading = false; call.resolve(["text": text]) }
                } catch {
                    DispatchQueue.main.async { self.reading = false; call.reject("PHOTO_NOT_READABLE") }
                }
            }
        }
    }
}

@objc(ChecklistExportPlugin)
public class ChecklistExportPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "ChecklistExportPlugin"
    public let jsName = "ChecklistExport"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "share", returnType: CAPPluginReturnPromise)
    ]
    private var sharing = false

    public override func load() {
        try? FileManager.default.removeItem(at: FileManager.default.temporaryDirectory.appendingPathComponent("finmix-checklist-export", isDirectory: true))
    }

    @objc func share(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, !self.sharing,
                  let text = call.getString("text"), !text.isEmpty, text.utf8.count <= 65536,
                  let filename = call.getString("filename"),
                  filename.range(of: "^finmix-taxes-[0-9]{4}\\.txt$", options: .regularExpression) != nil,
                  let presenter = self.bridge?.viewController,
                  presenter.view.window != nil, presenter.presentedViewController == nil else {
                call.reject("CHECKLIST_EXPORT_UNAVAILABLE")
                return
            }
            let root = FileManager.default.temporaryDirectory.appendingPathComponent("finmix-checklist-export", isDirectory: true)
            do {
                // Remove leftovers from an interrupted prior share. No archive is kept here.
                if FileManager.default.fileExists(atPath: root.path) { try FileManager.default.removeItem(at: root) }
                try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
                let file = root.appendingPathComponent(filename)
                try Data(text.utf8).write(to: file, options: [.atomic, .completeFileProtection])
                let sheet = UIActivityViewController(activityItems: [file], applicationActivities: nil)
                sheet.popoverPresentationController?.sourceView = presenter.view
                sheet.popoverPresentationController?.sourceRect = CGRect(x: presenter.view.bounds.midX, y: presenter.view.bounds.maxY - 1, width: 1, height: 1)
                sheet.completionWithItemsHandler = { [weak self] _, completed, _, error in
                    try? FileManager.default.removeItem(at: root)
                    self?.sharing = false
                    if error != nil { call.reject("CHECKLIST_EXPORT_UNAVAILABLE") }
                    else { call.resolve(["completed": completed]) }
                }
                self.sharing = true
                presenter.present(sheet, animated: true)
            } catch {
                try? FileManager.default.removeItem(at: root)
                self.sharing = false
                call.reject("CHECKLIST_EXPORT_UNAVAILABLE")
            }
        }
    }
}

@objc(BankBrowserPlugin)
public class BankBrowserPlugin: CAPPlugin, CAPBridgedPlugin, ASWebAuthenticationPresentationContextProviding {
    public let identifier = "BankBrowserPlugin"
    public let jsName = "BankBrowser"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "open", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "close", returnType: CAPPluginReturnPromise)
    ]
    private var session: ASWebAuthenticationSession?
    private var pending: CAPPluginCall?
    private var anchor: UIWindow?

    @objc func open(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, self.pending == nil,
                  let raw = call.getString("url"), let url = URL(string: raw),
                  url.scheme == "https", url.host == "secure.plaid.com",
                  url.port == nil || url.port == 443,
                  url.user == nil, url.password == nil, url.path.hasPrefix("/hl/"),
                  let window = self.bridge?.viewController?.view.window else {
                call.reject("BANK_LINK_UNAVAILABLE")
                return
            }
            self.anchor = window
            self.pending = call
            let session = ASWebAuthenticationSession(url: url, callbackURLScheme: "com.th3rdgroup.finmix") { [weak self] callback, error in
                DispatchQueue.main.async {
                    guard let self = self, self.pending === call else { return }
                    let canceled = (error as? ASWebAuthenticationSessionError)?.code == .canceledLogin
                    if canceled || callback?.absoluteString == "com.th3rdgroup.finmix://bank/complete" {
                        self.finish()
                    } else {
                        self.finish(failed: true)
                    }
                }
            }
            self.session = session
            session.presentationContextProvider = self
            if !session.start() { self.finish(failed: true) }
        }
    }

    private func finish(failed: Bool = false) {
        let call = pending
        pending = nil
        session = nil
        anchor = nil
        if failed { call?.reject("BANK_LINK_UNAVAILABLE") }
        else { call?.resolve() }
    }

    @objc func close(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            self?.session?.cancel()
            self?.finish()
            call.resolve()
        }
    }

    public func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        // Presentation begins only after open() has captured an attached window.
        return anchor!
    }
}
