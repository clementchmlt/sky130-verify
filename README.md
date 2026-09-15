# sky130-verify

Headless DRC/LVS verification for a single Sky130A cell. It runs Magic for
DRC, Netgen for LVS, and writes a provenance manifest and badge.

Use [ciel](https://github.com/fossi-foundation/ciel) or
[volare](https://github.com/efabless/volare) to manage PDK versions.

## Installation

```sh
git clone <this-repo> && cd sky130-verify
pip install -e .
```

`pip install .` and `pip install -e .` are supported. Install Magic, Netgen,
KLayout, and the PDK separately. `sky130-verify doctor` reports the resolved
environment.

### Container image

```sh
docker build -t sky130-verify .
docker run --rm --network=none --user "$(id -u):$(id -g)" \
  -v "$PDK_ROOT:/pdk:ro" -v "$PWD:/work" -w /work \
  sky130-verify check cells/my_opamp --pdk-root /pdk --pdk-commit "$PDK_COMMIT" --out out/
```

`Dockerfile` builds Magic and Netgen from pinned source commits on a
digest-pinned Debian base. The runtime image contains the installed binaries
and runtime libraries. It runs as uid/gid 1000 by default; pass
`--user "$(id -u):$(id -g)"` for mounted host volumes. `check` requires no
network access after environment resolution. Image size: ~412 MB.

## 60-second start

```sh
$ sky130-verify doctor
platform       : Linux x86_64 (python 3.12.1)
tool magic    : ok — Magic 8.3.510
tool netgen   : ok — (banner only, no version guarantee)
tool klayout  : absent (optional)
ASLR guard     : available (setarch)
PDK (sky130A)   : resolved

ready for `sky130-verify check`: yes

$ sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --out out/
drc=pass lvs=pass -> out/manifest.json
$ echo $?
0
```

`out/` contains `manifest.json` (the provenance contract), `report.md`
(human-readable), `badge.svg` + `badge.json` (a shields.io endpoint, derived
from the manifest's SHA-256), `run.json` (exact argv, cache state, timings),
and `logs/{drc,extract,lvs}.log` (raw). Logs, the report, and `run.json` are
hashed into the manifest; badges are regenerable derivatives kept out of
that hash to avoid a circular dependency.

## Commands

| Command | Role | Needs Magic/Netgen |
|---|---|---|
| `sky130-verify doctor` | environment diagnostics (tools, PDK, ASLR guard) | no |
| `sky130-verify check <cell>` | DRC then LVS, manifest + badge | yes |
| `sky130-verify batch <cell...>` | `check` on several independent cells, one subdirectory each | yes |
| `sky130-verify manifest validate <manifest.json>` | offline validation against the 1.0.0 schema | no |
| `sky130-verify manifest show <manifest.json>` | readable Markdown rendering | no |
| `sky130-verify badge render <manifest.json>` | (re)generate badge/JSON from an existing manifest | no |

`--help` on every subcommand lists its options with a worked example.
`--version` prints the installed version.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | clean — `drc_verdict == lvs_verdict == "pass"` |
| 1 | non-conformant — at least one `"fail"` |
| 2 | usage error — invalid argument, cell/schematic not found |
| 3 | incomplete environment — Magic/Netgen/PDK not resolved (see `doctor`) |
| 4 | tool failure or ambiguity — crash, or output the parser didn't recognize |
| 5 | refused — out of scope (reserved for macro sub-block detection, not automated yet) |

`manifest validate --verify-files <root>` also checks the SHA-256 of every
attested input and artifact under a given root, without running any EDA
tool. It stays binary (0 valid / 1 invalid).

## Machine output: `--json`

Every subcommand prints, under `--json`, a single JSON object on
stdout** — including on an argument error. The invariant keys are
`exit_code`, `status`, and `message`; each command adds its own details
(`manifest`, `report`, `cells`, `errors`, or `files`). No CI consumer ever
needs to parse a human sentence to decide the outcome:

```sh
$ sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --json | jq .
{
  "exit_code": 0,
  "status": "ok",
  "message": "drc=pass lvs=pass -> ...",
  "manifest_path": "out/manifest.json",
  "manifest": { "...": "the full eight-field manifest" },
  "warnings": []
}
```

`status` is one of `ok`, `not_clean`, `usage_error`,
`environment_incomplete`, `tool_failure`, `timeout`, `tool_error`,
`dry_run`, `refused`, or `interrupted`. Use it to distinguish outcomes in CI.

## Local workflow

```sh
sky130-verify doctor --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT"
sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --dry-run   # preview the commands
sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --out out/
sky130-verify badge render out/manifest.json                          # re-render the badge without redoing DRC/LVS
sky130-verify manifest validate out/manifest.json --verify-files .    # check the attested bytes
```

A second `check` on the same cell, with the same tools and PDK, re-runs
neither DRC, extraction, nor LVS (content-addressed cache under
`out/.cache/` — see `run.json`); `--no-cache` forces a full re-run. A run
left in the `running` state by an interruption requires `--resume` after
confirming no other process is running; `--force` is the only explicit
override for output belonging to an incompatible provenance.

## Declarative configuration: `verify.toml`

Automatic discovery ("one `.mag`/`.gds` and one golden schematic under the
given directory") fails by design on an ambiguous directory or a cell whose
files aren't at its root. An optional `verify.toml` at the cell's root
names them explicitly:

```toml
[cell]
layout = "layouts/my_opamp.mag"
schematic = "netlists/my_opamp_golden.spice"
name = "my_opamp"          # optional — defaults to the layout file's name

[pdk]
variant = "sky130A"        # optional — defaults to sky130A
commit = "…"                # optional — open_pdks commit SHA

[source]
repository = "https://example.com/my-repo.git"  # optional — otherwise detected via git
commit = "…"
```

Precedence, consistent with `$PDK_ROOT` vs. `--pdk-root`: an explicit CLI
flag always wins over `verify.toml`, which wins over automatic discovery.
`verify.toml` describes one complete electrical cell.

## Several independent cells: `batch`

```sh
sky130-verify batch cells/inv cells/nand2 cells/nor2 --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --out-root out/
```

Runs `check` on each given cell independently, under
`<out-root>/<cell-name>/` (disambiguated if two targets share a base name).
Overall exit code = the worst of the individual codes — a single
non-conformant cell or incomplete environment makes the whole batch not
"clean". `--json` produces a single object with `exit_code`, `status`,
`message`, and `cells: [{"target": …, …}, …]`. `--fail-fast` stops at the
first nonzero code instead of running every cell.

Each argument is verified as a complete electrical cell.

## KLayout cross-check: `--with-klayout`

```sh
sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" \
  --with-klayout --klayout-tech "$KLAYOUT_TECH_PATH" --out out/
```

Runs a KLayout DRC with the
[`sky130A_mr.drc`](https://github.com/efabless/sky130_klayout_pdk) deck. The
result is recorded separately in `manifest.toolchain.klayout_cross_check` and
`report.md`; it does not affect Magic's verdict or the exit code. `.mag`
inputs are converted to `.gds`; supplied `.gds` inputs are used directly.

`--klayout-tech` (default `$KLAYOUT_TECH_PATH`) points at an
`efabless/sky130_klayout_pdk` root already cloned at a fixed commit — like
the Sky130A PDK, `sky130-verify` neither builds nor clones it itself;
`sky130-verify doctor --klayout-tech <root>` diagnoses its resolution
independently of `--with-klayout`.

## CI (GitHub Actions)

```yaml
name: sky130-verify
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install sky130-verify
        run: pip install -e .
      - name: Environment diagnostics
        run: sky130-verify doctor --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --json
        env:
          PDK_ROOT: /opt/pdk
          PDK_COMMIT: <open_pdks-or-volare-attested-SHA>
      - name: Verify the cell
        run: sky130-verify check cells/my_opamp --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT" --out out/
        env:
          PDK_ROOT: /opt/pdk
          PDK_COMMIT: <open_pdks-or-volare-attested-SHA>
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: sky130-verify-out
          path: out/
```

The job fails on any nonzero exit code (GitHub Actions' default behavior);
`manifest.json`/`badge.svg`/`report.md` stay available as artifacts even on
failure (`if: always()`).

## Provenance

`manifest.json` follows an eight-field contract, with its JSON Schema
embedded in `sky130_verify/verification_manifest.py`:
`schema_version`, `cell_id`, `source` (repository/commit/paths), `pdk`
(variant + commit SHA), `toolchain` (executable versions and SHA-256, PDK
deck/script hashes, and KLayout deck if used), `inputs` and `artifacts`
(relative paths + SHA-256), `verification` (`drc_verdict` and
`lvs_verdict`, always distinct). No value is entered by hand: everything is
derived mechanically from tool output and the git repository.

A PDK commit is required for a real `check`: it's read from the open_pdks
clone when detectable, or must be supplied via `--pdk-commit` (or
`verify.toml`). If an explicit SHA contradicts a local PDK clone's HEAD,
`doctor` and `check` refuse to run before any DRC/LVS.

If the verified cell has uncommitted changes, `check` doesn't block (a
designer needs to iterate without committing every attempt) but writes the
drift explicitly — on stderr, in `report.md`, and in `warnings` — rather
than letting the manifest imply an exact match with the cited commit.

## Security

`check` requires no network access after environment resolution. Magic uses
the resolved PDK `magicrc`. Magic and Netgen receive an explicit environment
containing `PATH`, `HOME`, `PDK_ROOT`, and `PDK`.

To report a vulnerability, please use GitHub's private security advisories
for this repository rather than a public issue.

## Known limitations

Automatic macro sub-block detection is unavailable. The package is not yet
published to PyPI. CPU/RSS telemetry requires Linux. `verify.toml` describes
one complete electrical cell.

## License

Apache-2.0 — see [LICENSE](LICENSE).
