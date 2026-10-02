# Firmware source and interface findings

Verified from the saved firmware on October 2, 2026. Static findings below are
not claims of current device state. Raw inputs and decompiled vendor code are
kept outside this repository.

## Identity and source matching

| Item | Evidence |
| --- | --- |
| Stock archive | `firmware_FEMTO_4.7.11_production.tar.bz2`, 25,636,230 bytes |
| Archive SHA-256 | `6e6356b51cf197a63fc73acfd7580e5f009c97569d6e716d82767ef13c15f17d` |
| Kernel | Linux 4.1.15, ARMv7, PREEMPT, module versions enabled |
| Kernel label in INSTALL | `4.1.15_2.1.0_nano_ddr_v37` |
| Kernel raw SHA-256 | `e15f659afdffab3fde7c997883475e4c95b5d3cdc1fbcd23c76365ee4cd52dcb` |
| Embedded config SHA-256 | `8ca22c0f9ffd67872b86bf6b821e871b64ac5cae23e1c7234abcf170f1d68a8f` |
| DTB SHA-256 | `d3bef75091e2d812ca4b02126c11008b0a698ba426b9d0a05f856850666231f9` |
| nano_core SHA-256 | `dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a` |
| Build environment strings | GCC 7.4.0; `Buildroot 2019.02-48930-g364d59efe4` |
| Userspace runtime | uClibc-ng 1.0.31; `nano_core` needs `libc.so.0` |
| Algorithm build label | `algo_dreem-Nerves-3.6.0.1` |

The kernel gzip payload starts at byte 18,168 of `zImage`; the decompressed
image is 9,330,688 bytes. Its embedded IKCONFIG and kallsyms are intact. Symbol
recovery found 61,634 entries and a kernel base of `0x80008000`, consistent
with `CONFIG_PAGE_OFFSET=0x80000000` and the disassembled ARM code.

Public source baseline:
[NXP linux-imx rel_imx_4.1.15_2.1.0_ga](https://github.com/nxp-imx/linux-imx/tree/rel_imx_4.1.15_2.1.0_ga),
commit `30278abfe0977b1d2f065271ce1ea23c0e2d1b6e`.
The only enabled config identifiers missing from its Kconfig definitions are
`SENSORS_ADS1296`, `DREEM_DDR`, and `SND_SOC_IMX_AIC31XX`.

This is a baseline, not an exact source match. The binary also exports Dreem
SDMA data paths absent from upstream `drivers/dma/imx-sdma.c` and modifies bus
frequency/audio behavior. The exact Buildroot commit suffix did not resolve in
the public Buildroot repository; search results alone cannot establish that a
private fork is lost or publicly available.

The root filesystem has no matching complete source package. We still lack
the original `nano_core`, Nerves, custom board-driver source, Buildroot board
configuration, and any separately flashed controller source. Decompiled C is
an analysis aid rather than recovered original source.

## Kernel export verification

Recovered 7,041 exported symbol CRCs from `__kcrctab_*`, classified against
the normal/GPL export ranges. Checked all 41 archived modules with version
tables:

- 1,971 references to kernel exports match, with zero kernel CRC mismatches.
- 217 module-to-module references match.
- `mac80211.ko` has 63 CRC mismatches against archived module exports and five
  unresolved references. This prevents treating all bundled modules as a
  consistent rebuild target. It does not show that this module was loaded.

`module_layout` CRC is `0xae63ff3f`; `printk` is `0x27e1a049`. These are source
matching evidence, not a license to substitute checksums for ABI validation.

### Rebuilt upstream comparison

The NXP baseline built successfully with the recovered config passed through
`olddefconfig`, GCC 7.3.0, and host `-fcommon` for the old bundled dtc. Generated
outputs were kept outside the unchanged upstream source checkout.

| Export comparison | Count |
| --- | ---: |
| Dreem kernel exports | 7,041 |
| Rebuilt NXP exports | 7,038 |
| Matching CRC and export class | 7,036 |
| Shared names with different CRCs | 2 |
| Dreem-only names | 3 |
| NXP-only names | 0 |

The two differences are `request_bus_freq` (stock `0x5f9b360b`, baseline
`0x3f59d6dd`) and `release_bus_freq` (stock `0xd65ed1bb`, baseline `0x13bec41d`).
Dreem's added names are `ads_data_sem`, `sdma_ads_user_buffer`, and
`sdma_queue_head`. `module_layout` matches exactly. This narrows interface
reconstruction substantially, while modifications behind matching signatures
and board initialization remain unproven.

## EEG device interface

Both the kernel and `nano_core` confirm `/dev/eeg_cdev` is the ADC acquisition
interface. Function addresses apply only to the above kernel/core hashes.

| Kernel function | Address | Observed behavior |
| --- | --- | --- |
| `ads1296_probe` | `0x8041d76c` | Registers `eeg_cdev`; selects IRQ or SDMA implementation from hardware version |
| `ads1296_open` | `0x8041e4a0` | Stops an existing acquisition thread and initializes ADC registers |
| `ads1296_sdma_open` | `0x8041e868` | Changes clocks, toggles ADC power, and initializes registers |
| `ads1296_read` | `0x8041d620` | Consumes the shared queue; copies a fixed 16-byte record |
| `ads1296_sdma_read` | `0x8041d3fc` | Consumes SDMA queue; copies a fixed 16-byte record |
| `ads1296_ioctl` | `0x8041e614` | Plain command numbers, not encoded Linux `_IOC` values |
| `ads1296_sdma_ioctl` | `0x8041daa0` | SDMA command implementation |
| `dreem_ddr_ioctl` | `0x8041c804` | Command 7 requests a high bus-frequency mode; other commands log unsupported but return zero |

EEG commands confirmed in both kernel and userspace are stop `0`, start `1`,
and error-counter read `4` with a pointer to a 32-bit value. Command `5`
configures an ADC test signal in the kernel. Other values can return success
without doing anything. The normal `nano_core` wrappers are at `0x0008fa48`
(start), `0x0008facc` (stop), and `0x0008fb4c` (counter).

**A second open/read is not a passive tap.** It can reset the ADC and consume
samples intended for the recorder. The quality example uses native files,
whose float rows are a different interface from these 16-byte driver records.
Do not use `cat`, generic probes, or the example against this device node.

The driver requests Linux GPIO numbers 35 (ADC power), 90 (chip select), and
34 (data ready). These are software identifiers, not connector pin numbers.

## Existing sensor interfaces

Identified from calls in the stock core, with corroborating component documents:

| Component | Linux bus / address | Binary evidence |
| --- | --- | --- |
| LIS2HH12 accelerometer | `/dev/i2c-3`, `0x1e` | `0x0008fc20`/`0x0008fd94` check WHO_AM_I register `0x0f` for `0x41` |
| MAX30101 optical sensor | `/dev/i2c-3`, `0x57` | `0x00090758` opens the bus and reads part ID register `0xff` |
| CAP1298 touch/slide controller | `/dev/i2c-3`, `0x28` | `0x00097bd4` checks product register `0xfd` for `0x71`; associated setup error names CAP1298 |

The stock binary has branches for multiple hardware variants. These findings
identify supported code paths, not a fresh presence check on a particular unit.
The sensor bus has existing owners; reading a FIFO or rewriting configuration
can affect the recorder. Avoid broad I2C scans and forced address claims.

ST supplies a [platform-independent LIS2HH12 driver with source](https://github.com/STMicroelectronics/lis2hh12-pid)
under BSD-3-Clause. Its [datasheet](https://www.st.com/resource/en/datasheet/lis2hh12.pdf)
confirms the identity register. Other primary references are
[Analog Devices MAX30101](https://www.analog.com/en/products/MAX30101.html)
and [Microchip CAP1298](https://www.microchip.com/en-us/product/CAP1298).

## Bus and pad map from the device tree

| Linux alias | SoC controller | Muxed pads | Present configuration |
| --- | --- | --- | --- |
| i2c0 | I2C1 | UART4_TX_DATA / UART4_RX_DATA | Enabled; PMIC at `0x08` |
| i2c1 | I2C2 | CSI_HSYNC / CSI_VSYNC | Enabled; codec/RTC nodes for several board variants |
| i2c2 | I2C3 | Not enabled | Disabled |
| i2c3 | I2C4 | LCD_DATA03 / LCD_DATA02 | Enabled; sensor clients opened by userspace |
| spi0 | ECSPI1 | LCD_DATA23 MISO, LCD_DATA22 MOSI, LCD_DATA20 SCLK, LCD_DATA12 RDY | Enabled; EEG child, 20 MHz maximum in DT |
| serial0 | UART1 | UART1_TX_DATA / UART1_RX_DATA | Enabled; console in chosen stdout path |
| serial1 | UART2 | UART2_TX_DATA / RX_DATA / RTS_B / CTS_B | Enabled; not an established free port |

Pad tuples were matched to NXP's `imx6ul-pinfunc.h` and `imx6ull-pinfunc.h`.
SPI1–3 and other UART aliases are disabled in this DT. A disabled controller
is not necessarily physically accessible or free of board conflicts. Voltage,
pull-ups, connector routing, and power budget still require physical evidence.

## Rebuilding boundary

Independent static ARM programs are built and emulation-tested here. The NXP
baseline kernel is rebuilt, and its exported interfaces closely match stock.
A complete Dreem replacement kernel, the custom acquisition drivers, U-Boot,
and Nerves have not been rebuilt and qualified. The current work makes those
gaps explicit and provides inputs for reconstruction; it does not make
flashing an upstream image safe.
