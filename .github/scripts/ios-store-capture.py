#!/usr/bin/env python3
"""Authenticated fictional-reviewer screenshots; credentials never enter child argv/env.

Prepare the capture-only secret privately on the VPS, then run capture on the
existing GitHub macOS runner. No account data is changed and no message is sent.
"""
import argparse
import base64
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

ORIGIN = "https://finmix.th3rdgroup.com"
BUNDLE = "com.th3rdgroup.finmix"
SENTINEL = b"FINMIX_DEBUG_CAPTURE_HARNESS_V1"
ROUTES = ("home", "bills", "calendar", "tell", "money", "taxes")
SECRET = "FINMIX_CAPTURE_REVIEWER_BASE64"


class CaptureError(Exception):
    """Only fixed phase descriptions may reach public CI output."""


def require(condition, phase):
    if not condition:
        raise CaptureError(phase)


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def validate_reviewer(value):
    require(isinstance(value, dict), "reviewer schema")
    email, password, purpose = (value.get(k) for k in ("email", "password", "purpose"))
    require(isinstance(email, str) and email == email.strip().lower() and email.endswith(".invalid") and "review" in email, "fictional reviewer identity")
    require(isinstance(password, str) and 12 <= len(password) <= 200, "reviewer credential schema")
    require(isinstance(purpose, str) and "fictional" in purpose.lower() and "review" in purpose.lower(), "fictional reviewer provenance")
    return email


def canonical_fixture(state, money, taxes):
    state = {key: value for key, value in state.items() if key != "serverTime"}
    def normalize(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    return json.dumps(normalize({"state": state, "money": money, "taxes": taxes}), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def check_canonical():
    # These values are public, fictional test content, never reviewer data.
    state = {"serverTime": "ignored", "profile": {"name": "Revisión – Peña 🐦", "verified": False, "consent": True, "optional": None}, "items": [{"amount": 15000, "decimal": 0.5, "integral": 1.0, "zero": 0, "limit": 100000000, "note": 'https://example.invalid/a/b\n"quoted"\ttext'}]}
    fixture = json.loads(canonical_fixture(state, [], {"taxYear": 2025, "documents": {}}))
    expected = hashlib.sha256(canonical_fixture(state, [], {"taxYear": 2025, "documents": {}})).hexdigest()
    path = Path(os.environ["RUNNER_TEMP"]) / "finmix-capture-canonical.json"
    private_write(path, json.dumps({"fixture": fixture, "expected": expected}, ensure_ascii=False).encode())
    program = '''import Foundation
import CryptoKit
let input = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
let bytes = try JSONSerialization.data(withJSONObject: input["fixture"]!, options: [.sortedKeys, .withoutEscapingSlashes])
let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
guard digest == input["expected"] as? String else { exit(1) }
'''
    try:
        tool(["swift", "-e", program, str(path)], timeout=120)
    finally:
        path.unlink(missing_ok=True)
    print("Python/Swift fixture canonicalization agrees for Unicode, booleans, null, numbers and escaped text.")


def validate_fixture(actor, state, email):
    expected = "acct:" + hashlib.sha256(f"account:{email}".encode()).hexdigest()[:32]
    require(actor.get("id") == expected and actor.get("email") == email and actor.get("role") == "customer" and actor.get("roles") == ["customer"], "reviewer role and household")
    profile = state.get("profile", {})
    require(profile.get("id") == expected and profile.get("email") == email, "reviewer profile")
    require(profile.get("phone") == "" and profile.get("messagingConsent") is False and profile.get("contactMethod") == "inapp", "reviewer external contact disabled")
    require(state.get("items") and all(item.get("ownerId") == expected and "sourceDocument" not in item for item in state["items"]), "reviewer seeded bills")
    require(all(state.get(key) == [] for key in ("connections", "bankLinks", "grants")), "reviewer isolation")
    return expected


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prepare(reviewer_path, output_path):
    output = Path(output_path).resolve()
    repo = Path(__file__).resolve().parent.parent
    require(not output.is_relative_to(repo), "private output must be outside repository")
    reviewer = json.loads(Path(reviewer_path).read_text())
    email = validate_reviewer(reviewer)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(ORIGIN + "/api" + path, data=data, headers={"Origin": ORIGIN, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "FinMixNativeCapture/1.0"})
        with opener.open(req, timeout=25) as response:
            require(response.status == 200 and response.url == req.full_url and response.headers.get_content_type() == "application/json", "reviewer API response")
            return json.load(response)

    try:
        login = request("/auth/password", {"email": email, "password": reviewer["password"]})
        require(login.get("mustChangePassword") is False, "reviewer password ready")
        state = request("/state")
        actor = validate_fixture(login.get("actor", {}), state, email)
        year = dt.datetime.now(ZoneInfo(state["profile"]["timezone"])).year - 1
        fixture = canonical_fixture(state, request("/money"), request(f"/taxes/{year}"))
        payload = {"reviewer": reviewer, "expectedActorId": actor, "taxYear": year, "expectedFixtureSha256": hashlib.sha256(fixture).hexdigest()}
        private_write(output, base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()))
    finally:
        try:
            request("/auth/logout", {})
        except Exception:
            pass
    print("Private capture payload prepared; no household data changed.")


def bootstrap():
    encoded = os.environ.pop(SECRET, "")
    require(encoded and re.fullmatch(r"[A-Za-z0-9+/=]+", encoded), "capture secret format")
    data = base64.b64decode(encoded, validate=True)
    payload = json.loads(data)
    validate_reviewer(payload.get("reviewer"))
    require(re.fullmatch(r"acct:[0-9a-f]{32}", payload.get("expectedActorId", "")), "capture household identity")
    require(re.fullmatch(r"[0-9a-f]{64}", payload.get("expectedFixtureSha256", "")), "capture fixture digest")
    private_write(Path(os.environ["RUNNER_TEMP"]) / "finmix-capture/private.json", data)
    print("Private capture bootstrap ready.")


def tool(args, timeout=120, tolerate=False, phase="native tool"):
    env = {key: value for key, value in os.environ.items() if key not in {SECRET, "GH_TOKEN", "GITHUB_TOKEN"} and not key.startswith("APPLE_") and not key.startswith("APP_STORE_CONNECT_")}
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CaptureError(f"{phase} timed out") from None
    if result.returncode and not tolerate:
        raise CaptureError(f"{phase} failed")
    return result.stdout.decode(errors="replace").strip()


def verify_release(app):
    app = Path(app)
    info = plistlib.loads((app / "Info.plist").read_bytes())
    executable = info.get("CFBundleExecutable", "")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", executable), "release executable name")
    data = (app / executable).read_bytes()
    require(SENTINEL not in data and b"NativeCaptureHarness" not in data, "debug harness present in release executable")
    print("Release executable excludes capture sentinel and class symbols.")


def wait_status(path, expected, timeout=80, phase="native readiness"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text())
            require(value.get("state") != "failed", f"{phase} failed")
            if all(value.get(key) == item for key, item in expected.items()):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise CaptureError(f"{phase} timed out")


def capture(app, output):
    require(sys.platform == "darwin", "capture requires existing macOS runner")
    require(os.environ.get("GITHUB_REPOSITORY") == "Nelbarretoc/finmix-ios-build" and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and os.environ.get("GITHUB_REF") == "refs/heads/main", "capture dispatch scope")
    source = Path("SOURCE_COMMIT").read_text().strip()
    require(re.fullmatch(r"[0-9a-f]{40}", source) and source == os.environ.get("FINMIX_CAPTURE_SOURCE_COMMIT"), "capture source revision")
    app = Path(app).resolve()
    info = plistlib.loads((app / "Info.plist").read_bytes())
    require(info.get("CFBundleIdentifier") == BUNDLE and SENTINEL in (app / info["CFBundleExecutable"]).read_bytes(), "debug capture app")
    private = Path(os.environ["RUNNER_TEMP"]) / "finmix-capture/private.json"
    payload = private.read_bytes()
    output = Path(output).resolve()
    require(not output.exists(), "capture output already exists")
    output.mkdir(mode=0o700, parents=True)
    runtimes = json.loads(tool(["xcrun", "simctl", "list", "runtimes", "--json"]))["runtimes"]
    ios = [r for r in runtimes if r.get("isAvailable") and r["identifier"].startswith("com.apple.CoreSimulator.SimRuntime.iOS-")]
    require(ios, "available iOS runtime")
    runtime = max(ios, key=lambda r: tuple(int(x) for x in r["version"].split(".")))["identifier"]
    types = json.loads(tool(["xcrun", "simctl", "list", "devicetypes", "--json"]))["devicetypes"]
    choices = [
        ("iphone-medium", ["iPhone 17 Pro", "iPhone 17", "iPhone 16 Pro"], (1206, 2622)),
        ("ipad-13", ["iPad Pro 13-inch (M5)", "iPad Pro 13-inch (M4)"], (2064, 2752)),
    ]
    captures = []
    try:
        for label, preferred, dimensions in choices:
            print(f"Capture phase: {label} / creating simulator", flush=True)
            device_type = next((t for name in preferred for t in types if t["name"] == name), None)
            require(device_type, "required simulator size unavailable")
            device = tool(["xcrun", "simctl", "create", "FinMixCapture-" + label, device_type["identifier"], runtime])
            require(re.fullmatch(r"[A-Fa-f0-9-]{36}", device), "simulator identity")
            directory = None
            try:
                print(f"Capture phase: {label} / booting simulator (maximum 900 seconds)", flush=True)
                tool(["xcrun", "simctl", "boot", device], phase=f"{label} simulator boot")
                tool(["xcrun", "simctl", "bootstatus", device, "-b"], timeout=900, phase=f"{label} simulator boot readiness")
                print(f"Capture phase: {label} / installing app", flush=True)
                tool(["xcrun", "simctl", "ui", device, "appearance", "light"])
                tool(["xcrun", "simctl", "status_bar", device, "override", "--time", "9:41", "--dataNetwork", "wifi", "--wifiMode", "active", "--wifiBars", "3", "--batteryState", "charged", "--batteryLevel", "100"])
                tool(["xcrun", "simctl", "install", device, str(app)])
                container = Path(tool(["xcrun", "simctl", "get_app_container", device, BUNDLE, "data"]))
                directory = container / "Documents/FinMixCapture"
                private_write(directory / "private.json", payload)
                tool(["xcrun", "simctl", "launch", device, BUNDLE])
                print(f"Capture phase: {label} / verifying fictional reviewer", flush=True)
                wait_status(directory / "status.json", {"state": "authenticated"}, phase=f"{label} reviewer authentication")
                require(not (directory / "private.json").exists(), "private input not consumed")
                for language in ("en", "es"):
                    for route in ROUTES:
                        print(f"Capture phase: {label} / {language} / {route}", flush=True)
                        command = {"id": uuid.uuid4().hex, "route": route, "language": language}
                        private_write(directory / "command.next", json.dumps(command).encode())
                        (directory / "command.next").replace(directory / "command.json")
                        wait_status(directory / "status.json", {"state": "ready", **command}, phase=f"{label} {language} {route} readiness")
                        filename = f"{label}-{language}-{route}.jpg"
                        temporary = directory / "capture.png"
                        tool(["xcrun", "simctl", "io", device, "screenshot", "--type=png", str(temporary)])
                        tool(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "95", str(temporary), "--out", str(output / filename)])
                        temporary.unlink()
                        size = tool(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(output / filename)])
                        actual = tuple(int(re.search(key + r": (\d+)", size).group(1)) for key in ("pixelWidth", "pixelHeight"))
                        require(actual == dimensions, "native screenshot dimensions")
                        captures.append({"file": filename, "language": language, "route": route, "width": actual[0], "height": actual[1], "sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest()})
            finally:
                print(f"Capture phase: {label} / cleaning up simulator", flush=True)
                if directory is not None:
                    try:
                        finish = {"id": uuid.uuid4().hex, "route": "finish", "language": "en"}
                        private_write(directory / "command.next", json.dumps(finish).encode())
                        (directory / "command.next").replace(directory / "command.json")
                        wait_status(directory / "status.json", {"state": "finished", "id": finish["id"]}, timeout=5)
                    except Exception:
                        pass
                tool(["xcrun", "simctl", "terminate", device, BUNDLE], tolerate=True)
                tool(["xcrun", "simctl", "uninstall", device, BUNDLE], tolerate=True)
                tool(["xcrun", "simctl", "shutdown", device], tolerate=True)
                tool(["xcrun", "simctl", "delete", device], tolerate=True)
        require(len(captures) == 24, "complete native screenshot set")
        manifest = {"sourceCommit": source, "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "Actual iOS simulator app, bundled production UI and authenticated fictional reviewer", "configuration": "Debug simulator; capture harness excluded from Release", "physicalDeviceAcceptance": False, "readyForVisualReview": True, "captures": captures}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print("24 native screenshots captured; visual review remains required.")
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        private.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reviewer", required=True)
    prep.add_argument("--output", required=True)
    sub.add_parser("bootstrap")
    sub.add_parser("check-canonical")
    run = sub.add_parser("capture")
    run.add_argument("--app", required=True)
    run.add_argument("--output", required=True)
    check = sub.add_parser("verify-release")
    check.add_argument("--app", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare": prepare(args.reviewer, args.output)
        elif args.command == "bootstrap": bootstrap()
        elif args.command == "check-canonical": check_canonical()
        elif args.command == "capture": capture(args.app, args.output)
        else: verify_release(args.app)
    except Exception as error:
        print("Capture stopped: " + (str(error) if isinstance(error, CaptureError) else "private operation failed"), file=sys.stderr)
        sys.exit(1)
