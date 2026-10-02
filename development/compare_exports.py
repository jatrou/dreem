#!/usr/bin/env python3
"""Compare CRC and export class in two Linux 4.1 Module.symvers files."""

import argparse
import json
from pathlib import Path
import re


def read_symvers(path):
    entries = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 4 or not re.fullmatch(r"0x[0-9a-fA-F]{8}", fields[0]):
            raise ValueError("invalid Linux 4.1 Module.symvers record")
        crc, name, _, kind = fields
        if name in entries:
            raise ValueError(f"duplicate symbol: {name}")
        entries[name] = {"crc": crc.lower(), "type": kind}
    if not entries:
        raise ValueError("empty symbol table")
    return entries


def compare(stock, baseline):
    shared = stock.keys() & baseline.keys()
    differences = [{"name": name, "stock": stock[name], "baseline": baseline[name]}
                   for name in sorted(shared) if stock[name] != baseline[name]]
    return {"stock_exports": len(stock), "baseline_exports": len(baseline),
            "shared_exports": len(shared), "matching_exports": len(shared) - len(differences),
            "different_exports": differences,
            "stock_only": sorted(stock.keys() - baseline.keys()),
            "baseline_only": sorted(baseline.keys() - stock.keys())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(read_symvers(args.stock), read_symvers(args.baseline)), indent=2))


if __name__ == "__main__":
    main()
