#!/usr/bin/env python3
"""
Migrate Joplin notes to Nextcloud Notes.

Reads directly from the Joplin SQLite database and writes markdown files
into a local Nextcloud sync folder. Resources (images, attachments) are
copied alongside the notes.

Usage:
    python3 joplin_to_nextcloud.py [--dry-run]
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

JOPLIN_DB = Path.home() / ".config/joplin-desktop/database.sqlite"
JOPLIN_RESOURCES = Path.home() / ".config/joplin-desktop/resources"
NEXTCLOUD_NOTES = Path.home() / "Nextcloud/Notes"
ATTACHMENTS_DIR = "attachments"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.strip('.')
    if not name:
        name = "Untitled"
    if len(name) > 200:
        name = name[:200]
    return name


def build_folder_tree(cur) -> dict:
    """Build folder hierarchy: id -> {title, parent_id, path}."""
    cur.execute("SELECT id, title, parent_id FROM folders")
    folders = {}
    for fid, title, parent_id in cur.fetchall():
        folders[fid] = {"title": title, "parent_id": parent_id, "path": None}

    def resolve_path(fid):
        f = folders[fid]
        if f["path"] is not None:
            return f["path"]
        if not f["parent_id"] or f["parent_id"] not in folders:
            f["path"] = sanitize_filename(f["title"])
        else:
            parent_path = resolve_path(f["parent_id"])
            f["path"] = os.path.join(parent_path, sanitize_filename(f["title"]))
        return f["path"]

    for fid in folders:
        resolve_path(fid)

    return folders


def get_resource_map(cur) -> dict:
    """Build resource id -> {title, mime, file_extension, filename}."""
    cur.execute("SELECT id, title, mime, file_extension, filename FROM resources")
    resources = {}
    for rid, title, mime, ext, filename in cur.fetchall():
        resources[rid] = {
            "title": title,
            "mime": mime or "",
            "extension": ext,
            "filename": filename,
        }
    return resources


def find_resource_file(rid: str, resource_info: dict) -> Path | None:
    """Find the actual resource file in the Joplin resources directory."""
    ext = resource_info.get("extension", "")
    if ext:
        candidate = JOPLIN_RESOURCES / f"{rid}.{ext}"
        if candidate.exists():
            return candidate
    for f in JOPLIN_RESOURCES.glob(f"{rid}.*"):
        return f
    candidate = JOPLIN_RESOURCES / rid
    if candidate.exists():
        return candidate
    return None


def resource_target_name(rid: str, resource_info: dict) -> str:
    """Determine a human-readable filename for the resource."""
    title = resource_info.get("title", "")
    ext = resource_info.get("extension", "")

    if title:
        name = sanitize_filename(Path(title).stem)
    else:
        name = rid[:12]

    if ext:
        return f"{name}.{ext}"

    if title and "." in title:
        return sanitize_filename(title)

    return f"{name}.bin"


def rewrite_resource_links(body: str, resources: dict, note_folder_path: str,
                           attachments_base: Path, copied: set, dry_run: bool) -> str:
    """Replace Joplin resource references with relative paths."""

    def replace_match(m):
        rid = m.group(1)
        if rid not in resources:
            return m.group(0)

        info = resources[rid]
        target_name = resource_target_name(rid, info)
        target_dir = attachments_base
        target_path = target_dir / target_name

        # Handle name collisions
        if target_path.exists() and rid not in copied:
            base, ext = os.path.splitext(target_name)
            target_name = f"{base}_{rid[:8]}{ext}"
            target_path = target_dir / target_name

        if rid not in copied and not dry_run:
            src = find_resource_file(rid, info)
            if src:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target_path)
                copied.add(rid)

        # Relative path from the note's folder to the attachments dir
        note_dir = NEXTCLOUD_NOTES / note_folder_path
        try:
            rel = os.path.relpath(target_path, note_dir)
        except ValueError:
            rel = str(target_path)

        return f"({rel})"

    return re.sub(r'\(:/([a-f0-9]{32})\)', replace_match, body)


def migrate(dry_run: bool = False):
    if not JOPLIN_DB.exists():
        print(f"ERROR: Joplin database not found at {JOPLIN_DB}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{JOPLIN_DB}?mode=ro", uri=True)
    cur = conn.cursor()

    folders = build_folder_tree(cur)
    resources = get_resource_map(cur)

    cur.execute("""
        SELECT id, title, body, parent_id, is_todo, todo_completed,
               created_time, updated_time, markup_language
        FROM notes
        WHERE deleted_time = 0 AND is_conflict = 0
        ORDER BY parent_id, title
    """)
    notes = cur.fetchall()

    print(f"Found {len(notes)} notes in {len(folders)} folders")
    print(f"Found {len(resources)} resources")
    if dry_run:
        print("=== DRY RUN — no files will be written ===\n")

    attachments_base = NEXTCLOUD_NOTES / ATTACHMENTS_DIR
    copied_resources = set()
    used_paths = set()
    stats = {"notes": 0, "resources": 0, "skipped_html": 0, "errors": 0}

    for nid, title, body, parent_id, is_todo, todo_completed, ctime, utime, markup in notes:
        if markup == 2:
            print(f"  SKIP (HTML): {title}")
            stats["skipped_html"] += 1
            continue

        folder_path = folders[parent_id]["path"] if parent_id in folders else ""
        filename = sanitize_filename(title) + ".md"
        note_path = os.path.join(folder_path, filename)

        # Handle duplicate filenames
        if note_path.lower() in used_paths:
            base = sanitize_filename(title)
            filename = f"{base}_{nid[:8]}.md"
            note_path = os.path.join(folder_path, filename)
        used_paths.add(note_path.lower())

        full_path = NEXTCLOUD_NOTES / note_path

        # Add todo checkbox prefix if it's a todo item
        if is_todo:
            checkbox = "[x]" if todo_completed else "[ ]"
            body = f"**TODO {checkbox}**\n\n{body}"

        # Rewrite resource links
        prev_copied = len(copied_resources)
        body = rewrite_resource_links(
            body, resources, folder_path, attachments_base, copied_resources, dry_run
        )
        new_resources = len(copied_resources) - prev_copied

        if dry_run:
            action = "WOULD CREATE"
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(body, encoding="utf-8")

            # Set file timestamps
            if ctime and utime:
                os.utime(full_path, (utime / 1000, utime / 1000))
            action = "CREATED"

        stats["notes"] += 1
        stats["resources"] += new_resources
        res_info = f" (+{new_resources} resources)" if new_resources else ""
        print(f"  {action}: {note_path}{res_info}")

    conn.close()

    print(f"\n{'=== DRY RUN SUMMARY ===' if dry_run else '=== SUMMARY ==='}")
    print(f"  Notes migrated:    {stats['notes']}")
    print(f"  Resources copied:  {stats['resources']}")
    print(f"  Skipped (HTML):    {stats['skipped_html']}")
    print(f"  Total resources:   {len(copied_resources)} unique files")

    if not dry_run:
        print(f"\nNotes written to: {NEXTCLOUD_NOTES}")
        print(f"Attachments in:   {attachments_base}")
        print("\nThe Nextcloud Desktop Client will sync these files automatically.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Joplin notes to Nextcloud Notes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing files")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
