# Third-party components

The orchestration source in this repository is MIT licensed. External programs,
drivers, firmware, builds and their licenses are not bundled or relicensed here.

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): Python
  dependency installed separately; retain its upstream MIT license and dependency notices.
- [HWTrace](https://github.com/POEG1726/HWTrace): separate telemetry project;
  follow its license and third-party notices.
- [DSView-Mod](https://github.com/POEG1726/DSView): separate DSView fork under
  its GPL terms. This repository invokes its CLI/IPC, includes no DSView source
  or binaries, and does not change that project's licensing.
- [HWiNFO](https://www.hwinfo.com/download/),
  [y-cruncher](https://www.numberworld.org/y-cruncher/), AMI AFU, TM5, NanoKVM
  firmware and any drivers remain subject to their own distribution/license terms.
- [ZenTimings-Mod](https://github.com/POEG1726/ZenTimings-Mod), forked from
  [ZenTimings](https://github.com/irusanov/ZenTimings), ZenStates-Core,
  inpoutx64 and the external custom export bundle must retain their applicable
  upstream licenses and notices. No export bundle or driver is distributed here.

The local calibration screenshots, raw experiment data and private deployment
configuration are excluded from the public source export.
