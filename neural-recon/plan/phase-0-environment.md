# Phase 0 — Environment and Package Skeleton

Objective: a pinned, reproducible CPU Python environment in which every later
phase runs, plus an importable `nrecon` package and a working pytest entry.

Prerequisites: none.

## Steps

1. Choose the interpreter. Prefer the already-validated CPython x86-64 (under
   Windows ARM64 emulation) used by `host-tools/radar-map` if its version is
   3.10-3.12; otherwise install a CPython 3.11 x86-64 per the workspace
   tooling rules (check `tools/installers/` first, record in
   `tools/README.md`). Record the exact version and architecture in
   `DECISIONS.md`.
2. Create `neural-recon/.venv` with that interpreter. Never install into the
   global environment.
3. Install and pin dependencies:
   - `numpy` (prefer 2.2.6 to match the tooling manifest), `scipy`
     (Hungarian assignment, signal), `torch` CPU build, `pyyaml`, `pytest`.
   - Write exact resolved versions to `neural-recon/requirements.lock` via
     `pip freeze`. Commit the lock file.
   - Add any newly downloaded installer/wheel provenance to
     `tools/README.md` per workspace rules.
4. Create the package skeleton (all files with module docstrings, empty
   bodies where noted):
   - `nrecon/__init__.py`
   - `nrecon/constants.py` — every constant from `README.md` "Fixed
     conventions", plus `directed_links(n: int) -> list[tuple[int, int]]`
     returning the canonical sorted ordering.
   - `nrecon/sim/__init__.py`, `nrecon/baselines/__init__.py`,
     `nrecon/model/__init__.py`, `nrecon/train/__init__.py`,
     `nrecon/eval/__init__.py`
   - `tests/test_constants.py` — asserts `METRES_PER_TAP` equals the
     radar-map value `299702547.0 / 998400000.0` exactly, `TS_NS` within
     1e-6 of 1.0016026, `len(directed_links(5)) == 20`, ordering is
     lexicographic, and `directed_links` contains no self-links.
5. Add `neural-recon/pyproject.toml` with the package name `nrecon`,
   `requires-python`, and pytest configuration (`testpaths = ["tests"]`).
   Install editable: `pip install -e .` from `neural-recon/`.
6. Run `python -m pytest tests -q` and confirm all tests pass.
7. Verify determinism plumbing: add `nrecon/seeding.py` with
   `seed_all(seed: int)` seeding `random`, `numpy`, and `torch`, and a test
   that two seeded draws are identical.

## Exit Gate N0

- [ ] `requirements.lock` committed; interpreter + versions in `DECISIONS.md`
      (and `tools/README.md` if anything was newly installed).
- [ ] `python -c "import nrecon, torch, scipy"` succeeds in the venv.
- [ ] `python -m pytest tests -q` passes (constants + seeding tests).
