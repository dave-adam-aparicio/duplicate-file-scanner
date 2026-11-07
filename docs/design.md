# Review and design

## Original review (before edits)

The original scanner recursively uses os.walk without following symbolic links,
collects rows in batches of 500, filters by repeated size, and hashes complete
files with SHA-256 using 1 MiB reads. This architecture is retained. Candidate
path lists consume memory proportional to inventory size, not file contents.
SQLite preserves multiple scans and REAL modification timestamps. Existing SQL
incorrectly combines group numbers across scans.

Original cleanup sorts full paths using SQLite ordering, checks only candidates,
and mutates the database during preview. A missing or changed keeper can therefore
cause loss of the last valid copy. Groups are incorrectly combined across scans.
Move contains a trailing SQL comma, collision detection is not atomic, symlinks
can be followed after scanning, and hard links are counted as recoverable copies.
Logging in the scanner includes an inappropriate hard-coded filename.

## Decisions

Keep the original schema and scan pipeline. Select one completed scan (latest by
default); never combine group identifiers from different scans. Use live
st_mtime_ns for newest retention, with ascending full path as tie breaker.
Validate every pending member before selection; skip the whole group if uncertain.
Reject symlink path components and multiply linked files. Rehash keeper and
candidate before every action, checking identity and metadata around hashing.

Moves use exclusive destination creation, streamed copy, fsync, content
verification, durable SQLite intent recording, and only then source unlink.
This works across filesystems and never overwrites an existing destination.
A failed/interrupted operation can leave an extra recoverable copy; action_note
records the intended destination before mutation. Dry-run opens SQLite read-only.

## Limits

Portable pathname unlink cannot atomically compare file identity and remove it.
There remains a small race after final verification. Use only on trusted, idle
local directories, with backups; do not run competing writers or cleanup jobs.
Hard links are conservatively excluded, including links outside the scan root.
Storage estimates are logical bytes, not physical blocks. Timestamps stored in
SQLite are historical floating-point seconds; selection uses live nanoseconds.
No schema migration is required. Incompatible schemas fail with rescan guidance
without dropping data. File ownership, ACLs and extended attributes are not
preserved by quarantine copies; mode and timestamps are preserved where possible.
