#if DEBUG && targetEnvironment(simulator)
import UIKit
import WebKit
import ImageIO
import UniformTypeIdentifiers

// Offline, fictional bridge checks. This class and its entry point are excluded
// from device/Release builds. No login, customer record or filing is performed.
@MainActor
final class NativeFeatureSmokeHarness {
    private static let sentinel = "FINMIX_DEBUG_FEATURE_SMOKE_V1"
    private weak var webView: WKWebView?
    private let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("FinMixNativeSmoke", isDirectory: true)
    private let shareRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("finmix-checklist-export", isDirectory: true)
    private var proof: [String: Any] = [:]

    init(webView: WKWebView) { self.webView = webView }

    func start() {
        guard ProcessInfo.processInfo.environment["FINMIX_NATIVE_FEATURE_SMOKE"] == "1" else { return }
        Task { await run() }
    }

    private func js(_ body: String, arguments: [String: Any] = [:]) async throws -> Any? {
        guard let webView = webView else { throw SmokeError.failed }
        return try await webView.callAsyncJavaScript(body, arguments: arguments, in: nil, contentWorld: .page)
    }

    private func require(_ value: Bool) throws {
        if !value { throw SmokeError.failed }
    }

    private func fixture(_ language: String, rotated: Bool = false) throws -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let size = CGSize(width: 960, height: 430)
        let image = UIGraphicsImageRenderer(size: size, format: format).image { context in
            UIColor.white.setFill()
            context.fill(CGRect(origin: .zero, size: size))
            let text = language == "es"
                ? "CITA DE EJEMPLO\n12 noviembre 2026\n10:30 AM\n123 Example Street\nCiudad de ejemplo"
                : "EXAMPLE APPOINTMENT\nNovember 12, 2026\n10:30 AM\n123 Example Street\nExample City"
            (text as NSString).draw(in: CGRect(x: 40, y: 30, width: 880, height: 370), withAttributes: [
                .font: UIFont.monospacedSystemFont(ofSize: 44, weight: .medium),
                .foregroundColor: UIColor.black
            ])
        }
        if !rotated {
            guard let bytes = image.pngData() else { throw SmokeError.failed }
            return bytes
        }
        let sideways = UIGraphicsImageRenderer(size: CGSize(width: size.height, height: size.width), format: format).image { context in
            context.cgContext.translateBy(x: 0, y: size.width)
            context.cgContext.rotate(by: -.pi / 2)
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        let output = NSMutableData()
        guard let cgImage = sideways.cgImage,
              let destination = CGImageDestinationCreateWithData(output, UTType.jpeg.identifier as CFString, 1, nil) else { throw SmokeError.failed }
        CGImageDestinationAddImage(destination, cgImage, [kCGImagePropertyOrientation: 6] as CFDictionary)
        try require(CGImageDestinationFinalize(destination))
        return output as Data
    }

    private func recognize(_ language: String, rotated: Bool = false) async throws {
        let bytes = try fixture(language, rotated: rotated)
        let result = try await js("""
        return await Promise.race([
          window.Capacitor.nativePromise('AppointmentReader', 'recognize', {data}),
          new Promise((_, reject) => setTimeout(() => reject(new Error('SMOKE_TIMEOUT')), 25000))
        ]);
        """, arguments: ["data": bytes.base64EncodedString()])
        guard let result = result as? [String: Any], let text = result["text"] as? String else { throw SmokeError.failed }
        let normalized = text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: Locale(identifier: "en_US"))
        try require(["2026", "10:30", "123 example street", language == "es" ? "cita" : "appointment"].allSatisfy { normalized.contains($0) })
    }

    private func beginShare() async throws -> UIActivityViewController {
        _ = try await js("""
        window.__finmixFeatureShare = {state:'pending'};
        window.Capacitor.nativePromise('ChecklistExport', 'share', {
          filename:'finmix-taxes-2025.txt', text
        }).then(result => {window.__finmixFeatureShare = {state:'resolved', completed:result.completed};},
                () => {window.__finmixFeatureShare = {state:'rejected'};});
        return true;
        """, arguments: ["text": "FinMix fictional checklist\nNo real tax or identity data."])
        for _ in 0..<40 {
            if let sheet = webView?.window?.rootViewController?.presentedViewController as? UIActivityViewController,
               !sheet.isBeingPresented { return sheet }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        proof["windowAttached"] = webView?.window != nil
        if let root = webView?.window?.rootViewController {
            proof["rootControllerClass"] = String(describing: type(of: root))
            if let presented = root.presentedViewController {
                proof["presentedControllerClass"] = String(describing: type(of: presented))
            }
        }
        if let value = try await js("return window.__finmixFeatureShare;") as? [String: Any] {
            proof["shareCallState"] = value["state"] as? String
        }
        throw SmokeError.failed
    }

    private func finishShare(_ sheet: UIActivityViewController, failed: Bool) async throws {
        // Exercise the real plugin completion handler with an explicit simulated
        // framework callback. This does not claim a user saved anything to Files.
        let callback = sheet.completionWithItemsHandler
        sheet.completionWithItemsHandler = nil
        sheet.dismiss(animated: false)
        callback?(nil, false, nil, failed ? NSError(domain: "FinMixFictionalSmoke", code: 1) : nil)
        for _ in 0..<40 {
            let result = try await js("return window.__finmixFeatureShare;") as? [String: Any]
            if result?["state"] as? String == (failed ? "rejected" : "resolved") {
                if !failed { try require(result?["completed"] as? Bool == false) }
                try require(!FileManager.default.fileExists(atPath: shareRoot.path))
                return
            }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        throw SmokeError.failed
    }

    private func write(_ state: String, phase: String) {
        var value = proof
        value["state"] = state
        value["phase"] = phase
        value["harness"] = Self.sentinel
        value["originalHandwritingVerified"] = false
        value["saveToFilesVerified"] = false
        value["completionCallbacks"] = "simulated"
        if let bytes = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try? bytes.write(to: directory.appendingPathComponent("status.json"), options: [.atomic, .completeFileProtection])
        }
    }

    private func run() async {
        var phase = "bridge"
        do {
            var ready = false
            for _ in 0..<120 {
                ready = (try? await js("return location.protocol==='capacitor:' && location.hostname==='localhost' && document.readyState==='complete' && !!window.Capacitor?.nativePromise;")) as? Bool == true
                if ready { break }
                try await Task.sleep(nanoseconds: 500_000_000)
            }
            try require(ready)
            proof["bridgeReady"] = true
            phase = "english-photo"
            try await recognize("en"); proof["englishPhotoRead"] = true
            phase = "spanish-photo"
            try await recognize("es"); proof["spanishPhotoRead"] = true
            phase = "rotated-photo"
            try await recognize("en", rotated: true); proof["exifRotatedPhotoRead"] = true
            phase = "invalid-inputs"
            let rejected = try await js("""
            let photo=false, filename=false;
            try {await window.Capacitor.nativePromise('AppointmentReader','recognize',{data:'not-base64'});} catch(e) {photo=e.message==='PHOTO_NOT_READABLE';}
            try {await window.Capacitor.nativePromise('ChecklistExport','share',{filename:'../wrong.txt',text:'fictional'});} catch(e) {filename=e.message==='CHECKLIST_EXPORT_UNAVAILABLE';}
            return photo && filename;
            """)
            try require(rejected as? Bool == true); proof["invalidInputsRejected"] = true
            phase = "share-presentation"
            let sheet = try await beginShare()
            proof["shareSheetPresented"] = true
            phase = "share-file"
            let file = shareRoot.appendingPathComponent("finmix-taxes-2025.txt")
            try require(try String(contentsOf: file, encoding: .utf8) == "FinMix fictional checklist\nNo real tax or identity data.")
            phase = "share-protection"
            let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
            proof["fileProtection"] = String(describing: attributes[.protectionKey] ?? "missing")
            // Foundation documents this dictionary value as NSString. Accept
            // either bridge representation, while requiring the same value.
            // https://developer.apple.com/documentation/foundation/fileattributekey/protectionkey
            let protection = (attributes[.protectionKey] as? FileProtectionType)?.rawValue
                ?? (attributes[.protectionKey] as? String)
            try require(protection == FileProtectionType.complete.rawValue)
            phase = "share-anchor"
            if UIDevice.current.userInterfaceIdiom == .pad {
                try require(sheet.popoverPresentationController?.sourceView != nil)
            }
            proof["protectedShareFileMatches"] = true
            phase = "share-busy"
            let busyRejected = try await js("""
            try {await window.Capacitor.nativePromise('ChecklistExport','share',{filename:'finmix-taxes-2025.txt',text:'fictional'}); return false;}
            catch(e) {return e.message==='CHECKLIST_EXPORT_UNAVAILABLE';}
            """)
            try require(busyRejected as? Bool == true); proof["duplicateShareRejected"] = true
            write("share-presented", phase: phase)
            // The runner captures the actual share sheet before acknowledging.
            var acknowledged = false
            for _ in 0..<120 {
                acknowledged = (try? String(contentsOf: directory.appendingPathComponent("continue.txt"), encoding: .utf8)) == "continue"
                if acknowledged { break }
                try await Task.sleep(nanoseconds: 250_000_000)
            }
            try require(acknowledged)
            phase = "cancel-cleanup"
            try await finishShare(sheet, failed: false); proof["cancelReturnsFalseAndCleans"] = true
            phase = "error-cleanup"
            let secondSheet = try await beginShare()
            try await finishShare(secondSheet, failed: true); proof["retryAndErrorCleanup"] = true
            write("passed", phase: "complete")
        } catch {
            write("failed", phase: phase)
        }
    }

    private enum SmokeError: Error { case failed }
}
#endif
