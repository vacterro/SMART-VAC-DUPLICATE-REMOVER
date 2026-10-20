# conventions.md — harness and gate commands (canonical)

First recorded 2026-09-05 during T-41 SCOUT. Cite these instead of re-deriving.

## Test harness

- Full suite: `python -m unittest test_delete_duplicates` (33 tests, headless
  engine + tmp_path fs tests; run from the repository root)
- Byte-compile check: `python -m py_compile delete_duplicates_gui.py`

## SAIPEN gates

- Core gate: `python <saipen_home>\tools\validate.py --gate core` (run with
  the project root as cwd; `--gate converge` for the convergence gate)
- Engine CLI: `python <saipen_home>\tools\saipen.py <command> [--dry-run] [--json]`
  (`cc` continue, `ee`/`eee` prepare/collect saitranslate, `qq`/`qqq` for
  saiwiki, `scope`, `transition`, `checkpoint`, `ship`, `push`)

## Translation digest rule (canonical, validate.py 1b11)

Locale READMEs end with
`<!-- source-digest: README.md sha256:<64 hex> -->` where the hash is
sha256 of `README.md` read as utf-8-sig with `re.sub(r"\d+\.\d+\.\d+", "VERSION", text)`.
Current normalized digest of README.md:
`b6b91a1554d643cbf88b7aa4fc807a623957660f37d49aa1d9ff53e9c0e9da45`.
