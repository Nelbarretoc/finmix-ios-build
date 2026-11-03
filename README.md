# FinMix iPhone build

Generated from the private FinMix repository (source commit `0a300886a93f51e61e58d01b25a0ec9745ff6d3e`)
by `scripts/publish-ios-shell.sh`. It holds the Xcode wrapper, compiled native
web bundle, and build/release tooling so the iPhone app can be built on GitHub's
free macOS runners. Do not edit files here; changes are overwritten.

The iOS build workflow creates unsigned simulator evidence. The separate App
Store workflow is manual only: it requires the same revision's successful
unsigned build, verified organization signing credentials, and explicit app
identifiers. It validates without uploading unless upload is selected. Apple
processing, review, and public availability are separate steps.
