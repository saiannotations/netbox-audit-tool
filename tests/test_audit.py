"""Offline-mode tests for audit.py.

Each test builds its own inline CSVs via tmp_path (except the sample-data
integration test, which uses the shipped files deliberately).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audit  # noqa: E402
from audit import audit as run_audit  # noqa: E402

NETBOX_HDR = "name,serial,rack,position,role"
EXPECTED_HDR = "hostname,serial,rack,position,role"

NB_ROW = "web-01,SN1,DFW-01-A01,42,server"
EXP_ROW = "web-01,SN1,DFW-01-A01,42,server"


def make_pair(tmp_path, netbox_rows, expected_rows):
    nb = tmp_path / "netbox.csv"
    exp = tmp_path / "expected.csv"
    nb.write_text(NETBOX_HDR + "\n" + "\n".join(netbox_rows) + "\n")
    exp.write_text(EXPECTED_HDR + "\n" + "\n".join(expected_rows) + "\n")
    return audit.load_netbox_csv(nb), audit.load_expected_csv(exp)


def findings_by_kind(result):
    out = {}
    for f in result.findings:
        out.setdefault(f.kind, []).append(f.hostname)
    return out


# --------------------------------------------------------------------------- #
# One test per finding type, plus the clean case
# --------------------------------------------------------------------------- #

def test_clean_inventory(tmp_path):
    nb, exp = make_pair(tmp_path, [NB_ROW], [EXP_ROW])
    result = run_audit(nb, exp)
    assert result.clean
    assert result.findings == []


def test_missing_from_netbox(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        [NB_ROW],
        [EXP_ROW, "ghost-01,SN9,DFW-01-A02,41,server"],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert by_kind.get("missing") == ["ghost-01"]


def test_unexpected_in_netbox(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        [NB_ROW, "stray-01,SN9,DFW-01-A02,41,server"],
        [EXP_ROW],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert by_kind.get("unexpected") == ["stray-01"]


def test_serial_mismatch(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        ["web-01,SN-WRONG,DFW-01-A01,42,server"],
        [EXP_ROW],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert by_kind.get("serial_mismatch") == ["web-01"]


def test_unracked_device(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        ["loose-01,SN9,,,server"],  # no rack in NetBox
        ["loose-01,SN9,DFW-01-A02,41,server"],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert by_kind.get("unracked") == ["loose-01"]


def test_position_conflict_duplicate_expected_claim(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        ["sw-1,SN1,DFW-01-A01,2,switch"],
        [
            "sw-1,SN1,DFW-01-A01,2,switch",
            "sw-2,SN2,DFW-01-A01,2,switch",  # same slot
        ],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert "position_conflict" in by_kind
    # sw-2 is also missing from NetBox, but the conflict finding must exist
    conflicts = by_kind["position_conflict"]
    assert any("sw-1" in h and "sw-2" in h for h in conflicts)


def test_position_conflict_slot_occupied_by_other_host(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        [
            "storage-01,SN9,DFW-01-A03,30,storage",
            "squatter-01,SN8,DFW-01-A03,30,server",
        ],
        ["storage-01,SN9,DFW-01-A03,30,storage"],
    )
    by_kind = findings_by_kind(run_audit(nb, exp))
    assert by_kind.get("unexpected") == ["squatter-01"]
    assert by_kind.get("position_conflict") == ["squatter-01"]


# --------------------------------------------------------------------------- #
# Loader tolerance
# --------------------------------------------------------------------------- #

def test_expected_csv_tolerates_extra_columns_and_comment_lines(tmp_path):
    exp = tmp_path / "expected.csv"
    exp.write_text(
        "# SAMPLE DATA — SYNTHETIC\n"
        "hostname,serial,rack,position,role,notes,owner\n"
        "web-01,SN1,DFW-01-A01,42,server,hello,me\n",
        encoding="utf-8",
    )
    assets = audit.load_expected_csv(exp)
    assert len(assets) == 1
    assert assets[0].hostname == "web-01"


def test_netbox_csv_understands_export_headers(tmp_path):
    nb = tmp_path / "netbox.csv"
    nb.write_text(
        "id,name,device_role,serial,rack,position,status,site\n"
        "101,web-01,Server,SN1,DFW-01-A01,42,active,DFW-01\n",
        encoding="utf-8",
    )
    assets = audit.load_netbox_csv(nb)
    assert len(assets) == 1
    assert assets[0].hostname == "web-01"
    assert assets[0].role == "Server"


def test_expected_csv_missing_required_column_errors(tmp_path):
    exp = tmp_path / "expected.csv"
    exp.write_text("hostname,serial\nweb-01,SN1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        audit.load_expected_csv(exp)


# --------------------------------------------------------------------------- #
# CLI exit codes (offline mode)
# --------------------------------------------------------------------------- #

def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "audit.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_exit_0_when_clean(tmp_path):
    nb, exp = make_pair(tmp_path, [NB_ROW], [EXP_ROW])
    proc = run_cli("--offline",
                   "--netbox-csv", str(tmp_path / "netbox.csv"),
                   "--expected-csv", str(tmp_path / "expected.csv"))
    assert proc.returncode == 0
    assert "Clean" in proc.stdout


def test_cli_exit_2_when_findings(tmp_path):
    nb, exp = make_pair(
        tmp_path,
        [NB_ROW],
        [EXP_ROW, "ghost-01,SN9,DFW-01-A02,41,server"],
    )
    proc = run_cli("--offline",
                   "--netbox-csv", str(tmp_path / "netbox.csv"),
                   "--expected-csv", str(tmp_path / "expected.csv"))
    assert proc.returncode == 2
    assert "Missing from NetBox" in proc.stdout


def test_cli_writes_report_file(tmp_path):
    nb, exp = make_pair(tmp_path, [NB_ROW], [EXP_ROW])
    report = tmp_path / "report.md"
    proc = run_cli("--offline",
                   "--netbox-csv", str(tmp_path / "netbox.csv"),
                   "--expected-csv", str(tmp_path / "expected.csv"),
                   "--report", str(report))
    assert proc.returncode == 0
    assert report.exists()
    assert "NetBox audit report" in report.read_text()


# --------------------------------------------------------------------------- #
# Sample-data integration: the shipped fixtures must exercise every kind
# --------------------------------------------------------------------------- #

def test_sample_data_exercises_every_finding_type():
    nb = audit.load_netbox_csv(ROOT / "sample_data" / "netbox_export_sample.csv")
    exp = audit.load_expected_csv(ROOT / "sample_data" / "expected_assets.csv")
    result = run_audit(nb, exp)
    by_kind = findings_by_kind(result)
    assert set(by_kind) == {
        "missing", "unexpected", "serial_mismatch",
        "unracked", "position_conflict",
    }
    assert by_kind["missing"] == ["cache-01", "mgmt-01"]
    assert by_kind["serial_mismatch"] == ["db-01"]
    assert by_kind["unracked"] == ["pdu-1"]
    assert by_kind["unexpected"] == ["old-nas-1", "rogue-01"]
    # duplicate slot claim (sw-tor-1/sw-tor-2) + rogue-01 squatting storage-01
    assert len(by_kind["position_conflict"]) == 2
    assert not result.clean
