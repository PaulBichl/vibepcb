# How to work (shared by all issue agents)

You work in your own git worktree of /home/paulb/GIT_Clones/VibePCB (VibePCB tooling, Python). Read AGENTS.md first
and follow it (conventions, fixes contract, TOOLING.md 5.2 table, GLOSSARY.md vocabulary). Read the GitHub issue
with `gh issue view <n>`.

Python: do NOT create a new hatch env. Use the main checkout's env with your worktree's source first:
  PY=/home/paulb/GIT_Clones/VibePCB/.hatch/vibepcb/bin/python
  export PYTHONPATH=$PWD/src   (your worktree root)
  $PY -m pytest -q ; $PY -m ruff check src tests ; $PY -m ruff format --check src tests ; $PY -m mypy src
(look at the `style` / `type` / `test` scripts in pyproject.toml and run their equivalents). All must pass.

Example boards live in sibling repos (read-only for you; never write into them):
  ~/GIT_Clones/VibePCB-bench-psu (bench-psu), ~/GIT_Clones/VibePCB-theremin (open-theremin),
  ~/GIT_Clones/VibePCB-smu (smu-main, smu-front), ~/GIT_Clones/VibePCB-la (la-ecp5), this repo (led-indicator).
To run the pipeline on one, make a private copy in your own scratch dir (mktemp -d), e.g.:
  C=$(mktemp -d); R=~/GIT_Clones/VibePCB-theremin; B=open-theremin
  cp -r $R/boards $R/package.json $C/; ln -s $R/node_modules $C/node_modules
  mkdir -p $C/out/$B/.work && cp -r $R/out/$B/.work/raw $C/out/$B/.work/
  VIBEPCB_ROOT=$C $PY -c 'import sys; from vibepcb.cli import main; sys.exit(main())' fix $B
  (then `check $B`; `fix` + `check` reuse the raw build output, NO `build` - a build re-routes for 10+ min.
   Never run `build`/`all` unless the issue really needs it; then run it in the background and wait.)
Look at the PNG previews in $C/out/$B/.work/preview/ with the Read tool to judge visual results.

When done: commit on your worktree branch (message: what + why, end with
"Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"), do not push, do not touch main, do not comment on or
close the issue. Final report (short): branch name, what changed, before/after numbers on the example boards,
test/style/type status, anything left open or doubtful.
