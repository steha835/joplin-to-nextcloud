#!/usr/bin/env python3
"""
Migrate Joplin notes to Nextcloud Notes (and back).

Joplin → Nextcloud: Reads from the Joplin SQLite database, writes markdown
files into a local Nextcloud sync folder with resources.

Nextcloud → Joplin: Reads changed markdown files from Nextcloud, pushes
them into Joplin via the REST API (Web Clipper). Text only, no attachments.

Usage:
    python3 joplin_to_nextcloud.py [--dry-run] [--diff] [--reverse-diff]
    python3 joplin_to_nextcloud.py --sync-to-joplin [--dry-run]
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.parse
import urllib.request
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


def classify_note(full_path: Path, utime: int) -> str:
    """Compare a Joplin note against its Nextcloud counterpart.

    Returns: 'new', 'modified', 'unchanged'
    """
    if not full_path.exists():
        return "new"
    nc_mtime = full_path.stat().st_mtime
    joplin_mtime = utime / 1000
    if joplin_mtime > nc_mtime + 1:
        return "modified"
    return "unchanged"


def reverse_diff():
    """Find Nextcloud notes that are new or modified compared to Joplin."""
    from datetime import datetime, timezone

    if not JOPLIN_DB.exists():
        print(f"ERROR: Joplin database not found at {JOPLIN_DB}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{JOPLIN_DB}?mode=ro", uri=True)
    cur = conn.cursor()

    folders = build_folder_tree(cur)

    # Build a set of known Nextcloud paths with their Joplin updated_time
    joplin_notes = {}
    cur.execute("""
        SELECT id, title, parent_id, updated_time, markup_language
        FROM notes
        WHERE deleted_time = 0 AND is_conflict = 0
    """)
    used_paths = set()
    for nid, title, parent_id, utime, markup in cur.fetchall():
        if markup == 2:
            continue
        folder_path = folders[parent_id]["path"] if parent_id in folders else ""
        filename = sanitize_filename(title) + ".md"
        note_path = os.path.join(folder_path, filename)
        if note_path.lower() in used_paths:
            filename = f"{sanitize_filename(title)}_{nid[:8]}.md"
            note_path = os.path.join(folder_path, filename)
        used_paths.add(note_path.lower())
        joplin_notes[note_path.lower()] = {"path": note_path, "utime": utime}

    conn.close()

    # Walk the Nextcloud Notes folder for .md files
    print(f"=== REVERSE DIFF — Nextcloud notes not in Joplin or newer ===\n")
    stats = {"new": 0, "modified": 0, "unchanged": 0, "skipped": 0}

    for root, dirs, files in os.walk(NEXTCLOUD_NOTES):
        # Skip the attachments directory
        dirs[:] = [d for d in dirs if d != ATTACHMENTS_DIR]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            full_path = Path(root) / fname
            rel_path = str(full_path.relative_to(NEXTCLOUD_NOTES))
            nc_mtime = full_path.stat().st_mtime
            nc_dt = datetime.fromtimestamp(nc_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

            match = joplin_notes.get(rel_path.lower())
            if match is None:
                print(f"  NEW in Nextcloud:      {rel_path}  ({nc_dt})")
                stats["new"] += 1
            elif nc_mtime > match["utime"] / 1000 + 1:
                joplin_dt = datetime.fromtimestamp(match["utime"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                print(f"  MODIFIED in Nextcloud: {rel_path}  (NC: {nc_dt} > Joplin: {joplin_dt})")
                stats["modified"] += 1
            else:
                stats["unchanged"] += 1

    print(f"\n=== REVERSE DIFF SUMMARY ===")
    print(f"  New in Nextcloud:      {stats['new']}")
    print(f"  Modified in Nextcloud: {stats['modified']}")
    print(f"  Unchanged:             {stats['unchanged']}")
    total_changes = stats['new'] + stats['modified']
    if total_changes == 0:
        print("\n  Everything is in sync.")
    else:
        print(f"\n  {total_changes} note(s) to review for Joplin import.")


def migrate(dry_run: bool = False, diff: bool = False):
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
    if diff:
        print("=== DIFF MODE — showing changes since last sync ===\n")
    elif dry_run:
        print("=== DRY RUN — no files will be written ===\n")

    attachments_base = NEXTCLOUD_NOTES / ATTACHMENTS_DIR
    copied_resources = set()
    used_paths = set()
    stats = {
        "notes": 0, "resources": 0, "skipped_html": 0, "errors": 0,
        "new": 0, "modified": 0, "unchanged": 0,
    }

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

        status = classify_note(full_path, utime)
        stats[status] += 1

        if diff:
            if status == "unchanged":
                continue
            from datetime import datetime, timezone
            joplin_dt = datetime.fromtimestamp(utime / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if status == "new":
                print(f"  NEW:      {note_path}  (Joplin: {joplin_dt})")
            else:
                nc_dt = datetime.fromtimestamp(full_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                print(f"  MODIFIED: {note_path}  (Joplin: {joplin_dt} > Nextcloud: {nc_dt})")
            continue

        if status == "unchanged":
            continue

        if is_todo:
            checkbox = "[x]" if todo_completed else "[ ]"
            body = f"**TODO {checkbox}**\n\n{body}"

        prev_copied = len(copied_resources)
        body = rewrite_resource_links(
            body, resources, folder_path, attachments_base, copied_resources, dry_run
        )
        new_resources = len(copied_resources) - prev_copied

        if dry_run:
            action = "WOULD UPDATE" if status == "modified" else "WOULD CREATE"
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(body, encoding="utf-8")
            if ctime and utime:
                os.utime(full_path, (utime / 1000, utime / 1000))
            action = "UPDATED" if status == "modified" else "CREATED"

        stats["notes"] += 1
        stats["resources"] += new_resources
        res_info = f" (+{new_resources} resources)" if new_resources else ""
        print(f"  {action}: {note_path}{res_info}")

    conn.close()

    if diff:
        print(f"\n=== DIFF SUMMARY ===")
        print(f"  New notes:         {stats['new']}")
        print(f"  Modified notes:    {stats['modified']}")
        print(f"  Unchanged notes:   {stats['unchanged']}")
        print(f"  Skipped (HTML):    {stats['skipped_html']}")
    else:
        print(f"\n{'=== DRY RUN SUMMARY ===' if dry_run else '=== SUMMARY ==='}")
        print(f"  New notes:         {stats['new']}")
        print(f"  Updated notes:     {stats['modified']}")
        print(f"  Unchanged notes:   {stats['unchanged']}")
        print(f"  Resources copied:  {stats['resources']}")
        print(f"  Skipped (HTML):    {stats['skipped_html']}")
        print(f"  Total resources:   {len(copied_resources)} unique files")

        if not dry_run:
            print(f"\nNotes written to: {NEXTCLOUD_NOTES}")
            print(f"Attachments in:   {attachments_base}")
            print("\nThe Nextcloud Desktop Client will sync these files automatically.")


## ---------------------------------------------------------------------------
## Joplin REST API helpers (Web Clipper API on localhost)
## ---------------------------------------------------------------------------

JOPLIN_API_PORT = 41184
_api_token_cache = None


def _get_api_token():
    global _api_token_cache
    if _api_token_cache:
        return _api_token_cache
    settings_path = Path.home() / ".config/joplin-desktop/settings.json"
    if not settings_path.exists():
        print("ERROR: Joplin settings.json not found")
        sys.exit(1)
    with open(settings_path) as f:
        settings = json.load(f)
    _api_token_cache = settings.get("api.token")
    if not _api_token_cache:
        print("ERROR: API token not found. Enable Web Clipper in Joplin:")
        print("  Tools → Options → Web Clipper → Enable")
        sys.exit(1)
    return _api_token_cache


def _api_request(method, path, data=None):
    token = _get_api_token()
    url = f"http://localhost:{JOPLIN_API_PORT}{path}?token={urllib.parse.quote(token)}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw)


def _api_ping():
    token = _get_api_token()
    url = f"http://localhost:{JOPLIN_API_PORT}/ping?token={urllib.parse.quote(token)}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()


def _api_post(path, data):
    return _api_request("POST", path, data)


def _api_put(path, data):
    return _api_request("PUT", path, data)


## ---------------------------------------------------------------------------
## Sync Nextcloud → Joplin
## ---------------------------------------------------------------------------

def _build_joplin_note_map(cur, folders):
    """Build lowercase-path → {id, title, parent_id, utime} from Joplin DB."""
    cur.execute("""
        SELECT id, title, parent_id, updated_time, markup_language
        FROM notes
        WHERE deleted_time = 0 AND is_conflict = 0
    """)
    notes = {}
    used_paths = set()
    for nid, title, parent_id, utime, markup in cur.fetchall():
        if markup == 2:
            continue
        folder_path = folders[parent_id]["path"] if parent_id in folders else ""
        filename = sanitize_filename(title) + ".md"
        note_path = os.path.join(folder_path, filename)
        if note_path.lower() in used_paths:
            filename = f"{sanitize_filename(title)}_{nid[:8]}.md"
            note_path = os.path.join(folder_path, filename)
        used_paths.add(note_path.lower())
        notes[note_path.lower()] = {
            "id": nid, "title": title, "parent_id": parent_id, "utime": utime,
        }
    return notes


def _ensure_folder(folder_path, folder_path_to_id, dry_run):
    """Create Joplin folder hierarchy as needed. Returns the leaf folder ID."""
    if not folder_path:
        return ""
    if folder_path in folder_path_to_id:
        return folder_path_to_id[folder_path]

    parts = Path(folder_path).parts
    current = ""
    parent_id = ""
    for part in parts:
        current = os.path.join(current, part) if current else part
        if current in folder_path_to_id:
            parent_id = folder_path_to_id[current]
            continue
        if dry_run:
            fake_id = f"dry_{current}"
            folder_path_to_id[current] = fake_id
            parent_id = fake_id
            print(f"  WOULD CREATE FOLDER: {current}")
            continue
        payload = {"title": part}
        if parent_id:
            payload["parent_id"] = parent_id
        result = _api_post("/folders", payload)
        parent_id = result["id"]
        folder_path_to_id[current] = parent_id
        print(f"  CREATED FOLDER: {current}")
    return parent_id


def sync_to_joplin(dry_run=False):
    """Push new/modified Nextcloud notes into Joplin via the REST API."""
    from datetime import datetime, timezone

    if not JOPLIN_DB.exists():
        print(f"ERROR: Joplin database not found at {JOPLIN_DB}")
        sys.exit(1)

    try:
        _api_ping()
    except Exception:
        print("ERROR: Cannot reach Joplin API on localhost:41184")
        print("Make sure Joplin is running and Web Clipper is enabled.")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{JOPLIN_DB}?mode=ro", uri=True)
    cur = conn.cursor()
    folders = build_folder_tree(cur)
    joplin_notes = _build_joplin_note_map(cur, folders)
    conn.close()

    folder_path_to_id = {info["path"]: fid for fid, info in folders.items()}

    mode = "DRY RUN — " if dry_run else ""
    print(f"=== {mode}SYNC TO JOPLIN ===")
    print(f"Scanning {NEXTCLOUD_NOTES}\n")

    stats = {"new": 0, "modified": 0, "unchanged": 0, "errors": 0,
             "folders_created": 0}

    for root, dirs, files in os.walk(NEXTCLOUD_NOTES):
        dirs[:] = [d for d in dirs if d != ATTACHMENTS_DIR]
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue

            full_path = Path(root) / fname
            rel_path = str(full_path.relative_to(NEXTCLOUD_NOTES))
            nc_mtime = full_path.stat().st_mtime

            match = joplin_notes.get(rel_path.lower())

            if match and nc_mtime <= match["utime"] / 1000 + 1:
                stats["unchanged"] += 1
                continue

            folder_path = str(Path(rel_path).parent)
            if folder_path == ".":
                folder_path = ""
            note_title = match["title"] if match else Path(fname).stem

            body = full_path.read_text(encoding="utf-8")

            # Strip the TODO prefix added during Joplin→Nextcloud export
            todo_match = re.match(r'^\*\*TODO \[([ x])\]\*\*\n\n', body)
            is_todo = bool(todo_match)
            todo_completed = (todo_match.group(1) == "x") if todo_match else False
            if todo_match:
                body = body[todo_match.end():]

            nc_dt = datetime.fromtimestamp(nc_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M")

            if match is None:
                # --- NEW note ---
                parent_id = _ensure_folder(folder_path, folder_path_to_id,
                                           dry_run)
                if dry_run:
                    print(f"  WOULD CREATE: {rel_path}  ({nc_dt})")
                else:
                    note_data = {"title": note_title, "body": body,
                                 "parent_id": parent_id}
                    if is_todo:
                        note_data["is_todo"] = 1
                        note_data["todo_completed"] = (
                            int(datetime.now(timezone.utc).timestamp() * 1000)
                            if todo_completed else 0)
                    try:
                        result = _api_post("/notes", note_data)
                        print(f"  CREATED: {rel_path}  ({nc_dt})")
                    except urllib.error.URLError as e:
                        print(f"  ERROR creating {rel_path}: {e}")
                        stats["errors"] += 1
                        continue
                stats["new"] += 1

            else:
                # --- MODIFIED note ---
                joplin_dt = datetime.fromtimestamp(
                    match["utime"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
                if dry_run:
                    print(f"  WOULD UPDATE: {rel_path}  "
                          f"(NC: {nc_dt} > Joplin: {joplin_dt})")
                else:
                    note_data = {"body": body}
                    if is_todo:
                        note_data["is_todo"] = 1
                        note_data["todo_completed"] = (
                            int(datetime.now(timezone.utc).timestamp() * 1000)
                            if todo_completed else 0)
                    try:
                        _api_put(f"/notes/{match['id']}", note_data)
                        print(f"  UPDATED: {rel_path}  "
                              f"(NC: {nc_dt} > Joplin: {joplin_dt})")
                    except urllib.error.URLError as e:
                        print(f"  ERROR updating {rel_path}: {e}")
                        stats["errors"] += 1
                        continue
                stats["modified"] += 1

    print(f"\n=== {'DRY RUN ' if dry_run else ''}SUMMARY ===")
    print(f"  New notes:      {stats['new']}")
    print(f"  Updated notes:  {stats['modified']}")
    print(f"  Unchanged:      {stats['unchanged']}")
    if stats["errors"]:
        print(f"  Errors:         {stats['errors']}")
    total = stats["new"] + stats["modified"]
    if total == 0:
        print("\n  Everything is in sync.")
    else:
        verb = "would be" if dry_run else "were"
        print(f"\n  {total} note(s) {verb} synced to Joplin.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate notes between Joplin and Nextcloud Notes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    parser.add_argument("--diff", action="store_true",
                        help="Show only new/modified notes since last sync")
    parser.add_argument("--reverse-diff", action="store_true",
                        help="Find Nextcloud notes that are new or modified compared to Joplin")
    parser.add_argument("--sync-to-joplin", action="store_true",
                        help="Push new/modified Nextcloud notes into Joplin via API")
    args = parser.parse_args()
    if args.sync_to_joplin:
        sync_to_joplin(dry_run=args.dry_run)
    elif args.reverse_diff:
        reverse_diff()
    else:
        migrate(dry_run=args.dry_run, diff=args.diff)
