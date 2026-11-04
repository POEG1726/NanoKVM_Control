# ZenTimings-Mod integration

Use [our ZenTimings-Mod fork](https://github.com/POEG1726/ZenTimings-Mod), with
the [headless release bundle](https://github.com/POEG1726/ZenTimings-Mod/releases/latest).
This is a separate project retaining upstream GPL licensing. NanoKVM Control
invokes the exporter; it does not bundle or relicense its source, DLLs or drivers.

## Prepare the host bundle

1. Download the `windows-x64-headless.zip` asset and `SHA256SUMS.txt` from the same
   release. Verify the ZIP's SHA256 before using it.
2. Extract into `external/zentimings-headless/`, or another private tools folder.
3. Set `zentimings_cli_bundle` in your local `config/host.json` to that folder.

The folder must contain these files at its top level:

```text
build-manifest.json
ZenTimings.Export.exe
ZenStates-Core.dll
inpoutx64.dll
ZenTimings.Export.exe.config
```

Keep the release's license files and notices as well. The manifest's `files`
array describes exactly the four runtime files with name, size and SHA256.
`deploy` checks the bundle, uploads to a versioned target folder, checks the
uploaded hashes and selects that executable in target `config.json`. The existing
GUI installation is not replaced. `external/` is ignored by Git.

## Target contract

The target invokes one command per run:

```text
ZenTimings.Export.exe --export-json UNIQUE_RUN_FILE --exit
```

The output directory must exist, and the filename must be unused. The live JSON
must have `capture_mode=one_shot_cli` and the complete sections checked by
`target/desktop_capture.py`. Synthetic self-test output is rejected as live
evidence. The target runs no ZenTimings GUI or logon autostart.

The current exporter requires Windows x64, a compatible .NET Framework 4.x runtime
(net472 target), administrator privileges, existing upstream device access and
the hardcoded 9950X3D/B850M identity. It does not install/update drivers, configure
services or flash firmware. See the fork's
[CLI guide](https://github.com/POEG1726/ZenTimings-Mod/blob/main/CLI/README.md)
for the precise device behavior and offline/live distinction.

This JSON supplies an independent SMU VDDP sanity check and configuration evidence.
It does not replace the F12d exact saved-request dump parser required to distinguish
neighboring 5mV settings, or establish physical voltage accuracy or stability.
