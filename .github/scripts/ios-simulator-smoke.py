#!/usr/bin/env python3
"""Bounded unsigned native welcome checks; no login, signing or delivery."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import struct
import subprocess
import sys
import tempfile
import time

BUNDLE = "com.th3rdgroup.finmix"
FAMILIES = {
    "iphone": ("iPhone", "iPhone 17 Pro", 4, 3),
    "ipad": ("iPad", "iPad Pro 13-inch (M5)", 5, 4),
}
UUID = re.compile(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}")

# Same pixel sampling, threshold and English/Spanish sign-in requirement as
# the original unsigned workflow, now shared by the two device families.
OCR = r'''import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 5,
      let divisor = Int(CommandLine.arguments[3]),
      let upper = Int(CommandLine.arguments[4]),
      let image = NSBitmapImageRep(data: try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) else { exit(1) }
let label = CommandLine.arguments[2]
var colored = 0
for y in stride(from: image.pixelsHigh / divisor, to: image.pixelsHigh * upper / divisor, by: 50) {
    for x in stride(from: image.pixelsWide / divisor, to: image.pixelsWide * upper / divisor, by: 50) {
        guard let pixel = image.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
        if pixel.redComponent < 0.96 || pixel.greenComponent < 0.96 || pixel.blueComponent < 0.96 { colored += 1 }
    }
}
guard colored >= 5 else { fputs("Capture is blank\n", stderr); exit(1) }
print("\(label) content samples: \(colored)")
guard let cgImage = image.cgImage else { exit(1) }
let recognition = VNRecognizeTextRequest()
recognition.recognitionLevel = .accurate
recognition.recognitionLanguages = ["en-US", "es-ES"]
try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([recognition])
let labels = recognition.results?.compactMap {
    $0.topCandidates(1).first?.string.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
} ?? []
guard labels.contains("sign in") || labels.contains("iniciar sesión") else {
    fputs("Native welcome did not reach the sign-in action\n", stderr)
    exit(1)
}
print("Native sign-in action verified")
'''


class SmokeError(Exception):
    """Only fixed phase details may reach public CI output."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def log(message):
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} Smoke: {message}", flush=True)


def tool(args, phase, timeout=120):
    env = {key: value for key, value in os.environ.items()
           if key not in {"FINMIX_CAPTURE_REVIEWER_BASE64", "GH_TOKEN", "GITHUB_TOKEN"}
           and not key.startswith(("APPLE_", "APP_STORE_CONNECT_"))}
    started = time.monotonic()
    log(f"{phase} START (limit {timeout}s)")
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"{phase} TIMEOUT after {time.monotonic() - started:.1f}s")
        raise SmokeError(f"{phase} timed out") from None
    except OSError:
        log(f"{phase} FAILED after {time.monotonic() - started:.1f}s")
        raise SmokeError(f"{phase} could not start") from None
    if result.returncode:
        log(f"{phase} FAILED after {time.monotonic() - started:.1f}s (exit {result.returncode})")
        raise SmokeError(f"{phase} failed")
    log(f"{phase} PASS after {time.monotonic() - started:.1f}s")
    return result.stdout.decode(errors="replace").strip()


def select_device(runtimes, devices, family):
    require(family in FAMILIES, "Unsupported simulator family")
    available = [runtime for runtime in runtimes
                 if runtime.get("isAvailable") is True
                 and re.fullmatch(r"com\.apple\.CoreSimulator\.SimRuntime\.iOS-\d+(?:-\d+){0,2}", runtime.get("identifier", ""))
                 and re.fullmatch(r"\d+(?:\.\d+){0,2}", runtime.get("version", ""))]
    require(available, "No available valid iOS runtime")
    runtime = max(available, key=lambda value: (
        tuple(int(part) for part in value["version"].split(".")), value["identifier"]))
    label, preferred, _, _ = FAMILIES[family]
    candidates = [device for device in devices.get(runtime["identifier"], [])
                  if device.get("isAvailable") is True and device.get("state") == "Shutdown"
                  and device.get("name", "").startswith(label)
                  and re.fullmatch(r"[A-Za-z0-9 .()_-]{1,100}", device["name"])
                  and UUID.fullmatch(device.get("udid", ""))]
    require(candidates, f"No available shutdown {label} on latest iOS runtime")
    device = min(candidates, key=lambda value: (
        value["name"] != preferred, value["name"], value["udid"]))
    return runtime, device


def cleanup(device, label):
    # This is a pre-existing runner simulator, so remove only our app and
    # shut it down; never delete/erase the runner's simulator definition.
    for operation in ("terminate", "uninstall", "shutdown"):
        args = ["xcrun", "simctl", operation, device]
        if operation != "shutdown":
            args.append(BUNDLE)
        try:
            tool(args, f"{label} cleanup {operation}", timeout=30)
        except Exception:
            log(f"{label} cleanup {operation} WARNING; continuing cleanup")


def boot_ready(device, label):
    # Cold simctl boot itself can wait on CoreSimulator data migration. Share
    # one generous deadline rather than prematurely timing out that phase.
    started = time.monotonic()
    tool(["xcrun", "simctl", "boot", device], f"{label} simulator boot", timeout=900)
    remaining = 900 - (time.monotonic() - started)
    if remaining <= 0:
        log(f"{label} boot readiness TIMEOUT; shared 900s boot budget exhausted")
        raise SmokeError(f"{label} boot readiness timed out")
    tool(["xcrun", "simctl", "bootstatus", device, "-b"],
         f"{label} boot readiness", timeout=remaining)


def smoke(app, output, family):
    app, output = Path(app).resolve(), Path(output).resolve()
    info = plistlib.loads((app / "Info.plist").read_bytes())
    require(info.get("CFBundleIdentifier") == BUNDLE, "Unexpected smoke app bundle")
    require(not output.exists(), "Smoke screenshot already exists")
    runtimes = json.loads(tool(["xcrun", "simctl", "list", "runtimes", "--json"], "runtime discovery"))
    devices = json.loads(tool(["xcrun", "simctl", "list", "devices", "available", "--json"], "device discovery"))
    runtime, device = select_device(runtimes["runtimes"], devices["devices"], family)
    label, _, divisor, upper = FAMILIES[family]
    udid = device["udid"]
    log(f"Selected {label}: {device['name']} / {udid} / iOS {runtime['version']} / {runtime['identifier']}")
    try:
        boot_ready(udid, label)
        tool(["xcrun", "simctl", "install", udid, str(app)], f"{label} app install", timeout=300)
        tool(["xcrun", "simctl", "launch", udid, BUNDLE], f"{label} app launch", timeout=300)
        log(f"{label} WebKit settling START (30s)")
        time.sleep(30)
        log(f"{label} WebKit settling PASS after 30s")
        tool(["xcrun", "simctl", "io", udid, "screenshot", str(output)], f"{label} screenshot")
        data = output.read_bytes()
        require(data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24, f"{label} screenshot is not PNG")
        width, height = struct.unpack(">II", data[16:24])
        require(width > 0 and height > 0, f"{label} screenshot dimensions invalid")
        log(f"{label} screenshot: {width} x {height}")
        with tempfile.TemporaryDirectory(prefix="finmix-smoke-") as temporary:
            checker = Path(temporary) / "check-welcome.swift"
            checker.write_text(OCR)
            proof = tool(["swift", str(checker), str(output), label, str(divisor), str(upper)],
                         f"{label} color and sign-in OCR", timeout=300)
        samples = re.search(rf"^{label} content samples: (\d+)$", proof, re.MULTILINE)
        require(samples and int(samples[1]) >= 5 and "Native sign-in action verified" in proof,
                f"{label} OCR proof missing")
        log(f"{label} content samples: {samples[1]}; Native sign-in action verified")
    except BaseException:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            log(f"{label} screenshot cleanup WARNING; preserving original failure")
        raise
    finally:
        cleanup(udid, label)


def interrupted(_number, _frame):
    raise SmokeError("Smoke interrupted by workflow cancellation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=FAMILIES, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        require(sys.platform == "darwin", "Smoke requires the existing macOS runner")
        smoke(args.app, args.output, args.family)
    except SmokeError as error:
        log(str(error))
        sys.exit(1)
    except Exception:
        log("Smoke inspection failed; raw tool data was not printed")
        sys.exit(1)
