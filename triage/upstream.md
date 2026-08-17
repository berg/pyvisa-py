# Upstream state

Checked 2026-08-12. Re-check before cutting any branch: `hb020` is active in
exactly the code we are touching.

## Ours, in flight

| | | |
| --- | --- | --- |
| PR #616 | `berg:fix-default-open-timeout` | Open. Closes #578. Base of `network-robustness`. |

## Other people's, in flight — do not duplicate

| | | |
| --- | --- | --- |
| PR #612 | `hb020:main`, closes #608 | VXI-11 read returns `VI_SUCCESS_MAX_CNT` when END arrives on the last chunk. **Same bug we found independently**, so `Q-08` is not ours to submit; the termchar half is left over as a small follow-up. |
| PR #619 | `hb020:allow-link-id-0-for-srq`, closes #618 | Link ID 0 is valid and the SRQ path rejects it. Does not overlap our queue. |
| PR #587 | `bytewarrior`, NI GPIB-ENET/100 | Unrelated. |

## Open issues that back our queue

| | | |
| --- | --- | --- |
| #583 | VXI-11 lock handling incorrect | Backs `Q-05`, `Q-06`, `Q-07`. The reporter independently diagnosed the socket deadline (`lock_timeout + io_timeout + margin`) and saw the exact `Exception: error creating link: 3` our harness reproduces. They said they have no bandwidth for a PR. |
| #608 | END discarded on exact chunk multiple | Backs `Q-08`. Being fixed by #612. |
| #578 | default `open_timeout` isn't used | Closed by our #616. |
| #593 | exception propagating to user | USB, not ours. Same *class* as `Q-02`/`Q-03` (a raw exception escaping a VISA call) and worth citing as precedent that upstream considers this a bug, but it is not the same bug and we should not claim to close it. |
| #618 | SRQ fails if link ID is 0 | Being fixed by #619. |

## Recent merges that moved our ground

The rebase brought `network-robustness` from a base of #604 up to #615, which
includes two changes inside the VXI-11 SRQ code we touch:

- #610 — interrupt-channel shutdown is only attempted when needed
- #615 — no reply packet on `device_intr_srq`

Both are already reflected in the branch.
