# Third-party references

This directory contains small source snapshots from upstream projects that were directly relevant to Dreem access. They are references, not project-owned source. Preserve attribution and verify each upstream license before redistribution. Links to every upstream project live in `docs/references.md`.

License scope and captured-commit details are recorded in the repository's [third-party notices](../THIRD_PARTY_NOTICES.md#upstream-source-snapshots).

`source-snapshots/` contains those upstream trees, captured at these upstream commits:

- DreemEEG — https://github.com/jabituyaben/DreemEEG @ `084172e1565e75327eaf80e44b949602da585cf6` (branch `main`)
- dreem-standalone — https://github.com/Dreem-Organization/dreem-standalone @ `c103b70fe9bff3187e7b3d0f3d0982a21975f6a8` (branch `main`)
- lis2hh12-pid — https://github.com/STMicroelectronics/lis2hh12-pid @ `09c28df1a67e2d85e4ea9f6447e6a9a983de2f0e`; driver/header and BSD-3-Clause license, with LF-normalized snapshot hashes in `provenance.json`. Used by the [motion acquisition integration](../development/motion-lifecycle.md).

The snapshots intentionally preserve upstream sample values. `DreemEEG` includes a sample device MAC address, and `dreem-standalone` includes executable demo database/broker passwords. They are not credentials from this project; do not reuse the demo passwords in a deployment.
