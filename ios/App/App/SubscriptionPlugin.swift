import Capacitor
import StoreKit
import UIKit

/// StoreKit owns checkout. FinMix finishes a transaction only after its server
/// acknowledges Apple's verified purchase for the signed-in FinMix account.
@objc(SubscriptionPlugin)
public class SubscriptionPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "SubscriptionPlugin"
    public let jsName = "FinMixSubscriptions"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "products", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "purchase", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "entitlements", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "finish", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "manage", returnType: CAPPluginReturnPromise)
    ]
    private let productIds: Set<String> = [
        "com.th3rdgroup.finmix.essential.monthly",
        "com.th3rdgroup.finmix.plus.monthly",
        "com.th3rdgroup.finmix.complete.monthly"
    ]
    private var updates: Task<Void, Never>?
    private var purchasing = false

    public override func load() {
        updates = Task { [weak self] in
            for await result in Transaction.updates {
                if Task.isCancelled { return }
                guard let self = self, case .verified(let transaction) = result,
                      self.productIds.contains(transaction.productID) else { continue }
                let data: JSObject = ["signedTransaction": result.jwsRepresentation,
                                      "transactionId": String(transaction.id)]
                await MainActor.run {
                    self.notifyListeners("changed", data: data)
                }
            }
        }
    }
    deinit { updates?.cancel() }

    @objc func products(_ call: CAPPluginCall) {
        Task {
            do {
                let products = try await Product.products(for: productIds)
                let values: [JSObject] = products.compactMap { product in
                    guard product.type == .autoRenewable,
                          let period = product.subscription?.subscriptionPeriod,
                          period.unit == .month, period.value == 1 else { return nil }
                    return ["id": product.id, "displayPrice": product.displayPrice,
                            "displayName": product.displayName]
                }
                call.resolve(["products": values])
            } catch { call.reject("APPLE_PRODUCTS_UNAVAILABLE") }
        }
    }

    @objc func purchase(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, !self.purchasing,
                  let productId = call.getString("productId"), self.productIds.contains(productId),
                  let rawToken = call.getString("appAccountToken"), let token = UUID(uuidString: rawToken) else {
                call.reject("APPLE_PURCHASE_UNAVAILABLE"); return
            }
            self.purchasing = true
            Task {
                defer { self.purchasing = false }
                do {
                    guard let product = try await Product.products(for: [productId]).first,
                          product.type == .autoRenewable else {
                        call.reject("APPLE_PRODUCTS_UNAVAILABLE"); return
                    }
                    switch try await product.purchase(options: [.appAccountToken(token)]) {
                    case .success(let result):
                        guard case .verified(let transaction) = result else {
                            call.reject("APPLE_PURCHASE_NOT_VERIFIED"); return
                        }
                        call.resolve(["state": "purchased", "signedTransaction": result.jwsRepresentation,
                                      "transactionId": String(transaction.id)])
                    case .pending: call.resolve(["state": "pending"])
                    case .userCancelled: call.resolve(["state": "canceled"])
                    @unknown default: call.reject("APPLE_PURCHASE_UNAVAILABLE")
                    }
                } catch { call.reject("APPLE_PURCHASE_UNAVAILABLE") }
            }
        }
    }

    @objc func entitlements(_ call: CAPPluginCall) {
        Task {
            do {
                // AppStore.sync can ask for credentials; call only on an explicit Restore tap.
                if call.getBool("restore") == true { try await AppStore.sync() }
                var values: [String: JSObject] = [:]
                func collect(_ result: VerificationResult<Transaction>) {
                    guard case .verified(let transaction) = result,
                          productIds.contains(transaction.productID), values.count < 20 else { return }
                    values[String(transaction.id)] = ["signedTransaction": result.jwsRepresentation,
                                                      "transactionId": String(transaction.id)]
                }
                for await result in Transaction.unfinished { collect(result) }
                for await result in Transaction.currentEntitlements { collect(result) }
                call.resolve(["transactions": Array(values.values)])
            } catch { call.reject("APPLE_RESTORE_UNAVAILABLE") }
        }
    }

    @objc func finish(_ call: CAPPluginCall) {
        guard let id = call.getString("transactionId") else {
            call.reject("APPLE_PURCHASE_NOT_VERIFIED"); return
        }
        Task {
            for await result in Transaction.unfinished {
                if case .verified(let transaction) = result,
                   String(transaction.id) == id, productIds.contains(transaction.productID) {
                    await transaction.finish(); break
                }
            }
            call.resolve()
        }
    }

    @objc func manage(_ call: CAPPluginCall) {
        Task { @MainActor [weak self] in
            guard let scene = self?.bridge?.viewController?.view.window?.windowScene else {
                call.reject("APPLE_MANAGE_UNAVAILABLE"); return
            }
            do { try await AppStore.showManageSubscriptions(in: scene); call.resolve() }
            catch { call.reject("APPLE_MANAGE_UNAVAILABLE") }
        }
    }
}
