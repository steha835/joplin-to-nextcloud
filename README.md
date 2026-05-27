# Joplin ↔ Nextcloud Notes

Sync your [Joplin](https://joplinapp.org/) notes with [Nextcloud Notes](https://apps.nextcloud.com/apps/notes) -- in both directions.

The script reads directly from the Joplin SQLite database and writes standard markdown files into a local Nextcloud sync folder. Changes made in Nextcloud can be pushed back to Joplin via the REST API. No manual export needed, no external dependencies -- just Python 3.10+ and the standard library.

## Features

- Reads notes directly from the Joplin database (read-only)
- Preserves notebook hierarchy as folder structure
- Copies resources (images, PDFs, attachments) and rewrites internal `:/`-links to relative paths
- Preserves original created/updated timestamps
- Marks todo notes with a checkbox prefix
- Handles duplicate filenames and sanitizes special characters
- `--sync-to-joplin` to push Nextcloud changes back into Joplin via REST API
- `--dry-run` mode to preview without writing
- `--diff` mode to detect new and modified notes since the last sync

## Quick Start

```bash
# Clone the repo
git clone https://github.com/steha835/joplin-to-nextcloud.git
cd joplin-to-nextcloud

# Preview what would be migrated
python3 joplin_to_nextcloud.py --dry-run

# Run the migration
python3 joplin_to_nextcloud.py
```

## Usage

```
python3 joplin_to_nextcloud.py [--dry-run] [--diff] [--reverse-diff]
python3 joplin_to_nextcloud.py --sync-to-joplin [--dry-run]
```

| Flag | Description |
|------|-------------|
| *(no flag)* | Full migration -- writes all notes and copies resources |
| `--dry-run` | Shows what would be created without writing any files |
| `--diff` | Lists Joplin notes that are new or modified compared to Nextcloud |
| `--reverse-diff` | Lists Nextcloud notes that are new or modified compared to Joplin |
| `--sync-to-joplin` | Pushes new/modified Nextcloud notes into Joplin via the REST API |

### Diff Mode (Joplin → Nextcloud)

After the initial migration, use `--diff` to check what changed in Joplin:

```
$ python3 joplin_to_nextcloud.py --diff

=== DIFF MODE -- showing changes since last sync ===

  NEW:      06_Development/New Project.md        (Joplin: 2026-05-26 20:39)
  MODIFIED: 01_Home/Shopping List.md             (Joplin: 2026-05-25 14:22 > Nextcloud: 2026-05-20 10:00)

=== DIFF SUMMARY ===
  New notes:         1
  Modified notes:    1
  Unchanged notes:   380
```

### Reverse Diff (Nextcloud → Joplin)

Use `--reverse-diff` to find notes that were added or edited in Nextcloud:

```
$ python3 joplin_to_nextcloud.py --reverse-diff

=== REVERSE DIFF -- Nextcloud notes not in Joplin or newer ===

  NEW in Nextcloud:      01_Home/New Note.md               (2026-05-26 20:29)
  MODIFIED in Nextcloud: 01_Home/Shopping List.md           (NC: 2026-05-26 20:56 > Joplin: 2026-05-20 18:41)

=== REVERSE DIFF SUMMARY ===
  New in Nextcloud:      1
  Modified in Nextcloud: 1
  Unchanged:             380

  2 note(s) to review for Joplin import.
```

### Sync to Joplin (Nextcloud → Joplin)

Use `--sync-to-joplin` to push new and modified Nextcloud notes back into Joplin via the REST API. Joplin must be running with the Web Clipper service enabled (Tools → Options → Web Clipper).

```
$ python3 joplin_to_nextcloud.py --sync-to-joplin --dry-run

=== DRY RUN — SYNC TO JOPLIN ===
Scanning /home/user/Nextcloud/Notes

  WOULD CREATE FOLDER: 07_NewProject
  WOULD CREATE: 07_NewProject/Ideas.md  (2026-05-27 14:10)
  WOULD UPDATE: 01_Home/Shopping List.md  (NC: 2026-05-27 09:30 > Joplin: 2026-05-20 18:41)

=== DRY RUN SUMMARY ===
  New notes:      1
  Updated notes:  1
  Unchanged:      380

  2 note(s) would be synced to Joplin.
```

Remove `--dry-run` to actually sync:

```
$ python3 joplin_to_nextcloud.py --sync-to-joplin
```

This mode:
- Creates new notes in Joplin (including any missing folders)
- Updates notes that are newer in Nextcloud than in Joplin
- Restores TODO flags from the `**TODO [x]**` prefix added during export
- **Does not sync attachments** -- only markdown text is transferred

## Default Paths

| Path | Description |
|------|-------------|
| `~/.config/joplin-desktop/database.sqlite` | Joplin database (source) |
| `~/.config/joplin-desktop/resources/` | Joplin resource files (images, etc.) |
| `~/Nextcloud/Notes/` | Nextcloud Notes sync folder (target) |
| `~/Nextcloud/Notes/attachments/` | Copied resources |

To use different paths, edit the constants at the top of the script.

## How It Works

1. Opens the Joplin SQLite database in **read-only** mode
2. Builds the notebook folder hierarchy
3. For each note:
   - Creates a `.md` file named after the note title
   - Places it in the folder matching its Joplin notebook
   - Rewrites Joplin resource references (`(:/abc123...)`) to relative paths
   - Copies referenced resource files to `attachments/`
   - Sets the file's mtime to the Joplin `updated_time`
4. The Nextcloud Desktop Client syncs the files automatically

## Limitations

- **HTML notes** are skipped (Joplin `markup_language = 2`). Most Joplin notes are markdown.
- **Tags** are not migrated (Nextcloud Notes does not support tags).
- **Internal note links** (links between Joplin notes) are not rewritten to Nextcloud paths.
- **Encryption**: Encrypted notes are not supported. Decrypt them in Joplin first.
- **Attachments** are only synced Joplin → Nextcloud. The `--sync-to-joplin` mode transfers text only.

## Requirements

- Python 3.10+
- Joplin Desktop (for the SQLite database and resource files)
- Joplin Web Clipper enabled (for `--sync-to-joplin`; Tools → Options → Web Clipper)
- Nextcloud Desktop Client (for syncing the output folder)

## License

MIT
