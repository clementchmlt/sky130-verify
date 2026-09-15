# sky130-verify

Headless DRC/LVS verification for one Sky130A cell. It runs Magic for DRC,
Netgen for LVS, and writes a provenance manifest and badge.

## Prerequisites

- Python 3.10 or newer
- Magic and Netgen on `PATH`
- A Sky130A PDK with Magic and Netgen technology files
- Optional: KLayout and `efabless/sky130_klayout_pdk` for `--with-klayout`

Manage PDK versions with [ciel](https://github.com/fossi-foundation/ciel) or
[volare](https://github.com/efabless/volare).

## Install

```sh
git clone https://github.com/clementchmlt/sky130-verify.git
cd sky130-verify
python -m pip install .
```

Use `python -m pip install -e .` for development.

## Quick start

```sh
sky130-verify doctor --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT"
sky130-verify check cells/my_opamp \
  --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --out out/
```

`check` writes `manifest.json`, `report.md`, `run.json`, `badge.svg`,
`badge.json`, and raw logs under `out/`.

## Commands

| Command | Description | Requires Magic/Netgen |
|---|---|---|
| `sky130-verify doctor` | Report tool and PDK resolution | no |
| `sky130-verify check <cell>` | Run DRC, LVS, and write outputs | yes |
| `sky130-verify batch <cell...>` | Check independent cells | yes |
| `sky130-verify manifest validate <manifest.json>` | Validate a manifest offline | no |
| `sky130-verify manifest show <manifest.json>` | Render a manifest as Markdown | no |
| `sky130-verify badge render <manifest.json>` | Render badge files | no |

Use `--help` for command options and examples.

## Exit status

| Code | Meaning |
|---|---|
| 0 | DRC and LVS passed |
| 1 | At least one check failed |
| 2 | Invalid input or invocation |
| 3 | Magic, Netgen, or the PDK is unavailable |
| 4 | Tool failure or unrecognized output |
| 5 | Unsupported input scope |

With `--json`, each command writes one JSON object to stdout. Its common keys
are `exit_code`, `status`, and `message`.

## Input selection

By default, a cell directory must contain one `.mag` or `.gds` layout and one
schematic. Use `verify.toml` to select files explicitly:

```toml
[cell]
layout = "layouts/my_opamp.mag"
schematic = "netlists/my_opamp_golden.spice"
name = "my_opamp"

[pdk]
variant = "sky130A"
commit = "<open_pdks commit SHA>"

[source]
repository = "https://example.com/my-repo.git"
commit = "<source commit SHA>"
```

CLI flags take precedence over `verify.toml`, followed by automatic discovery.
Each target describes one complete electrical cell.

## Batch and KLayout

`batch` runs `check` for each target and writes one output directory per cell.
Its exit status is the highest status returned by an individual check.

`--with-klayout --klayout-tech "$KLAYOUT_TECH_PATH"` runs the
`sky130A_mr.drc` KLayout deck. The result is stored separately in the
manifest and report; it does not change the Magic DRC result or exit status.

## Outputs and provenance

`manifest.json` records the source repository and commit, PDK commit,
toolchain, input hashes, DRC/LVS results, and output hashes. Its JSON Schema
is included in the package.

```sh
sky130-verify manifest validate out/manifest.json --verify-files .
sky130-verify badge render out/manifest.json
```

Results are cached under `out/.cache/`. Use `--no-cache` to rerun every step,
`--resume` for interrupted output, and `--force` to replace output with
different provenance. `check` reports files that differ from their recorded
source commit.

## CI

Install the PDK, then run `doctor` and `check` in CI. The repository workflows
exercise the unit suite, wheel installation, and a Magic/Netgen check against
a Sky130A PDK.

## Limits

Automatic macro sub-block detection is unavailable. CPU and memory telemetry
is available only on Linux.

## Security

`check` requires no network access after environment resolution. Magic uses
the resolved PDK `magicrc`. Magic and Netgen receive `PATH`, `HOME`,
`PDK_ROOT`, and `PDK`.

Please report vulnerabilities through GitHub private security advisories.

## License

Apache-2.0. See [LICENSE](LICENSE).
