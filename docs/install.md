# Install

## 1. Add the tool

As a submodule, so you can run it on the command line as well as in CI:

    git submodule add https://github.com/JitterCompany/pcb_release.git tools/pcb_release

Optional. Skip it and CI fetches the tool itself, see
[which version runs](#which-version-runs) below.

## 2. Add the workflows

`.github/workflows/hardware.yml` runs the checks on every push and pull request:

```yaml
name: hardware
on:
  push: { branches: [master] }
  pull_request: { branches: [master] }
jobs:
  checks:
    uses: JitterCompany/pcb_release/.github/workflows/kicad-checks.yml@master
    with:
      tools: tools/pcb_release     # omit if you skipped step 1
      project-dirs: |
        hardware/my-board
```

`.github/workflows/hardware-release.yml` is the same file calling
`kicad-release.yml@master` with the same `with:` block, triggered by a tag instead:

```yaml
on:
  workflow_dispatch:
  push: { tags: ["hw-v*"] }
```

Both take the same inputs. See
[Running it in CI](../README.md#running-it-in-ci) for the board list and the
optional settings.

## 3. Add a `release.toml` per board

Beside each board's `.kicad_pro`. It declares what you are asking the fab for, which is
the one thing that cannot be read off the design. You do not have to go and find the
template: the first run of a board without one fails and drops an annotated
[`release.toml.example`](../release.toml.example) next to the project, so fill it in and
rename it.

Full option list in the [`release.toml` reference](release-toml.md).

## Which version runs

`tools:` is the path to your submodule checkout. Leave it out and the workflow fetches
this repo itself at `tools-ref`, which takes a branch, a tag or a full commit SHA (short
ones are not accepted). The default `master` floats, so pass a tag or a SHA to pin CI to
one version.

What the submodule adds over pinning `tools-ref` is a copy on your own machine, so you
can run the checks by hand before pushing, and one SHA governing both that copy and CI,
so a local run predicts what CI will say.

## Requirements

Only for running it locally: `kicad-cli` (KiCad 9 or 10) on PATH and Python 3.8+. No pip
dependencies. CI needs neither, the workflows run in a KiCad container image.
