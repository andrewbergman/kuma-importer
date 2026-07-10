## GitHub Actions CI

This workflow validates the repository on every push and pull request.

### What it does

- tests against Python 3.10, 3.11, and 3.12
- installs dependencies from `requirements.txt`
- installs the package in editable mode
- verifies both entry points:
  - `kuma-import --help`
  - `python kuma_importer.py --help`
- validates `example_monitors.csv`
- runs the scenario test runner in a non-destructive way

### Important note

The workflow is deliberately designed so it does **not require live Uptime Kuma credentials**.
It validates packaging, CLI wiring, and example input parsing safely in CI.

If you later want a second workflow for live integration testing, create a separate job that uses GitHub Secrets for:

- `KUMA_URL`
- `KUMA_USERNAME`
- `KUMA_PASSWORD`

Keep that as a separate manual or protected workflow so it does not touch production unintentionally.
