# Contributing

Thanks for taking a look. PRs welcome.

## Set up a dev environment

```powershell
git clone https://github.com/nikolsen1234-bit/rewind-recorder.git
cd rewind-recorder
python -m pip install -e .
```

That installs the package in editable mode, so changes you make in `rewind_recorder/` show up the next time you run `rewind-recorder`.

## Run from source

```powershell
rewind-recorder
```

Or:

```powershell
python -m rewind_recorder
```

## Bug reports and feature requests

[Open an issue](https://github.com/nikolsen1234-bit/rewind-recorder/issues). Include your Windows version, Python version, and steps to reproduce if it's a bug.

## Pull requests

- Keep changes focused — one topic per PR.
- Make sure CI passes.
- If you add a user-facing change, update `CHANGELOG.md` under `[Unreleased]`.
