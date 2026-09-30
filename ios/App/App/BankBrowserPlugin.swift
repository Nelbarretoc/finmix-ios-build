import UIKit
import Capacitor
import AuthenticationServices

class AlDiaBridgeViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(BankBrowserPlugin())
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
