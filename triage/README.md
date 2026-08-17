# Working branch and upstream triage

`network-robustness` is an integration branch, not a pull request. It carries
every fix we have in flight so they can be tested together against real
instruments; upstream sees them one at a time, cut from `main`.

    main ──┬── fix-default-open-timeout ──── PR #616 (open)
           │            │
           │            └── network-robustness  ← this branch, never submitted
           │
           └── one branch per queued PR, cut from main, carrying the net change

`queue.md` is the list of what we intend to submit, in order.
`upstream.md` tracks what other people already have in flight, so we do not
collide with them. `locking.md` is a findings-and-design note for the one
queued item too large to describe in `queue.md`. `drafts/` holds text meant for GitHub — PR bodies, review
comments — written here and posted by hand, never by tooling.

## Rules for anything we submit

**One reviewable change per PR.** Upstream squash-merges, so the branch does
not have to be a single commit, but the diff has to read as one idea. A fix
plus a test plus its `CHANGES` entry is one idea. A fix plus a drive-by
cleanup is two.

**Cite the rule, not the vibe.** Every behavioral claim in a commit message,
a code comment or a PR body names the document and the clause: `VXI-11 RULE
B.6.23`, `VPP-4.3 RULE 6.1.2`, `IVI-6.1 section 6.14`. If we cannot point at a
clause, we say it is a judgment call and explain the reasoning. The specs are
in `../vxi11-stress/docs/`.

**Explain the change, not its history.** A PR body describes what the code
does now and why. How the work got here, what was folded into what, which
defect came from which commit, is our record and lives in `queue.md`.

**Show the failure.** A PR that fixes a bug says what a user sees when it
bites: the exception, the wrong status code, the wall-clock stall. Reviewers
should not have to reconstruct the symptom from the patch.

**Test what changed.** `pyvisa_py/testsuite/` runs without hardware.
`test_hislip_session.py` already drives a fake HiSLIP server in-process; the
VXI-11 fixes need the same thing for the core channel, introduced in the first
PR that needs it rather than as a test-infrastructure PR of its own.

**Match the house style.** Terse `CHANGES` entry in the existing voice, ending
`Closes #N PR #N`. Run `ruff check`, `ruff format`, `mypy pyvisa_py` — the PR
template still says `black && isort && flake8`, which the repo replaced with
ruff; tick the box for the check that actually exists and say so if asked.

**No tooling attribution in commits.** Nothing we author carries a
Co-Authored-By or session trailer, on this branch or upstream.

**A PR carries the net change, not the history.** Development here is a
stack, and a later commit tightens comments across earlier ones. Cherry-
picking a historical commit would submit the untightened version, so a
submission branch takes the change as it stands now.

**Nothing from this directory ships.** `triage/` exists only on the working
branch. Submission branches are cut from `main`, so it cannot ride along by
accident.

## Provenance

The VXI-11 findings come from `../vxi11-stress/`, an out-of-tree conformance
suite driving a scriptable VXI-11 server. Where an entry in `queue.md` says
"reproduced", there is a check there that fails before the fix and passes
after. The HiSLIP findings come from `../hislip-stress/`, which runs against
real instruments, and from reading IVI-6.1.

The specifications, all outside this repo:

    VPP-4.3   ../vxi11-stress/docs/vpp43_2024-01-04.pdf
    VXI-11    ../vxi11-stress/docs/VXI-11/vxi-11.pdf and _1 to _3
    IVI-6.1   ../hislip-stress/docs/IVI-6.1_HiSLIP-2.0-2020-04-23.pdf

Read them. A clause cited from memory rather than from the text is how `Q-21`
came to recommend the opposite of what VPP-4.3 requires.
