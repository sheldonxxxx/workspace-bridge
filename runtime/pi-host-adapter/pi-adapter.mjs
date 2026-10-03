#!/usr/bin/env node
// Stable package-manager-provided entrypoint for the Pi host adapter.
//
// Installed as the `workspace-bridge-pi-adapter` command via the npm `bin`
// entry. This wrapper exists so global/package-manager installation exposes
// a stable lexical executable that `workspace-bridge adapter init/serve`
// can resolve and supervise. All runtime behavior lives in `main.mjs`; this
// file only re-exports its entrypoint so startup stays in one place.
import "./main.mjs";
