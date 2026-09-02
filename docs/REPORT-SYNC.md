# Report collection — local archive on the Mac

Every completed evaluation (single **and** batch student) is collected into one organized, immutable,
queryable place on this Mac, so reports can be verified centrally during teacher testing. **Nothing leaves
the machine** — no cloud, no account, no credentials.

The collector (`scripts/report_sync.py`) runs off the grading path: it scans `output/<run_id>/` for finished
reports (a run is done when `review_state.json` exists), bundles each into a `.zip`, and records a row in a
local **SQLite** index. It is idempotent (each report is archived once) and versioned (a teacher's edit is
saved as a new version — history preserved). The teacher-facing reports in `output/` and `~/Evaluation
Reports/` are left untouched.

## Where reports land

Default archive: **`~/Evaluation Report Archive/`**
```
~/Evaluation Report Archive/
├── index.sqlite3            # queryable index: one row per report version
├── index.csv               # same rows, human-readable (open in Excel/Numbers)
└── bundles/
    └── <tester>/<subject>/<run_id>/vN-<hash>.zip
```
Each bundle contains the **lossless report data**: `review_state.json` (+ `review_render.json` if the teacher
edited it), the `ocr_output/` result, `report.pdf`, the grading metadata JSONs, and a `manifest.json` with a
sha256 of every file, the models/flags used, and the source `run_dir`.

By default the bundle does **not** copy the ~59 MB of original page scans (they already live in `output/` on
this same Mac — the manifest's `run_dir` points to them). To make each bundle fully self-contained (e.g. to
copy the archive to another machine), set `include_evidence` true (below).

## Run it

```bash
cd "/Users/nidhishchettri/Desktop/Answer_Evaluator_OpenClaw Test OpenSource"
python3 scripts/report_sync.py --dry-run     # preview — lists every report it would archive, writes nothing
python3 scripts/report_sync.py --once        # archive new/changed reports (safe to re-run; skips unchanged)
python3 scripts/report_sync.py --loop --interval 300   # keep archiving every 5 min in the foreground
```

## Install as an automatic background agent (recommended)

Runs `--once` every 5 minutes via launchd (mirrors the credit-monitor agent):

```bash
mkdir -p ~/.report_sync
cp "deploy/com.methdai.report-sync.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.methdai.report-sync.plist   # starts now + on login
# to stop:   launchctl unload ~/Library/LaunchAgents/com.methdai.report-sync.plist
```
Logs: `~/.report_sync/sync.log`. If the repo folder ever moves, update the two paths in the plist.

## Verify / query the reports

```bash
cd ~/"Evaluation Report Archive"
# marks + review flags per student, per tester:
sqlite3 index.sqlite3 "select tester_id, exam_subject, student_name, total_awarded||'/'||total_max as marks, \
  needs_review_count, off_topic_count, injection_count, version from report_submissions order by uploaded_at desc;"
# the full graded JSON for one report (every question's marks/answer/justification/flags):
sqlite3 index.sqlite3 "select review_state from report_submissions where run_id='sheet_1_KRISHNA_RAI';"
# open a bundle:
unzip -l bundles/central/Mathematics/sheet_1_KRISHNA_RAI/v1-*.zip
```

## Configuration (optional — defaults work out of the box)

Create `~/.report_sync/config` (JSON) only if you want to change a default. No secrets go here.

| Key | Default | Meaning |
|---|---|---|
| `archive_dir` | `~/Evaluation Report Archive` | where bundles + index live |
| `include_evidence` | `false` | copy the original scans into each bundle (self-contained, ~60 MB/report) |
| `include_preprocessed` | `false` | also copy the derived preprocessed PNGs (rarely needed) |
| `index_full_review_state` | `true` | store the full graded JSON in the SQLite index (queryable) |
| `tester_default` | `central` | fallback tester id when a report has no Tester/School value |
| `enabled` | `true` | set `false` to pause the agent without unloading it |

Example (`~/.report_sync/config`):
```json
{ "archive_dir": "/Volumes/Backup/Report Archive", "include_evidence": true }
```
Every key can also be set via env, e.g. `REPORT_SYNC_INCLUDE_EVIDENCE=1`.

## Attribution

Each report is tagged with the **Tester / School** field entered on the upload screen (persisted in the
browser and stored in the report's `student_details` / `review_state`). It surfaces as the `tester_id` column
in the index and the top-level folder in `bundles/`, so you can tell whose evaluation each report was.

## Notes

- **Idempotent + versioned:** re-running is a no-op for unchanged reports; a teacher edit (which rewrites
  `review_render.json`) produces a new `vN` bundle + index row, so you keep the full history.
- **Reproducibility:** each report graded after this change also carries a `run_meta.json` (the exact models +
  flags used), captured into the bundle's manifest; older reports fall back to the current `.env`.
- **Retention:** everything is on your disk; delete old bundles/rows whenever you like (the source stays in
  `output/`). Student PII (names, scans) never leaves this Mac.
