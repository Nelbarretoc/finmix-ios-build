# FinMix iPhone build

Generated from the private FinMix repository (source commit `dd76f6695f082b8298d3bd646412bec7cdaadb20`)
by `scripts/publish-ios-shell.sh`. It holds the Xcode wrapper, compiled native
web bundle, and build/release tooling so the iPhone app can be built on GitHub's
free macOS runners. Do not edit files here; changes are overwritten.

The iOS build workflow creates unsigned simulator evidence. The separate App
Store workflow is manual only: it requires a successful main unsigned build
from the last 24 hours with identical compiled app inputs and unsigned workflow,
verified organization signing credentials, and explicit app identifiers.
Only named release/capture tooling and repository metadata may differ from
that run. It validates without uploading unless upload is selected. Apple
processing, review, and public availability are separate steps.
