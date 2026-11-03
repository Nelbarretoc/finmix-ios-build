#!/usr/bin/env python3
"""Manual App Store release helper; no secrets or IPA become public artifacts.

Apple tool contracts, checked 2026-11-03:
https://help.apple.com/asc/appsaltool/en.lproj/static.html
https://help.apple.com/xcode/mac/current/en.lproj/deva1f2ab5a2.html
https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles
Runtime xcodebuild/altool help is checked before decoding credentials.
"""

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import urllib.request
import zipfile

BUNDLE_ID = "com.th3rdgroup.finmix"
REPOSITORY = "Nelbarretoc/finmix-ios-build"
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
SECRET_NAMES = (
    "APPLE_DISTRIBUTION_P12_BASE64",
    "APPLE_DISTRIBUTION_P12_PASSWORD",
    "APPLE_PROVISIONING_PROFILE_BASE64",
    "APP_STORE_CONNECT_API_KEY_BASE64",
    "APP_STORE_CONNECT_KEY_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
)


class ReleaseError(Exception):
    """Messages are deliberately safe for public CI logs."""


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def tool_environment(env=None):
    clean = dict(os.environ if env is None else env)
    # Build tools need neither the original encoded credentials nor CI tokens.
    # Pass only the explicit file paths/arguments needed for each operation.
    for name in (*SECRET_NAMES, "GH_TOKEN", "GITHUB_TOKEN"):
        clean.pop(name, None)
    return clean


def config(env):
    rules = {
        "FINMIX_SOURCE_COMMIT": r"[0-9a-f]{40}",
        "FINMIX_APPLE_TEAM_ID": r"[A-Z0-9]{10}",
        "FINMIX_PROFILE_UUID": UUID,
        "FINMIX_BUILD_NUMBER": r"[1-9][0-9]{0,3}",
        "FINMIX_APP_APPLE_ID": r"[1-9][0-9]{5,14}",
        "FINMIX_ASC_PUBLIC_ID": UUID,
        "FINMIX_UPLOAD": r"true|false",
    }
    result = {}
    for name, pattern in rules.items():
        value = env.get(name, "")
        require(bool(re.fullmatch(pattern, value)), f"Invalid release input: {name}")
        result[name] = value
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "Unexpected repository")
    require(env.get("GITHUB_REF") == "refs/heads/main", "Release requires main")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Manual dispatch required")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))), "Invalid wrapper commit")
    if result["FINMIX_UPLOAD"] == "true":
        require(env.get("GITHUB_RUN_ATTEMPT") == "1", "Reconcile the previous upload in App Store Connect before a new dispatch; automatic upload reruns are disabled")
    return result


def capture(command, label):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=tool_environment())
    require(result.returncode == 0, f"{label} failed; no secret output was printed")
    return result.stdout


def check_source(cfg):
    require(Path("SOURCE_COMMIT").read_text().strip() == cfg["FINMIX_SOURCE_COMMIT"], "Wrapper does not contain the requested application revision")
    require(capture(["git", "rev-parse", "HEAD"], "Wrapper revision check").decode().strip() == os.environ["GITHUB_SHA"], "Checkout does not match the dispatched wrapper revision")


def check_tools(diagnostics=False):
    version = capture(["xcodebuild", "-version"], "Xcode version check").decode()
    match = re.search(r"Xcode (\d+)", version)
    require(match is not None and int(match[1]) >= 26, "Xcode 26 or later is required")
    help_result = subprocess.run(["xcodebuild", "-help"], capture_output=True, env=tool_environment())
    help_text = (help_result.stdout + help_result.stderr).decode(errors="replace")
    missing = [option for option in ("app-store-connect", "provisioningProfiles", "signingCertificate", "signingStyle", "manageAppVersionAndBuildNumber", "destination", "teamID") if option not in help_text]
    require(not missing, f"Runner xcodebuild help (exit {help_result.returncode}) is missing required export options: {', '.join(missing)}")
    help_result = subprocess.run(["xcrun", "altool", "--help"], capture_output=True, env=tool_environment())
    help_text = (help_result.stdout + help_result.stderr).decode(errors="replace")
    missing = [option for option in ("--upload-package", "--validate-app", "--apiKey", "--apiIssuer", "--apple-id", "--asc-public-id", "--bundle-version", "--type") if option not in help_text]
    if missing and diagnostics:
        # Only the fixed, unauthenticated help command may be printed. Never
        # reuse this path for signing, validation, upload or credential errors.
        credential_names = (*SECRET_NAMES, "API_PRIVATE_KEYS_DIR", "GH_TOKEN", "GITHUB_TOKEN")
        if not any(os.environ.get(name) for name in credential_names):
            print(f"Installed xcrun altool --help (exit {help_result.returncode}; no authentication supplied):", flush=True)
            print(help_text, flush=True)
        else:
            print("Installed altool help is not printed in a credential-bearing environment.", flush=True)
    require(not missing, f"Runner altool help (exit {help_result.returncode}) is missing required upload options: {', '.join(missing)}")
    manual = capture(["man", "-P", "cat", "codesign"], "Installed codesign documentation")
    manual = re.sub(r".\x08", "", manual.decode(errors="replace"))
    require("--entitlements" in manual and "--xml" in manual, "Runner codesign does not document XML entitlement output")


def check_unsigned():
    token = os.environ.get("GH_TOKEN", "")
    require(bool(token), "GitHub read token is missing")
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/ios-build.yml/runs?head_sha={os.environ['GITHUB_SHA']}&status=success&per_page=20"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    require(any(run.get("head_sha") == os.environ["GITHUB_SHA"] and run.get("head_branch") == "main" and run.get("status") == "completed" and run.get("conclusion") == "success" for run in result.get("workflow_runs", [])), "This exact wrapper revision has no successful unsigned iPhone/iPad workflow")


def validate_profile(profile, cfg, now):
    team = cfg["FINMIX_APPLE_TEAM_ID"]
    require(profile.get("UUID", "").lower() == cfg["FINMIX_PROFILE_UUID"].lower(), "Provisioning profile UUID does not match")
    require(profile.get("TeamIdentifier") == [team], "Provisioning profile belongs to another team")
    expiry = profile.get("ExpirationDate")
    require(isinstance(expiry, dt.datetime), "Provisioning profile has no valid expiry")
    require(expiry.replace(tzinfo=dt.timezone.utc) > now, "Provisioning profile is expired")
    entitlements = profile.get("Entitlements", {})
    require(entitlements.get("application-identifier") == f"{team}.{BUNDLE_ID}", "Provisioning profile does not match the exact FinMix bundle")
    require(entitlements.get("com.apple.developer.team-identifier") == team, "Provisioning entitlement team mismatch")
    require(entitlements.get("get-task-allow") is False, "Development provisioning profile is not allowed")
    require(not profile.get("ProvisionedDevices") and not profile.get("ProvisionsAllDevices"), "An App Store distribution profile is required")
    require(entitlements.get("beta-reports-active") is True, "Provisioning profile is not enabled for App Store/TestFlight distribution")
    domains = entitlements.get("com.apple.developer.associated-domains", [])
    require("*" in domains or "applinks:finmix.th3rdgroup.com" in domains, "Profile is missing FinMix Associated Domains capability")
    certs = profile.get("DeveloperCertificates", [])
    require(bool(certs) and all(isinstance(cert, bytes) for cert in certs), "Provisioning profile has no signing certificates")
    return {hashlib.sha1(cert).hexdigest().upper() for cert in certs}


def select_identity(output, allowed, team):
    matches = [(digest.upper(), name) for digest, name in re.findall(r'([0-9A-Fa-f]{40}) "([^"\n]+)"', output) if digest.upper() in allowed and name.startswith("Apple Distribution:") and name.endswith(f"({team})")]
    require(len(matches) == 1, "Exactly one valid matching Apple Distribution identity is required")
    return matches[0][0]


def export_options(cfg, identity):
    return {
        "method": "app-store-connect",
        "destination": "export",
        "teamID": cfg["FINMIX_APPLE_TEAM_ID"],
        "signingStyle": "manual",
        "signingCertificate": identity,
        "provisioningProfiles": {BUNDLE_ID: cfg["FINMIX_PROFILE_UUID"]},
        "manageAppVersionAndBuildNumber": False,
    }


def signing_settings(cfg, identity, keychain):
    # Only the App Release target consumes these variables. Standard signing
    # settings on the command line would also override dependency frameworks.
    return [
        "FINMIX_SIGNING_STYLE=Manual",
        f"FINMIX_SIGNING_TEAM={cfg['FINMIX_APPLE_TEAM_ID']}",
        f"FINMIX_SIGNING_IDENTITY={identity}",
        f"FINMIX_SIGNING_PROFILE={cfg['FINMIX_PROFILE_UUID']}",
        f"CURRENT_PROJECT_VERSION={cfg['FINMIX_BUILD_NUMBER']}",
        f"OTHER_CODE_SIGN_FLAGS=--keychain {keychain}",
    ]


def validate_build_settings(targets, cfg, identity):
    apps = [target for target in targets if target.get("target") == "App"]
    require(len(apps) == 1, "Expected exactly one App build target")
    expected = {
        "PRODUCT_BUNDLE_IDENTIFIER": BUNDLE_ID,
        "CODE_SIGN_STYLE": "Manual",
        "CODE_SIGN_IDENTITY": identity,
        "DEVELOPMENT_TEAM": cfg["FINMIX_APPLE_TEAM_ID"],
        "PROVISIONING_PROFILE_SPECIFIER": cfg["FINMIX_PROFILE_UUID"],
    }
    settings = apps[0].get("buildSettings", {})
    require(all(settings.get(key) == value for key, value in expected.items()), "App signing settings did not resolve to the verified identity and profile")
    for target in targets:
        if target.get("target") != "App":
            require(not target.get("buildSettings", {}).get("PROVISIONING_PROFILE_SPECIFIER"), "App provisioning profile leaked into a dependency target")


def upload_command(ipa, cfg, version, key_id, issuer_id):
    return ["xcrun", "altool", "--upload-package", str(ipa), "--type", "ios", "--asc-public-id", cfg["FINMIX_ASC_PUBLIC_ID"], "--apple-id", cfg["FINMIX_APP_APPLE_ID"], "--bundle-id", BUNDLE_ID, "--bundle-short-version-string", version, "--bundle-version", cfg["FINMIX_BUILD_NUMBER"], "--apiKey", key_id, "--apiIssuer", issuer_id, "--output-format", "json"]


def workspace():
    root = Path(os.environ.get("RUNNER_TEMP", ""))
    require(root.is_absolute() and root.is_dir(), "Valid runner temporary directory required")
    return root / "finmix-app-store-private"


def cleanup():
    private = workspace()
    if not private.exists():
        return
    require(not private.is_symlink(), "Unexpected temporary workspace link")
    saved = {}
    failure = None
    try:
        state = private / "cleanup.json"
        if state.exists():
            try:
                saved = json.loads(state.read_text())
            except (ValueError, OSError):
                failure = "Interrupted cleanup state; runner teardown still required"
        # Remove only links into this exact temporary workspace, even if the
        # preceding process was interrupted before saving its cleanup metadata.
        directory = Path.home() / "Library/MobileDevice/Provisioning Profiles"
        if directory.exists():
            for link in directory.glob("*.mobileprovision"):
                if link.is_symlink() and link.resolve() == private / "profile.mobileprovision":
                    link.unlink()
        keychain = private / "signing.keychain-db"
        if keychain.exists():
            subprocess.run(["security", "delete-keychain", str(keychain)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=tool_environment())
        if "previous_keychains" in saved:
            result = subprocess.run(["security", "list-keychains", "-d", "user", "-s", *saved["previous_keychains"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=tool_environment())
            if result.returncode != 0:
                failure = "Failed to restore runner keychain search list"
    finally:
        shutil.rmtree(private)
    require(failure is None, failure)


def run_private(command, label, private, env=None):
    # Security/Apple output can include credential material or customer data.
    # Discard it instead of persisting a diagnostic log, even temporarily.
    result = subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=tool_environment(env),
    )
    require(
        result.returncode == 0,
        f"{label} failed (exit {result.returncode}); sensitive tool output is discarded",
    )


def validate_bundle_entitlements(entitlements, cfg):
    require(entitlements.get("application-identifier") == f"{cfg['FINMIX_APPLE_TEAM_ID']}.{BUNDLE_ID}", "Signed app belongs to another team or bundle")
    require(entitlements.get("com.apple.developer.team-identifier") == cfg["FINMIX_APPLE_TEAM_ID"], "Signed app team mismatch")
    require(entitlements.get("get-task-allow") is not True, "Signed app allows debugging")
    require(entitlements.get("com.apple.developer.associated-domains") == ["applinks:finmix.th3rdgroup.com"], "Signed app must contain the exact FinMix Associated Domains entitlement")


def verify_privacy_manifests(app):
    manifests = (
        (Path("ios/App/App/PrivacyInfo.xcprivacy"), "PrivacyInfo.xcprivacy"),
        (Path("node_modules/@capacitor/ios/Capacitor/Capacitor/PrivacyInfo.xcprivacy"), "Frameworks/Capacitor.framework/PrivacyInfo.xcprivacy"),
        (Path("node_modules/@capacitor/ios/CapacitorCordova/CapacitorCordova/PrivacyInfo.xcprivacy"), "Frameworks/Cordova.framework/PrivacyInfo.xcprivacy"),
    )
    for source, relative in manifests:
        bundled = app / relative
        require(source.is_file() and bundled.is_file(), "Signed app is missing a required privacy manifest")
        require(plistlib.loads(source.read_bytes()) == plistlib.loads(bundled.read_bytes()), "Signed app privacy declaration differs from its reviewed source")


def verify_bundle(app, cfg, private):
    info = plistlib.loads((app / "Info.plist").read_bytes())
    require(info.get("CFBundleIdentifier") == BUNDLE_ID, "Signed bundle ID mismatch")
    require(str(info.get("CFBundleVersion")) == cfg["FINMIX_BUILD_NUMBER"], "Signed build number mismatch")
    version = info.get("CFBundleShortVersionString", "")
    require(bool(re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version)), "Invalid signed app version")
    run_private(["codesign", "--verify", "--deep", "--strict", str(app)], "verify-signature", private)
    entitlements = plistlib.loads(capture(["codesign", "--display", "--entitlements", "-", "--xml", str(app)], "Signed entitlement check"))
    validate_bundle_entitlements(entitlements, cfg)
    verify_privacy_manifests(app)
    count = 0
    for source in Path("dist-native").rglob("*"):
        if source.is_file():
            bundled = app / "public" / source.relative_to("dist-native")
            require(bundled.is_file() and hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(bundled.read_bytes()).digest(), "Signed app web assets differ from the tested wrapper")
            count += 1
    require(count > 0, "No frozen native assets found")
    return version, count


def release(cfg):
    for name in SECRET_NAMES:
        require(bool(os.environ.get(name)), f"Missing existing GitHub secret: {name}")
    key_id = os.environ["APP_STORE_CONNECT_KEY_ID"]
    issuer_id = os.environ["APP_STORE_CONNECT_ISSUER_ID"]
    require(bool(re.fullmatch(r"[A-Z0-9]{10}", key_id)), "Invalid API key ID")
    require(bool(re.fullmatch(UUID, issuer_id)), "Invalid API issuer ID")
    check_source(cfg)
    private = workspace()
    require(not private.exists(), "Signing workspace already exists; cleanup is required")
    os.umask(0o077)
    private.mkdir(mode=0o700)
    saved = {}
    result_path = private.parent / "finmix-app-store-result.json"
    result = {"sourceCommit": cfg["FINMIX_SOURCE_COMMIT"], "wrapperCommit": os.environ["GITHUB_SHA"], "bundleId": BUNDLE_ID, "buildNumber": cfg["FINMIX_BUILD_NUMBER"], "signedVerified": False, "appleValidation": "not_attempted", "upload": "not_attempted", "appStoreAvailable": False}

    def record():
        result_path.write_text(json.dumps(result, indent=2) + "\n")

    def save_cleanup():
        pending = private / "cleanup.pending.json"
        pending.write_text(json.dumps(saved))
        pending.replace(private / "cleanup.json")

    def decode(name, target):
        try:
            target.write_bytes(base64.b64decode("".join(os.environ[name].split()), validate=True))
        except Exception:
            raise ReleaseError(f"Invalid encoded credential: {name}") from None

    record()
    try:
        decode("APPLE_DISTRIBUTION_P12_BASE64", private / "distribution.p12")
        decode("APPLE_PROVISIONING_PROFILE_BASE64", private / "profile.mobileprovision")
        keys = private / "private_keys"
        keys.mkdir(mode=0o700)
        decode("APP_STORE_CONNECT_API_KEY_BASE64", keys / f"AuthKey_{key_id}.p8")
        run_private(["openssl", "pkey", "-in", str(keys / f"AuthKey_{key_id}.p8"), "-check", "-noout"], "api-key-check", private)
        profile = plistlib.loads(capture(["security", "cms", "-D", "-i", str(private / "profile.mobileprovision")], "Provisioning profile decoding"))
        allowed = validate_profile(profile, cfg, dt.datetime.now(dt.timezone.utc))
        cfg = {**cfg, "FINMIX_PROFILE_UUID": profile["UUID"]}
        saved["previous_keychains"] = shlex.split(capture(["security", "list-keychains", "-d", "user"], "Keychain search list capture").decode())
        save_cleanup()
        keychain = private / "signing.keychain-db"
        password = secrets.token_urlsafe(32)
        run_private(["security", "create-keychain", "-p", password, str(keychain)], "create-keychain", private)
        saved["keychain_created"] = True
        save_cleanup()
        run_private(["security", "set-keychain-settings", "-lut", "3600", str(keychain)], "keychain-settings", private)
        run_private(["security", "unlock-keychain", "-p", password, str(keychain)], "unlock-keychain", private)
        run_private(["security", "import", str(private / "distribution.p12"), "-k", str(keychain), "-P", os.environ["APPLE_DISTRIBUTION_P12_PASSWORD"], "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"], "import-certificate", private)
        run_private(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, str(keychain)], "keychain-partitions", private)
        run_private(["security", "list-keychains", "-d", "user", "-s", str(keychain), *saved["previous_keychains"]], "keychain-search", private)
        identity = select_identity(capture(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], "Signing identity check").decode(), allowed, cfg["FINMIX_APPLE_TEAM_ID"])
        profile_dir = Path.home() / "Library/MobileDevice/Provisioning Profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_link = profile_dir / f"{profile['UUID']}.mobileprovision"
        require(not profile_link.exists() and not profile_link.is_symlink(), "Provisioning profile already exists on this runner")
        saved["profile_link"] = str(profile_link)
        save_cleanup()
        profile_link.symlink_to(private / "profile.mobileprovision")
        archive = private / "FinMix.xcarchive"
        build = [
            "xcodebuild", "-project", "ios/App/App.xcodeproj", "-scheme", "App",
            "-configuration", "Release", "-sdk", "iphoneos",
            "-destination", "generic/platform=iOS",
            "-derivedDataPath", str(private / "DerivedData"),
            *signing_settings(cfg, identity, keychain),
        ]
        resolved = json.loads(capture([*build, "-showBuildSettings", "-json"], "Resolved app signing settings"))
        validate_build_settings(resolved, cfg, identity)
        run_private([*build, "-archivePath", str(archive), "archive"], "archive", private)
        version, count = verify_bundle(archive / "Products/Applications/App.app", cfg, private)
        options = private / "ExportOptions.plist"
        options.write_bytes(plistlib.dumps(export_options(cfg, identity)))
        export = private / "export"
        run_private(["xcodebuild", "-exportArchive", "-archivePath", str(archive), "-exportPath", str(export), "-exportOptionsPlist", str(options)], "export", private)
        ipas = list(export.glob("*.ipa"))
        require(len(ipas) == 1, "Expected exactly one exported IPA")
        ipa = ipas[0]
        with zipfile.ZipFile(ipa) as package:
            require(all(not name.startswith("/") and ".." not in Path(name).parts for name in package.namelist()), "Invalid IPA archive paths")
        unpacked = private / "verify-ipa"
        run_private(["ditto", "-x", "-k", str(ipa), str(unpacked)], "unpack-ipa", private)
        apps = list((unpacked / "Payload").glob("*.app"))
        require(len(apps) == 1, "Expected exactly one app in exported IPA")
        require(verify_bundle(apps[0], cfg, private) == (version, count), "Exported IPA metadata mismatch")
        result.update({"signedVerified": True, "version": version, "matchedNativeFiles": count, "ipaSha256": hashlib.sha256(ipa.read_bytes()).hexdigest()})
        record()
        upload_env = {**os.environ, "API_PRIVATE_KEYS_DIR": str(keys)}
        run_private(["xcrun", "altool", "--validate-app", "-f", str(ipa), "--type", "ios", "--apiKey", key_id, "--apiIssuer", issuer_id, "--output-format", "json"], "apple-validation", private, upload_env)
        result["appleValidation"] = "passed"
        record()
        if cfg["FINMIX_UPLOAD"] == "true":
            result["upload"] = "attempted_outcome_unknown"
            record()
            run_private(upload_command(ipa, cfg, version, key_id, issuer_id), "apple-upload", private, upload_env)
            result["upload"] = "uploaded_for_processing"
            record()
        print("Signed IPA verified and Apple validation passed. " + ("Upload accepted for processing; TestFlight and App Store release are not confirmed." if cfg["FINMIX_UPLOAD"] == "true" else "Upload was not requested; private IPA is removed after validation."))
    finally:
        cleanup()


def main():
    try:
        command = sys.argv[1] if len(sys.argv) == 2 else ""
        if command == "cleanup":
            cleanup()
            return 0
        if command == "tools":
            check_tools(diagnostics=True)
            print("Installed Apple archive, export and upload tool options verified; nothing was signed or uploaded.")
            return 0
        cfg = config(os.environ)
        if command == "check":
            check_source(cfg)
            check_unsigned()
            check_tools()
            print("Reviewed source, matching unsigned workflow and Apple tooling verified.")
        elif command == "release":
            release(cfg)
        else:
            raise ReleaseError("Expected check, release or cleanup")
        return 0
    except ReleaseError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"::error::App Store release stopped ({type(error).__name__}); sensitive exception details are not printed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
