# Claude Code Instructions

## Git workflow

- **One commit per release.** All changes for a release go in a single branch, single PR, squash-merged. Never split fixes, version bumps, or changelog updates into separate PRs.
- **Release flow:** one branch → one PR → squash-merge → tag the merge commit → workflow publishes to PyPI.
- **Commit messages:** `fix:`, `feat:`, `refactor:`, `release:` prefixes. Short, no co-author lines.
- **PR body:** only a `## Summary` section with bullet points. No test plan, no generated-by footer.
- **Never force-push main.**

## Versioning

- Patch (`0.1.x`) for bugfixes and cleanup.
- Minor (`0.x.0`) for new features.
- Major (`x.0.0`) for breaking changes.
- Bump version in both `pyproject.toml` and `rewind_recorder/__init__.py`.
- Update `CHANGELOG.md` — move Unreleased items into the new version section.

## Code style

- No comments unless the WHY is non-obvious.
- No docstrings that just restate the method name.
- Python 3.11+ idioms: `X | None`, not `Optional[X]`.
- No unused imports, no dead code.
