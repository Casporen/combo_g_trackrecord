# SHA256SUMS errata — 2026-07-08

**Finding**: the 23 entries of the initial backfill (exports 2026-05-19 → 2026-06-09 +
snapshot 20260610T170033Z), written at the original init commit (2026-06-11) of the
private source archive, did NOT match the committed content of those files — the hashes
had been computed on an earlier version, prior to the final reformatting, which was never
committed. `export_daily.py --verify` had therefore been failing on those 23 files since
the beginning (first run of --verify: 2026-07-08).

**Verification before correction**: for each of the 23 files, strict equality
`sha256(disk) == sha256(git blob at the init commit)` — proof that the DATA itself never
changed since its first commit (the git history of the private source archive attests it).
Only the hash table was wrong.

**Correction (2026-07-08)**: the 23 lines were replaced with the hash of the actual
content. The old table remains inspectable in the private source archive's history.
Every entry added after the init commit was correct. Result: 81/81 OK.

*Note: this public repository was initialized from the private source archive after the
correction; all hashes here reflect the corrected, verified table.*
