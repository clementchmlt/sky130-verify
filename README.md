# sky130-verify

Run Magic DRC and Netgen LVS on one Sky130A cell. The CLI writes separate
verdicts, a provenance manifest, logs, a Markdown report, and badge files.

## Install

Requires Python 3.10+, Magic, Netgen, and a Sky130A PDK installed through
open_pdks. Install from PyPI with:

```sh
python3 -m pip install sky130-verify
sky130-verify --help
```

The GitHub release also provides a wheel that can be installed directly:

```sh
python3 -m pip install https://github.com/clementchmlt/sky130-verify/releases/download/v0.2.1/sky130_verify-0.2.1-py3-none-any.whl
```

Run `sky130-verify doctor` to see which tools and PDK files are available.
Set `PDK_ROOT` to the directory containing
`sky130A/libs.tech`, or pass `--pdk-root`. Checks also need the open_pdks commit
SHA. Pass `--pdk-commit` when the PDK root is outside its Git checkout.
If a checkout is detected, the supplied SHA must match its HEAD.

## Check a cell

Set `CELL_DIR` to a directory with one `.mag` or `.gds` layout and one `.spice`,
`.spc`, `.cdl`, or `.sp` reference schematic. Run from the source repository
root:

```sh
sky130-verify check "$CELL_DIR" --pdk-root "$PDK_ROOT" --pdk-commit "$PDK_COMMIT"
```

The cell must belong to a Git repository with an `origin` remote, or you must
pass `--source-repository` and `--source-commit`. The default output directory
is `<repository root>/sky130-verify-out/<cell>`. Outside Git, it is
`<cell directory>/sky130-verify-out/<cell>`. `--out` selects a path inside that
same base. Use `--dry-run` to inspect the planned tool invocations.

For a directory with multiple candidate files, add `verify.toml` beside the
cell files:

```toml
[cell]
layout = "layouts/my_opamp.mag"
schematic = "netlists/my_opamp.spice"
name = "my_opamp"

[pdk]
variant = "sky130A"
commit = "0123456789abcdef0123456789abcdef01234567"

[source]
repository = "https://example.com/my-repo.git"
commit = "0123456789abcdef0123456789abcdef01234567"
```

Paths in `verify.toml` are relative to that file. CLI options take precedence.
The commit values above are examples; use the commits of your actual PDK and
source. The CLI warns if checked files differ from the source commit.

## Commands and output

| Command | Result |
|---|---|
| `doctor` | Tool and PDK availability report |
| `check <cell>` | DRC, extraction, LVS, and output files |
| `batch <cell...>` | Sequential cell checks; `--fail-fast` stops at the first nonzero result |
| `manifest validate <file>` | Schema validation; `--verify-files <root>` also checks recorded file hashes |
| `manifest show <file>` | Markdown verdict summary; `--json` returns the full manifest |
| `badge render <file>` | `badge.svg`, `badge.json`, and `snippet.md` |

Run `sky130-verify <command> --help` for options and defaults. Except for
`--help` and `--version`, `--json` prints one JSON object on stdout with
`exit_code`, `status`, and `message`; use the other fields for automation.
Human usage and input errors go to stderr.

Each completed check writes `manifest.json`, `report.md`, `run.json`,
`logs/{drc,extract,lvs}.log`, and badge files under its output directory.
`logs/lvs.out` is recorded when Netgen produces it. The manifest records
SHA-256 digests for inputs, tools, PDK files, scripts, and results. To verify
its recorded files, pass the source repository root to
`manifest validate --verify-files`.

`snippet.md` references `badge.svg` in the same directory. Adjust that path
if you copy the snippet elsewhere.

`--no-cache` reruns DRC, extraction, and LVS. Use `--resume` when an output is
marked interrupted and no process is still writing there. `--force` permits
reusing an output directory with different provenance. `--with-klayout` adds a
separate informational DRC result; set `--klayout-tech` or
`KLAYOUT_TECH_PATH` to its technology directory. The KLayout result does not
affect the exit code.

| Exit | Meaning |
|---:|---|
| 0 | DRC and LVS pass, or dry run completed |
| 1 | DRC or LVS fails, or manifest validation fails |
| 2 | Invalid argument or input file |
| 3 | Required tool, PDK file, or PDK commit missing |
| 4 | Tool failure, timeout, or inconclusive verdict |
| 130 | Interrupted |

## Container

The container includes Magic and Netgen. It needs a mounted PDK and source
repository. From the checkout root, build the image:

```sh
docker build -t sky130-verify .
docker run --rm --network=none --user "$(id -u):$(id -g)" \
  -v "$PDK_ROOT:/pdk:ro" -v "$PWD:/work" -w /work \
  sky130-verify check "$CELL_DIR" --pdk-root /pdk --pdk-commit "$PDK_COMMIT"
```

For the container command, `CELL_DIR` must be relative to the checkout root.
