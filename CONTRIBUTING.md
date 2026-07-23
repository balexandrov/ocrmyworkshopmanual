# Contributing

Thanks for looking at this. A few notes on how the project is put together and
what a good PR looks like here.

## Dev environment

Either:

```bash
pip install -r requirements.txt
pip install pytest
```

or, if you use VS Code / GitHub Codespaces, open the repo and reopen in the
**dev container** (`.devcontainer/`) — it installs Ghostscript, builds
jbig2enc from source, and installs Tesseract + the Python dependencies for
you. That container is for working *on* the tool, not for running it against
a real manual archive — `--in-place` writes back to the exact host path it
read from, which needs a mount that lines up with wherever your files
actually live; for real use, just run the script directly per the README.

## Running tests

```bash
pytest tests/
```

`tests/README.md` has the full picture: the fixture corpus (real scanned
pages, committed to the repo), how to extend it, and the settings-matrix
tuning harness. Read that before touching `classify_page()`, the binarization
pipeline, or the photo strategy — changes there should come with matrix
numbers, not just "looked fine."

CI (`.github/workflows/ci.yml`) runs the same suite on Python 3.10 and 3.11 on
every push and PR. It needs Ghostscript + jbig2enc, which it builds the same
way the dev container does (no Debian/Ubuntu apt package ships the `jbig2`
CLI, only the library — verified against the actual package repos, and worth
remembering if you ever touch that workflow).

## Design philosophy — please read before adding a flag

This project went through a deliberate pass to shrink its CLI surface (~35
flags down to ~22), and the reasoning is worth preserving:

- **A flag that only weakens a safety guarantee, with no real use case, gets
  removed, not documented.** `--no-skip-born-digital`, `--no-verify-output`,
  `--no-repair`, `--no-photo-clean` all used to exist; each was "turn off a
  check that only ever helps." If you're tempted to add an off-switch for a
  safety/correctness check, ask first whether anyone would ever legitimately
  want it off — if not, don't add it.
- **A legacy/broken mode is dead code, not a flag.** `--symbol` (shared-dict
  JBIG2 that renders blank in Chrome/Edge) and `--global-threshold` (the old
  fixed-threshold binarizer, strictly worse on real scans) were removed
  entirely, not just hidden behind a flag — including the code path itself.
- **New options need a demonstrated use case.** Tuning knobs that survived
  (`--sauvola-k`, `--photo-descreen`, `--jpeg-quality`, …) each control
  something where the "right" value genuinely varies by input. If a new flag
  would only ever be set one way in practice, it's a constant, not a flag.
- **The atomic write pattern is load-bearing.** Anything touching the
  compress/OCR/`--in-place` path relies on: compress to scratch → verify
  (reopen, check page count) → atomic `os.replace`. A failed verify must keep
  the original untouched. Don't weaken this to "make a test pass."
- **No dependency pinning, on purpose.** `requirements.txt` and
  `pyproject.toml` deliberately don't pin versions — this is a CLI tool run
  locally, not a deployed service, and the reproducibility pinning buys isn't
  worth the version-conflict friction it costs.

## Licensing

MIT (see `LICENSE`). If you add a dependency on a new external tool or
library, update `NOTICE` with its license — see the existing entries for the
Ghostscript/AGPL boundary reasoning (subprocess-invoked, not linked/
redistributed, so its license isn't imposed on this one) as the model to
follow.

## PRs

- Keep the change focused; split unrelated cleanups into their own PR.
- Make sure `pytest tests/` passes locally and CI is green.
- Explain the *why* in the commit message/PR description, not just the *what*.
