#!/usr/bin/env python3
"""
netbox-audit-tool: reconcile NetBox device inventory against an expected-assets list.

Two modes:

  * live mode   -- pull devices from the NetBox REST API and compare against
                   an expected-assets CSV.
  * offline mode -- compare a NetBox CSV export against an expected-assets CSV
                   (no NetBox instance needed).

Findings:
  * missing        -- expected asset not present in NetBox
  * unexpected     -- NetBox device with no matching expected asset
  * serial_mismatch -- hostname matches but the serial numbers differ
  * unracked       -- NetBox device with no rack assigned
  * position_conflict -- two or more expected assets claim the same
                        rack+position, or a NetBox device's rack+position is
                        occupied by a *different* expected host

Exit codes:
  * 0 -- clean, no findings
  * 2 -- one or more findings were reported
  * 1 -- usage / runtime error (argparse failures exit 2 like any CLI; I/O and
         API errors raise here)
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:  # offline mode only needs stdlib
    requests = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

EXPECTED_COLUMNS = ("hostname", "serial", "rack", "position", "role")


@dataclass
class Asset:
    """One row from either source. `source` is 'netbox' or 'expected'."""

    hostname: str
    serial: str
    rack: str
    position: str
    role: str
    source: str = "expected"


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #

def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def _data_lines(fh):
    """Yield CSV lines, skipping `#` comment lines (used for SAMPLE DATA
    banners in example files)."""
    for line in fh:
        if line.lstrip().startswith("#"):
            continue
        yield line


def load_expected_csv(path: Path) -> List[Asset]:
    """Load the expected-assets list. Tolerates extra columns; errors on
    missing required ones."""
    assets: List[Asset] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(_data_lines(fh))
        cols = {c.strip().lower() for c in (reader.fieldnames or [])}
        missing_cols = [c for c in EXPECTED_COLUMNS if c not in cols]
        if missing_cols:
            raise ValueError(
                f"{path}: expected CSV is missing required column(s): "
                f"{', '.join(missing_cols)}"
            )
        for line_no, row in enumerate(reader, start=2):
            # Re-key rows by lower-cased, stripped header for tolerance.
            row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
            if not _norm(row.get("hostname")):
                continue  # skip blank lines
            assets.append(
                Asset(
                    hostname=_norm(row.get("hostname")),
                    serial=_norm(row.get("serial")),
                    rack=_norm(row.get("rack")),
                    position=_norm(row.get("position")),
                    role=_norm(row.get("role")),
                    source="expected",
                )
            )
    return assets


def load_netbox_csv(path: Path) -> List[Asset]:
    """Load a NetBox CSV export. NetBox exports use headers such as `name`,
    `serial`, `rack`, `position`, `device_role`; we map those onto the
    canonical fields and tolerate both naming styles."""
    alias = {
        "name": "hostname",
        "device name": "hostname",
        "hostname": "hostname",
        "serial": "serial",
        "serial number": "serial",
        "rack": "rack",
        "rack name": "rack",
        "position": "position",
        "device_role": "role",
        "device role": "role",
        "role": "role",
    }
    assets: List[Asset] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(_data_lines(fh))
        canonical: Dict[int, str] = {}
        for idx, header in enumerate(reader.fieldnames or []):
            key = (header or "").strip().lower()
            if key in alias:
                canonical[idx] = alias[key]
        for line_no, row in enumerate(reader, start=2):
            values = list(row.values())
            fields = {"hostname": "", "serial": "", "rack": "",
                      "position": "", "role": ""}
            for idx, canon in canonical.items():
                if idx < len(values):
                    fields[canon] = _norm(values[idx])
            if not fields["hostname"]:
                continue
            assets.append(Asset(**fields, source="netbox"))
    return assets


# --------------------------------------------------------------------------- #
# NetBox REST API (live mode)
# --------------------------------------------------------------------------- #

def fetch_netbox_devices(base_url: str, token: str) -> List[Asset]:
    """Paginate through NetBox's `/api/dcim/devices/` endpoint and map each
    device onto an Asset."""
    if requests is None:
        raise RuntimeError(
            "live mode requires the 'requests' package "
            "(pip install -r requirements.txt)"
        )
    url = base_url.rstrip("/") + "/api/dcim/devices/"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    assets: List[Asset] = []
    params: Dict[str, str] = {"limit": "100", "brief": "0"}
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"NetBox API request failed ({resp.status_code}): {exc}"
            ) from exc
        data = resp.json()
        for dev in data.get("results", []):
            rack = dev.get("rack") or {}
            assets.append(
                Asset(
                    hostname=_norm(dev.get("name")),
                    serial=_norm(dev.get("serial")),
                    rack=_norm(rack.get("name") if isinstance(rack, dict) else rack),
                    position=_norm(dev.get("position")),
                    role=_norm(
                        (dev.get("device_role") or {}).get("name")
                        if isinstance(dev.get("device_role"), dict)
                        else dev.get("device_role")
                    ),
                    source="netbox",
                )
            )
        url = data.get("next")  # absolute URL from NetBox; params consumed
        params = {}
    return assets


# --------------------------------------------------------------------------- #
# Audit logic
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    kind: str        # missing | unexpected | serial_mismatch | unracked |
                     # position_conflict
    hostname: str
    detail: str


@dataclass
class AuditResult:
    findings: List[Finding] = field(default_factory=list)
    netbox_count: int = 0
    expected_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings


def _slot(asset: Asset) -> Optional[str]:
    """Canonical rack+position slot, or None when unracked/unpositioned."""
    if asset.rack and asset.position:
        return f"{asset.rack} / U{asset.position}"
    return None


def audit(netbox_assets: List[Asset], expected_assets: List[Asset]) -> AuditResult:
    result = AuditResult(
        netbox_count=len(netbox_assets), expected_count=len(expected_assets)
    )
    by_host_netbox: Dict[str, Asset] = {
        a.hostname.lower(): a for a in netbox_assets
    }
    by_host_expected: Dict[str, Asset] = {
        a.hostname.lower(): a for a in expected_assets
    }

    # --- missing & serial mismatches -------------------------------------
    for host_key, exp in by_host_expected.items():
        nb = by_host_netbox.get(host_key)
        if nb is None:
            result.findings.append(
                Finding(
                    "missing",
                    exp.hostname,
                    f"expected at {exp.rack or 'no rack'} / "
                    f"U{exp.position or '?'} ({exp.role or 'no role'}) "
                    "but not found in NetBox",
                )
            )
        elif _norm(nb.serial).lower() != _norm(exp.serial).lower():
            result.findings.append(
                Finding(
                    "serial_mismatch",
                    exp.hostname,
                    f"NetBox serial '{nb.serial or '(blank)'}' != "
                    f"expected '{exp.serial or '(blank)'}'",
                )
            )

    # --- unexpected & unracked --------------------------------------------
    for host_key, nb in by_host_netbox.items():
        if host_key not in by_host_expected:
            result.findings.append(
                Finding(
                    "unexpected",
                    nb.hostname,
                    f"present in NetBox ({nb.rack or 'no rack'} / "
                    f"U{nb.position or '?'}"
                    f"{', ' + nb.role if nb.role else ''}) but not in "
                    "expected-assets list",
                )
            )
        if not nb.rack:
            result.findings.append(
                Finding(
                    "unracked",
                    nb.hostname,
                    "device exists in NetBox but has no rack assigned",
                )
            )

    # --- position conflicts -----------------------------------------------
    # (a) two expected assets claim the same rack+position
    slot_claims: Dict[str, List[Asset]] = {}
    for exp in expected_assets:
        slot = _slot(exp)
        if slot:
            slot_claims.setdefault(slot, []).append(exp)
    for slot, claimants in sorted(slot_claims.items()):
        if len(claimants) > 1:
            names = ", ".join(c.hostname for c in claimants)
            result.findings.append(
                Finding(
                    "position_conflict",
                    names,
                    f"slot {slot} claimed by {len(claimants)} expected "
                    f"assets: {names}",
                )
            )
    # (b) a NetBox device's slot is claimed by a *different* expected host
    # (if the device itself is one of the claimants, the duplicate-claim
    # finding above already covers the ambiguity)
    for nb in netbox_assets:
        slot = _slot(nb)
        if not slot:
            continue
        claimants = slot_claims.get(slot, [])
        if any(c.hostname.lower() == nb.hostname.lower() for c in claimants):
            continue
        for claimant in claimants:
            result.findings.append(
                Finding(
                    "position_conflict",
                    nb.hostname,
                    f"occupies slot {slot} in NetBox, but expected list "
                    f"assigns that slot to '{claimant.hostname}'",
                )
            )

    # Deterministic output order.
    kind_order = {
        "missing": 0,
        "unexpected": 1,
        "serial_mismatch": 2,
        "unracked": 3,
        "position_conflict": 4,
    }
    result.findings.sort(
        key=lambda f: (kind_order.get(f.kind, 9), f.hostname.lower())
    )
    return result


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_KIND_TITLES = {
    "missing": "Missing from NetBox",
    "unexpected": "Unexpected in NetBox",
    "serial_mismatch": "Serial mismatches",
    "unracked": "Unracked devices",
    "position_conflict": "Position conflicts",
}


def render_markdown(result: AuditResult) -> str:
    buf = io.StringIO()
    buf.write("# NetBox audit report\n\n")
    buf.write(
        f"Compared **{result.expected_count}** expected assets against "
        f"**{result.netbox_count}** NetBox devices. "
        f"**{len(result.findings)}** finding(s).\n\n"
    )
    if result.clean:
        buf.write("✅ **Clean** — NetBox inventory matches the expected-assets "
                  "list.\n")
        return buf.getvalue()

    by_kind: Dict[str, List[Finding]] = {}
    for f in result.findings:
        by_kind.setdefault(f.kind, []).append(f)

    for kind, title in _KIND_TITLES.items():
        items = by_kind.get(kind, [])
        if not items:
            continue
        buf.write(f"## {title} ({len(items)})\n\n")
        buf.write("| Host | Detail |\n| --- | --- |\n")
        for f in items:
            buf.write(f"| `{f.hostname}` | {f.detail} |\n")
        buf.write("\n")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile NetBox device inventory against an "
                    "expected-assets CSV. Exit 0 = clean, 2 = findings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true",
                      help="compare two CSVs, no NetBox connection")
    mode.add_argument("--live", action="store_true",
                      help="pull devices from the NetBox REST API")
    p.add_argument("--netbox-csv", type=Path,
                   help="NetBox CSV export (offline mode)")
    p.add_argument("--expected-csv", type=Path, required=True,
                   help="expected-assets CSV "
                        "(columns: hostname,serial,rack,position,role)")
    p.add_argument("--netbox-url",
                   default=os.environ.get("NETBOX_URL"),
                   help="NetBox base URL (or $NETBOX_URL)")
    p.add_argument("--token",
                   default=os.environ.get("NETBOX_TOKEN"),
                   help="NetBox API token (or $NETBOX_TOKEN)")
    p.add_argument("--report", type=Path,
                   help="also write the Markdown report to this file")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        expected = load_expected_csv(args.expected_csv)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.offline:
        if not args.netbox_csv:
            print("error: --offline requires --netbox-csv", file=sys.stderr)
            return 1
        try:
            netbox_assets = load_netbox_csv(args.netbox_csv)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:  # live mode
        if not args.netbox_url or not args.token:
            print("error: live mode needs --netbox-url and --token "
                  "(or $NETBOX_URL / $NETBOX_TOKEN)", file=sys.stderr)
            return 1
        try:
            netbox_assets = fetch_netbox_devices(args.netbox_url, args.token)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    result = audit(netbox_assets, expected)
    report = render_markdown(result)
    print(report, end="")
    if args.report:
        args.report.write_text(report, encoding="utf-8")
        print(f"(report written to {args.report})", file=sys.stderr)

    return 2 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
