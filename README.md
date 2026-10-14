# netbox-audit-tool

[![CI](https://img.shields.io/github/actions/workflow/status/saivanka/netbox-audit-tool/ci.yml?branch=main&label=CI)](https://github.com/saivanka/netbox-audit-tool/actions)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A CLI that reconciles **NetBox** device inventory against an expected-assets
list and reports discrepancies as a Markdown audit report.

> **Portfolio project.** Built at home-lab scale to demonstrate data-center
> asset-reconciliation skills (inventory auditing, REST API pagination,
> structured reporting). It was not built at, deployed at, or used by any
> employer.

## What it finds

| Finding | Meaning |
|---|---|
| `missing` | Expected asset is not present in NetBox |
| `unexpected` | NetBox device has no matching expected asset |
| `serial_mismatch` | Hostname matches but serial numbers differ |
| `unracked` | NetBox device has no rack assigned |
| `position_conflict` | Two expected assets claim the same rack+position, or a NetBox device's slot is claimed by a different expected host |

## Quickstart (offline, zero infrastructure)

```bash
pip install -r requirements.txt

python audit.py --offline \
  --netbox-csv sample_data/netbox_export_sample.csv \
  --expected-csv sample_data/expected_assets.csv \
  --report report.md
echo "exit code: $?"   # 2 → findings present (expected for the sample data)
```

## Live NetBox mode

Pulls devices from the NetBox REST API with pagination:

```bash
export NETBOX_URL="https://netbox.example.com"
export NETBOX_TOKEN="your-api-token"

python audit.py --live --expected-csv sample_data/expected_assets.csv
```

Flags `--netbox-url` / `--token` work as CLI alternatives to the env vars.

## Expected-CSV schema

| Column | Required | Description |
|---|---|---|
| `hostname` | ✅ | Device hostname (matches NetBox `name`) |
| `serial` | ✅ | Serial number |
| `rack` | ✅ | Rack name (e.g. `DFW-01-A01`) |
| `position` | ✅ | Rack unit (e.g. `42`) |
| `role` | ✅ | Device role (e.g. `server`, `switch`) |

Extra columns are ignored; lines starting with `#` are treated as comments.
A NetBox CSV export (headers like `name`, `device_role`, `rack`, `position`,
`serial`) is accepted directly as `--netbox-csv` in offline mode.

## Sample report output

Against the shipped synthetic sample data:

```markdown
# NetBox audit report

Compared **16** expected assets against **16** NetBox devices. **8** finding(s).

## Missing from NetBox (2)

| Host | Detail |
| --- | --- |
| `cache-01` | expected at DFW-01-A02 / U41 (cache) but not found in NetBox |
| `mgmt-01` | expected at DFW-01-A03 / U42 (server) but not found in NetBox |

## Unexpected in NetBox (2)

| Host | Detail |
| --- | --- |
| `old-nas-1` | present in NetBox (DFW-01-A03 / U20, Storage) but not in expected-assets list |
| `rogue-01` | present in NetBox (DFW-01-A03 / U30, Server) but not in expected-assets list |

## Serial mismatches (1)

| Host | Detail |
| --- | --- |
| `db-01` | NetBox serial 'SN20-9999' != expected 'SN20-0001' |

## Unracked devices (1)

| Host | Detail |
| --- | --- |
| `pdu-1` | device exists in NetBox but has no rack assigned |

## Position conflicts (2)

| Host | Detail |
| --- | --- |
| `rogue-01` | occupies slot DFW-01-A03 / U30 in NetBox, but expected list assigns that slot to 'storage-01' |
| `sw-tor-1, sw-tor-2` | slot DFW-01-A01 / U2 claimed by 2 expected assets: sw-tor-1, sw-tor-2 |
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean — NetBox inventory matches the expected-assets list |
| `2` | Findings reported (see the Markdown report) |
| `1` | Usage or runtime error (bad CSV, missing credentials, API failure) |

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs the test suite on a
Python 3.11 / 3.12 matrix on every push and pull request.

## Running tests

```bash
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
