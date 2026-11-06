#!/usr/bin/env python3
"""Exercise native bridges with generated fictional content; no sign-in or send."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import struct
import sys
import time

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("finmix_simulator_smoke", Path(__file__).with_name("ios-simulator-smoke.py"))
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)

MARKER = "FINMIX_DEBUG_FEATURE_SMOKE_V1"
CHECKS = (
    "bridgeReady", "englishPhotoRead", "spanishPhotoRead", "exifRotatedPhotoRead",
    "invalidInputsRejected", "protectedShareFileMatches", "shareSheetPresented",
    "duplicateShareRejected", "cancelReturnsFalseAndCleans", "retryAndErrorCleanup",
)
PHASES = {"bridge", "english-photo", "spanish-photo", "rotated-photo", "invalid-inputs",
          "share-presentation", "share-file", "share-protection", "share-anchor", "share-busy",
          "cancel-cleanup", "error-cleanup", "complete"}


def validate_proof(value):
    simulator.require(isinstance(value, dict) and value.get("harness") == MARKER, "Native bridge proof identity is missing")
    simulator.require(value.get("state") == "passed" and value.get("phase") == "complete", "Native bridge checks did not finish")
    simulator.require(all(value.get(key) is True for key in CHECKS), "A required native bridge check did not pass")
    simulator.require(value.get("originalHandwritingVerified") is False and value.get("saveToFilesVerified") is False
                      and value.get("completionCallbacks") == "simulated", "Native bridge proof overstates physical acceptance")
    return {key: value[key] for key in (*CHECKS, "originalHandwritingVerified", "saveToFilesVerified", "completionCallbacks")}


def read_status(path, allow_failure=False):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    simulator.require(isinstance(value, dict) and value.get("harness") == MARKER, "Unexpected native bridge status")
    phase = value.get("phase")
    simulator.require(phase in PHASES, "Unknown native bridge phase")
    if not allow_failure:
        simulator.require(value.get("state") != "failed", f"Native bridge failed at {phase}")
    return value


def failure_proof(value):
    result = {key: value[key] for key in CHECKS if value.get(key) is True}
    result["phase"] = value["phase"]
    for key in ("rootControllerClass", "presentedControllerClass", "fileProtection", "shareCallState"):
        text = value.get(key)
        if isinstance(text, str) and re.fullmatch(r'[A-Za-z0-9_.: ()"]{1,120}', text):
            result[key] = text
    if isinstance(value.get("windowAttached"), bool):
        result["windowAttached"] = value["windowAttached"]
    return result


def check(app, output, family):
    app, output = Path(app).resolve(), Path(output).resolve()
    simulator.require(not output.exists(), "Native bridge output already exists")
    info = plistlib.loads((app / "Info.plist").read_bytes())
    simulator.require(info.get("CFBundleIdentifier") == simulator.BUNDLE, "Unexpected native bridge app")
    executable = info.get("CFBundleExecutable", "")
    simulator.require(executable == "App" and MARKER.encode() in (app / executable).read_bytes(), "Native bridge checks require a Debug simulator app")
    runtimes = json.loads(simulator.tool(["xcrun", "simctl", "list", "runtimes", "--json"], "feature runtime discovery"))
    devices = json.loads(simulator.tool(["xcrun", "simctl", "list", "devices", "available", "--json"], "feature device discovery"))
    runtime, device = simulator.select_device(runtimes["runtimes"], devices["devices"], family)
    udid, label = device["udid"], simulator.FAMILIES[family][0]
    output.mkdir(mode=0o700, parents=True)
    screenshot = output / "share-sheet.png"
    captured = False
    try:
        simulator.boot_ready(udid, label)
        simulator.tool(["xcrun", "simctl", "install", udid, str(app)], f"{label} feature app install", timeout=300)
        container = Path(simulator.tool(["xcrun", "simctl", "get_app_container", udid, simulator.BUNDLE, "data"], f"{label} feature container"))
        directory = container / "Documents/FinMixNativeSmoke"
        simulator.require(not directory.exists(), "Native bridge test container is not fresh")
        previous = os.environ.pop("SIMCTL_CHILD_FINMIX_NATIVE_FEATURE_SMOKE", None)
        try:
            os.environ["SIMCTL_CHILD_FINMIX_NATIVE_FEATURE_SMOKE"] = "1"
            simulator.tool(["xcrun", "simctl", "launch", udid, simulator.BUNDLE], f"{label} feature app launch", timeout=300)
        finally:
            os.environ.pop("SIMCTL_CHILD_FINMIX_NATIVE_FEATURE_SMOKE", None)
            if previous is not None:
                os.environ["SIMCTL_CHILD_FINMIX_NATIVE_FEATURE_SMOKE"] = previous
        deadline = time.monotonic() + 240
        last_phase = None
        while time.monotonic() < deadline:
            value = read_status(directory / "status.json", allow_failure=True)
            if value is not None:
                if value.get("state") == "failed":
                    detail = failure_proof(value)
                    receipt = {"status": "FAIL", "checkedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "family": family, "device": device["name"], "runtime": runtime["identifier"],
                               "partialChecks": detail, "originalHandwritingVerified": False, "saveToFilesVerified": False}
                    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
                    simulator.log("Native bridge failure evidence: " + json.dumps(detail))
                    try:
                        simulator.tool(["xcrun", "simctl", "io", udid, "screenshot", str(output / "failure.png")],
                                       f"{label} fictional failure screenshot", timeout=30)
                    except simulator.SmokeError:
                        simulator.log(f"{label} failure screenshot unavailable")
                    raise simulator.SmokeError(f"Native bridge failed at {value['phase']}")
                if value["phase"] != last_phase:
                    last_phase = value["phase"]
                    simulator.log(f"{label} native bridge phase: {last_phase}")
                if value.get("state") == "share-presented" and not captured:
                    simulator.tool(["xcrun", "simctl", "io", udid, "screenshot", str(screenshot)], f"{label} actual share-sheet screenshot")
                    image = screenshot.read_bytes()
                    simulator.require(image[:8] == b"\x89PNG\r\n\x1a\n" and len(image) >= 24, "Share-sheet screenshot is not PNG")
                    width, height = struct.unpack(">II", image[16:24])
                    simulator.require(width > 0 and height > 0, "Share-sheet screenshot dimensions are invalid")
                    (directory / "continue.txt").write_text("continue")
                    captured = True
                if value.get("state") == "passed":
                    proof = validate_proof(value)
                    simulator.require(captured, "Actual share-sheet screenshot is missing")
                    receipt = {"status": "PASS", "checkedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(), "family": family,
                               "device": device["name"], "runtime": runtime["identifier"], "checks": proof,
                               "screenshot": screenshot.name, "width": width, "height": height,
                               "configuration": "Debug simulator; exact production plugin source with debug-only driver",
                               "signedInstallerTested": False, "productionRecordCreated": False, "messageSent": False}
                    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
                    simulator.log(f"{label} native bridge checks PASS; physical handwriting and Save to Files remain unverified")
                    return
            time.sleep(0.25)
        raise simulator.SmokeError(f"{label} native bridge checks timed out")
    finally:
        simulator.cleanup(udid, label)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=simulator.FAMILIES, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, simulator.interrupted)
    signal.signal(signal.SIGINT, simulator.interrupted)
    try:
        simulator.require(sys.platform == "darwin", "Native bridge checks require the existing macOS runner")
        check(args.app, args.output, args.family)
    except simulator.SmokeError as error:
        simulator.log(str(error))
        sys.exit(1)
    except Exception:
        simulator.log("Native bridge checks failed; raw data was not printed")
        sys.exit(1)
