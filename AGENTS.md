# Repository instructions

This repository contains the standalone OpenHands FastAPI application and
sandbox control plane. The primary code lives in `openhands/app_server/`.

Before changing code, run:

```bash
make install-pre-commit-hooks
```

Before pushing changes:

```bash
make lint
make test
```

Use specific paths with `git add`. Keep the server runnable through
`openhands.app_server.app:app`; do not reintroduce a dependency on the removed
monorepo frontend or `openhands.server.listen`.

Third-party GitHub Actions must be pinned to a full commit SHA. Preserve the
tool versions recorded in `poetry.lock` and `uv.lock` when regenerating either
lockfile.

## Dependency release-age policy

- Wait at least seven days after publication before adding or updating a
  third-party npm or PyPI version, including transitive lockfile changes.
  A weekly update schedule alone does not enforce this minimum age.
- Keep Dependabot `cooldown.default-days: 7` for npm and Python version
  updates, and preserve `tool.uv.exclude-newer = "7 days"` where configured.
  Existing named first-party exceptions do not exempt their third-party
  dependencies. Do not add broad exemptions or disable the cutoff to make a
  resolver succeed.
- Verify the exact version's publication/upload time from npm or PyPI metadata
  (or artifact `upload-time` in `uv.lock`), not the package's creation date.
  If age cannot be established, report the missing evidence rather than guess.
- Security updates can bypass Dependabot cooldown. An urgent exception needs
  explicit maintainer approval recorded in the PR with the package, version,
  advisory/reason, and publication time. Agents must not grant themselves an
  exception. Keep overrides narrow and remove temporary exceptions afterward.
- Use the committed lock for reproducible installs; review newly resolved
  artifacts before committing a lock update.
