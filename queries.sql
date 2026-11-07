-- Run with sqlite3 inventory.db < queries.sql
.headers on
.mode column

-- Scan summaries; duplicate_files includes the retained copy in each group.
SELECT id, root_path, started_at, completed_at, files_seen, files_hashed,
       duplicate_groups, duplicate_files, errors
FROM scans ORDER BY id DESC;

-- All detected groups. Group IDs are local to a scan.
SELECT scan_id, duplicate_group, COUNT(*) AS copies, MAX(size) AS bytes_per_file,
       COUNT(*) * MAX(size) AS total_logical_bytes
FROM files WHERE duplicate_group IS NOT NULL
GROUP BY scan_id, duplicate_group ORDER BY scan_id DESC, total_logical_bytes DESC;

-- Files in groups, with historical modification time in Unix seconds.
SELECT scan_id, duplicate_group, path, size, modified_time, sha256, status
FROM files WHERE duplicate_group IS NOT NULL
ORDER BY scan_id DESC, duplicate_group, path;

-- Example: select a particular scan and group; edit both IDs as needed.
SELECT path, size, modified_time, status FROM files
WHERE scan_id = 1 AND duplicate_group = 1 ORDER BY path;

-- Groups with at least three copies.
SELECT scan_id, duplicate_group, COUNT(*) AS copies FROM files
WHERE duplicate_group IS NOT NULL
GROUP BY scan_id, duplicate_group HAVING COUNT(*) >= 3 ORDER BY copies DESC;

-- Estimated pending savings per scan; revalidation may reduce this estimate.
-- Logical bytes only: compression, sparse files and snapshots affect real savings.
SELECT scan_id, SUM((copies - 1) * bytes_per_file) AS potential_logical_bytes
FROM (
    SELECT scan_id, duplicate_group, COUNT(*) AS copies, MAX(size) AS bytes_per_file
    FROM files WHERE duplicate_group IS NOT NULL AND status = 'pending'
    GROUP BY scan_id, duplicate_group HAVING COUNT(*) > 1
) GROUP BY scan_id;

-- Cleanup status and recoverable source-to-destination mapping / interrupted intent.
SELECT scan_id, status, COUNT(*) AS files FROM files GROUP BY scan_id, status;
SELECT scan_id, path, status, actioned_at, action_note, error_message
FROM files WHERE action_note IS NOT NULL OR error_message IS NOT NULL
ORDER BY scan_id, id;
