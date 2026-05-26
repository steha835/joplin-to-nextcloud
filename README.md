# Joplin to Nextcloud Notes

Migrate your [Joplin](https://joplinapp.org/) notes to [Nextcloud Notes](https://apps.nextcloud.com/apps/notes) with a single command.

The script reads directly from the Joplin SQLite database and writes standard markdown files into a local Nextcloud sync folder. No manual export needed, no external dependencies -- just Python 3.10+ and the standard library.

## Features

- Reads notes directly from the Joplin database (read-only)
- Preserves notebook hierarchy as folder structure
- Copies resources (images, PDFs, attachments) and rewrites internal `:/`-links to relative paths
- Preserves original created/updated timestamps
- Marks todo notes with a checkbox prefix
- Handles duplicate filenames and sanitizes special characters
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
python3 joplin_to_nextcloud.py [--dry-run] [--diff]
```

| Flag | Description |
|------|-------------|
| *(no flag)* | Full migration -- writes all notes and copies resources |
| `--dry-run` | Shows what would be created without writing any files |
| `--diff` | Compares timestamps and lists only new or modified notes |

### Diff Mode

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

## Requirements

- Python 3.10+
- Joplin Desktop (for the SQLite database and resource files)
- Nextcloud Desktop Client (for syncing the output folder)

## License

MIT
