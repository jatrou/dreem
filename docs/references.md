# Public References

This file replaces copied upstream README snapshots. Keep links here instead of vendoring other projects' documentation into this archive.

## Device And Hardware

- Dreem 2 FCC filing: https://fccid.io/2AH2Q-DREEM2
- MajorInput Dreem teardown: https://www.majorinput.co.uk/post/a-closer-look-at-the-dreem-eeg-headband
- Dreem Wi-Fi behavior/support context: https://support.dreem.com/
- ST LIS2HH12 component datasheet: https://www.st.com/resource/en/datasheet/lis2hh12.pdf
- ST LIS2HH12 public driver source: https://github.com/STMicroelectronics/lis2hh12-pid/tree/09c28df1a67e2d85e4ea9f6447e6a9a983de2f0e

## Data And Research Context

- Dreem sleep staging / device background: https://academic.oup.com/sleep/article/43/11/zsaa097/5841249
- Dreem headband layout figure: https://www.researchgate.net/figure/Ilustration-of-the-Dreem-2-EEG-Headband-with-the-layout-of-the-device-left-and-a_fig1_374860583
- Dreamento toolbox context: https://github.com/dreamento/dreamento
- Wearipedia Dreem guide: https://github.com/Stanford-Health/Wearipedia

## Public Dreem / BLE Prior Art

- DreemEEG: https://github.com/jabituyaben/DreemEEG
- Dreem Organization: https://github.com/Dreem-Organization
- dreem-learning-open: https://github.com/Dreem-Organization/dreem-learning-open
- dreem-learning-evaluation: https://github.com/Dreem-Organization/dreem-learning-evaluation

## Boot, Recovery, And Hardware Tools

- U-Boot: https://source.denx.de/u-boot/u-boot
- U-Boot documentation: https://docs.u-boot.org/
- NXP UUU: https://github.com/nxp-imx/mfgtools
- imx_usb_loader: https://github.com/boundarydevices/imx_usb_loader
- usbarmory: https://github.com/usbarmory/usbarmory
- Tigard: https://github.com/tigard-tools/tigard
- Bus Pirate firmware/docs: https://firmware.buspirate.com/ and https://docs.buspirate.com/

## Community Leads (Dreem owner forums / Discord)

Salvaged pointers from other Dreem 2 owners working the same orphaned-device problem. The durable findings independently corroborate this project's conclusions (raw EEG is processed on-headband, not streamed over BLE; the cloud/report path is dead but the device still stores data locally).

- Archived Dreem support pages via Wayback: http://web.archive.org/web/20211207034635/https://support.dreem.com/hc/en-us
- 2017 Dreem whitepaper (originally at `s3-us-west-1.amazonaws.com/rythm/rythm-dreem-whitepaper.pdf`) — recover via the Wayback Machine.
- A community "modified APK" reportedly re-enables account creation, Wi-Fi setup, and firmware update against the dead backend, so sleep tracking still works locally (reports remain blocked by lost server permissions). Shared owner-to-owner on Discord; not vendored here.
- Reported lead worth verifying: with the modified APK connected, night data is said to be readable "from the root folder on the phone," and the headband still reaches its own (separate) firmware-update server. In-app CSV/OSCAR export is broken.

## Local Outcome

These references were checked for extraction clues. They helped with hardware and protocol orientation, but did not provide a public path to stored raw Dreem 2 `.h5` recovery.
