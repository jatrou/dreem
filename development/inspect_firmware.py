#!/usr/bin/env python3
"""Extract a small, explicit firmware allowlist for offline development.

Never executes vendor code or extracts the full root filesystem. Extracted
artifacts are private research inputs; the JSON report contains hashes and
hardware/build facts, not arbitrary strings from the firmware.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import tarfile
import zlib

STOCK_SHA256 = "6e6356b51cf197a63fc73acfd7580e5f009c97569d6e716d82767ef13c15f17d"
LIMIT = 128 * 1024 * 1024
OUTER = {"zImage", "imx6ul-nano.dtb", "INSTALL", "rootfs.tar.gz"}
INNER = {"usr/bin/nano_core", "usr/lib/os-release", "etc/firmware/ads_sdma.bin",
         "etc/init.d/S99_load_sdma_firmware", "usr/bin/simple_acquisition_ads1296",
         "etc/imx6ul-nano1.dtb", "etc/imx6ul-nano2.dtb",
         "etc/init.d/S99_dreem", "etc/init.d/S99_watchdog",
         "etc/init.d/S15watchdog", "usr/bin/mpu_watchdog.sh",
         "etc/inittab", "etc/fstab"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def selected_members(archive, wanted):
    """Read regular, exactly named members. Never follow archive links."""
    found = {}
    for member in archive:
        name = member.name.removeprefix("./")
        if name not in wanted:
            continue
        if name in found or not member.isfile() or not 0 <= member.size <= LIMIT:
            raise ValueError(f"invalid or duplicate requested member: {name}")
        with archive.extractfile(member) as stream:
            data = stream.read(LIMIT + 1)
        if len(data) != member.size or len(data) > LIMIT:
            raise ValueError(f"invalid member size: {name}")
        found[name] = data
    missing = wanted - found.keys()
    if missing:
        raise ValueError(f"missing members: {sorted(missing)}")
    return found


def gunzip_member(data, limit=LIMIT):
    dec = zlib.decompressobj(31)
    raw = dec.decompress(data, limit + 1)
    if len(raw) > limit or not dec.eof:
        raise ValueError("truncated or oversized gzip member")
    return raw


def recover_kernel(zimage):
    """Locate a gzip-compressed Linux image and its embedded IKCONFIG."""
    for match in re.finditer(b"\x1f\x8b\x08", zimage):
        try:
            raw = gunzip_member(zimage[match.start():])
            start = raw.index(b"IKCFG_ST") + 8
            end = raw.index(b"IKCFG_ED", start)
            config = gunzip_member(raw[start:end], 1024 * 1024).decode("ascii")
            if "CONFIG_" not in config:
                continue
            return raw, config, match.start()
        except (ValueError, zlib.error, UnicodeError):
            continue
    raise ValueError("no gzip Linux image with an embedded configuration found")


def parse_fdt(data):
    """Parse the FDT structure block, preserving raw property values."""
    if len(data) < 40:
        raise ValueError("short FDT header")
    magic, total, off_struct, off_strings, _, version, _, _, nstrings, nstruct = struct.unpack_from(
        ">10I", data
    )
    if magic != 0xD00DFEED or version < 17 or not 40 <= total <= len(data):
        raise ValueError("unsupported FDT header")
    if any(off < 40 or size > total - off for off, size in
           ((off_struct, nstruct), (off_strings, nstrings))):
        raise ValueError("FDT blocks outside file")
    strings = data[off_strings:off_strings + nstrings]
    block = data[off_struct:off_struct + nstruct]
    nodes, stack, pos = {}, [], 0
    while pos + 4 <= len(block):
        token, = struct.unpack_from(">I", block, pos)
        pos += 4
        if token == 1:  # BEGIN_NODE
            end = block.find(b"\0", pos)
            if end < 0:
                raise ValueError("unterminated FDT node")
            name = block[pos:end].decode("ascii")
            stack.append(name)
            path = "/".join(stack) or "/"
            if path in nodes:
                raise ValueError("duplicate FDT node")
            nodes[path] = {}
            pos = (end + 4) & ~3
        elif token == 2:  # END_NODE
            if not stack:
                raise ValueError("unbalanced FDT nodes")
            stack.pop()
        elif token == 3:  # PROP
            if not stack or pos + 8 > len(block):
                raise ValueError("invalid FDT property")
            length, nameoff = struct.unpack_from(">II", block, pos)
            pos += 8
            end = strings.find(b"\0", nameoff)
            if end < 0 or pos + length > len(block):
                raise ValueError("invalid FDT property bounds")
            name = strings[nameoff:end].decode("ascii")
            props = nodes["/".join(stack) or "/"]
            if name in props:
                raise ValueError("duplicate FDT property")
            props[name] = block[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:  # NOP
            pass
        elif token == 9:  # END
            if stack:
                raise ValueError("unbalanced FDT end")
            return nodes
        else:
            raise ValueError(f"unknown FDT token {token}")
    raise ValueError("missing FDT end")


def text_property(props, name, default=""):
    return props.get(name, default.encode()).rstrip(b"\0").decode("ascii")


def cells(value):
    if len(value) % 4:
        raise ValueError("invalid FDT cells")
    return list(struct.unpack(f">{len(value) // 4}I", value))


def hardware_report(nodes):
    aliases = nodes.get("/aliases", {})
    phandles = {cells(p["phandle"])[0]: (path, p)
                for path, p in nodes.items() if "phandle" in p}
    buses = []
    for alias, value in sorted(aliases.items()):
        if not re.fullmatch(r"(?:i2c|spi|serial)\d+", alias):
            continue
        path = value.rstrip(b"\0").decode("ascii")
        p = nodes[path]
        bus = {"alias": alias, "path": path,
               "status": text_property(p, "status", "okay"),
               "compatible": text_property(p, "compatible").split("\0"),
               "pin_groups": [], "children": []}
        for handle in cells(p.get("pinctrl-0", b"")):
            if handle not in phandles:
                raise ValueError("unresolved pinctrl phandle")
            pinpath, pinprops = phandles[handle]
            bus["pin_groups"].append({"path": pinpath,
                                     "fsl_pins": cells(pinprops.get("fsl,pins", b""))})
        for child, props in nodes.items():
            if child.rsplit("/", 1)[0] == path:
                bus["children"].append({"node": child.rsplit("/", 1)[1],
                                        "compatible": text_property(props, "compatible").split("\0"),
                                        "reg": cells(props.get("reg", b""))})
        buses.append(bus)
    return {"model": text_property(nodes["/"], "model"), "buses": buses}


def upstream_config_gaps(config, source):
    known = set()
    for p in source.rglob("Kconfig*"):
        if p.is_file():
            known.update(re.findall(r"^\s*(?:menu)?config\s+(\w+)",
                                    p.read_text(errors="replace"), re.M))
    if not known:
        raise ValueError("no Kconfig definitions in source directory")
    return {k: v for k, v in re.findall(r"^CONFIG_(\w+)=(.*)", config, re.M)
            if k not in known}


def inspect(archive_path, output, expected_sha256, kernel_source=None):
    if archive_path.stat().st_size > LIMIT:
        raise ValueError("archive too large")
    blob = archive_path.read_bytes()
    if digest(blob) != expected_sha256:
        raise ValueError("archive SHA-256 does not match the expected input")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:bz2") as archive:
        outer = selected_members(archive, OUTER)
    with tarfile.open(fileobj=io.BytesIO(outer["rootfs.tar.gz"]), mode="r:gz") as archive:
        inner = selected_members(archive, INNER)
    raw, config, offset = recover_kernel(outer["zImage"])
    report = {"schema": 1, "archive_sha256": digest(blob),
              "components": {name: {"bytes": len(data), "sha256": digest(data)}
                             for name, data in {**outer, **inner}.items()},
              "kernel": {"gzip_offset": offset, "raw_sha256": digest(raw),
                         "config_sha256": digest(config.encode())},
              "hardware": hardware_report(parse_fdt(outer["imx6ul-nano.dtb"]))}
    if kernel_source:
        report["kernel"]["config_not_in_upstream"] = upstream_config_gaps(config, kernel_source)
    # Require a new directory, including when the existing path is a symlink.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    artifacts = {name: data for name, data in outer.items() if name != "rootfs.tar.gz"}
    artifacts.update({Path(name).name: data for name, data in inner.items()
                      if name != "usr/lib/os-release"})
    artifacts.update({"kernel.raw": raw,
                      "kernel.config": config.encode(),
                      "manifest.json": (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()})
    for name, data in artifacts.items():
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path, help="new private output directory")
    parser.add_argument("--sha256", default=STOCK_SHA256)
    parser.add_argument("--kernel-source", type=Path)
    args = parser.parse_args()
    try:
        report = inspect(args.archive, args.output, args.sha256, args.kernel_source)
    except (OSError, ValueError, tarfile.TarError, zlib.error) as exc:
        parser.exit(1, f"Inspection failed: {exc}\n")
    print(json.dumps({"archive_sha256": report["archive_sha256"],
                      "model": report["hardware"]["model"],
                      "private_output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
