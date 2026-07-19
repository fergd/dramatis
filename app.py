"""
app.py

FastAPI backend for Dramatis — a single-user character-profile catalog for
the Baretext novel-writing project. Mirrors zamak-ledger's structure: one
app.py with all routes, schema.sql as the canonical schema, light
auto-migration on startup so an existing profiles.db survives schema
changes across deployments.

Data model departs from the original brief in two deliberate ways (see
HANDOFF.md): characters can have multiple portraits with one marked
primary (character_images), and relationships are structured
character-to-character links (relationships), not a plain textarea.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8421
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # must run before cloudinary_images imports the cloudinary SDK,
                # which auto-configures from CLOUDINARY_URL at import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cloudinary_images
import drive_backup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dramatis")

DB_PATH = os.environ.get("PROFILES_DB_PATH", "profiles.db")

app = FastAPI(title="Dramatis")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# Canonical column sets, kept in sync with schema.sql. CREATE TABLE IF NOT
# EXISTS is a no-op on a table that already exists — it does NOT add new
# columns from later schema revisions. Since profiles.db persists across
# deployments (gitignored, never recreated), every schema change that adds
# a column needs an entry here, or existing installs crash with "no such
# column" the moment a feature touching the new column runs.
FIELDS_COLUMNS = {
    "key": "TEXT",
    "label": "TEXT",
    "type": "TEXT DEFAULT 'text'",
    "options": "TEXT",
    "section": "TEXT DEFAULT 'Custom'",
    "is_builtin": "INTEGER DEFAULT 0",
    "sort_order": "INTEGER DEFAULT 0",
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
}
CHARACTERS_COLUMNS = {
    "name": "TEXT DEFAULT ''",
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
}
CHARACTER_IMAGES_COLUMNS = {
    "character_id": "INTEGER",
    "url": "TEXT",
    "public_id": "TEXT",
    "is_primary": "INTEGER DEFAULT 0",
    "sort_order": "INTEGER DEFAULT 0",
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
}

# Built-in fields seeded on first run, grouped into the sections the detail
# view renders. One place to tweak the starting set — order here is the
# initial sort_order. (label, type, section, options)
BUILTIN_FIELDS = [
    ("Role / Archetype", "text", "Identity", None),
    ("Age", "number", "Identity", None),
    ("Species / Race", "text", "Identity", None),
    ("Occupation", "text", "Identity", None),
    ("Accent colour", "color", "Identity", None),
    ("Physical description", "textarea", "Description", None),
    ("Personality", "textarea", "Description", None),
    ("Backstory", "textarea", "Description", None),
    ("Goals / motivations", "textarea", "Description", None),
    ("Notes & other comments", "textarea", "Notes", None),
]

# The card-summary field keys returned inline by GET /characters, keyed by
# field key. Matches the card design: species tag, role/occupation line,
# age line, accent colour driving the accent bar/dot.
CARD_SUMMARY_KEYS = ["role_archetype", "age", "species_race", "accent_colour"]

VALID_FIELD_TYPES = {"text", "textarea", "number", "select", "date", "color"}
CARD_IMAGE_WIDTH = 640
DETAIL_IMAGE_WIDTH = 900
THUMB_IMAGE_WIDTH = 200


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "field"


def _migrate_table(conn: sqlite3.Connection, table_name: str, columns: dict):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if not existing:
        return  # table doesn't exist yet — executescript above already created it fully
    for col, coltype in columns.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {coltype}")
                logger.info(f"Migrated {table_name} table: added missing column '{col}'")
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add column '{col}' to {table_name}: {e}")
    conn.commit()


def _seed_builtin_fields(conn: sqlite3.Connection):
    count = conn.execute("SELECT COUNT(*) FROM fields").fetchone()[0]
    if count > 0:
        return
    for i, (label, ftype, section, options) in enumerate(BUILTIN_FIELDS):
        conn.execute(
            "INSERT INTO fields (key, label, type, options, section, is_builtin, sort_order) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (slugify(label), label, ftype, json.dumps(options) if options else None, section, i),
        )
    conn.commit()
    logger.info(f"Seeded {len(BUILTIN_FIELDS)} built-in fields")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(Path("schema.sql").read_text())
    _migrate_table(conn, "fields", FIELDS_COLUMNS)
    _migrate_table(conn, "characters", CHARACTERS_COLUMNS)
    _migrate_table(conn, "character_images", CHARACTER_IMAGES_COLUMNS)
    _seed_builtin_fields(conn)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class CharacterIn(BaseModel):
    name: Optional[str] = ""
    values: Optional[dict] = None  # {field_key: value}


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    values: Optional[dict] = None
    tags: Optional[list] = None  # full-replace when provided


class FieldIn(BaseModel):
    label: str
    type: str = "text"
    options: Optional[list] = None
    section: Optional[str] = "Custom"


class FieldUpdate(BaseModel):
    label: Optional[str] = None
    options: Optional[list] = None
    sort_order: Optional[int] = None
    section: Optional[str] = None


class RelationshipIn(BaseModel):
    related_id: Optional[int] = None
    label: Optional[str] = ""


class RelationshipUpdate(BaseModel):
    related_id: Optional[int] = None
    label: Optional[str] = None


class RestoreIn(BaseModel):
    confirm: bool = False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _field_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "key": row["key"],
        "label": row["label"],
        "type": row["type"],
        "options": json.loads(row["options"]) if row["options"] else None,
        "section": row["section"],
        "is_builtin": bool(row["is_builtin"]),
        "sort_order": row["sort_order"],
    }


def _all_fields(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT * FROM fields ORDER BY sort_order, id").fetchall()
    return [_field_row_to_dict(r) for r in rows]


def _character_values(conn: sqlite3.Connection, character_id: int) -> dict:
    rows = conn.execute(
        "SELECT f.key AS key, cv.value AS value "
        "FROM character_values cv JOIN fields f ON f.id = cv.field_id "
        "WHERE cv.character_id = ?",
        (character_id,),
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def _character_tags(conn: sqlite3.Connection, character_id: int) -> list:
    rows = conn.execute(
        "SELECT tag FROM character_tags WHERE character_id = ? ORDER BY id", (character_id,)
    ).fetchall()
    return [r["tag"] for r in rows]


def _character_images(conn: sqlite3.Connection, character_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM character_images WHERE character_id = ? ORDER BY sort_order, id",
        (character_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "url": cloudinary_images.derived_url(r["public_id"], width=DETAIL_IMAGE_WIDTH),
            "thumb_url": cloudinary_images.derived_url(r["public_id"], width=THUMB_IMAGE_WIDTH),
            "is_primary": bool(r["is_primary"]),
            "sort_order": r["sort_order"],
        })
    return out


def _primary_image_public_id(conn: sqlite3.Connection, character_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT public_id FROM character_images WHERE character_id = ? AND is_primary = 1 LIMIT 1",
        (character_id,),
    ).fetchone()
    return row["public_id"] if row else None


def _character_relationships(conn: sqlite3.Connection, character_id: int) -> list:
    rows = conn.execute(
        "SELECT r.id AS id, r.related_id AS related_id, r.label AS label, c.name AS related_name "
        "FROM relationships r LEFT JOIN characters c ON c.id = r.related_id "
        "WHERE r.character_id = ? ORDER BY r.sort_order, r.id",
        (character_id,),
    ).fetchall()
    return [
        {"id": r["id"], "related_id": r["related_id"], "related_name": r["related_name"], "label": r["label"]}
        for r in rows
    ]


def _character_detail(conn: sqlite3.Connection, character_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "values": _character_values(conn, character_id),
        "tags": _character_tags(conn, character_id),
        "images": _character_images(conn, character_id),
        "relationships": _character_relationships(conn, character_id),
    }


def _character_card(conn: sqlite3.Connection, row: sqlite3.Row, values_by_char: dict, tags_by_char: dict) -> dict:
    public_id = _primary_image_public_id(conn, row["id"])
    portrait_url = cloudinary_images.derived_url(public_id, width=CARD_IMAGE_WIDTH) if public_id else None
    values = values_by_char.get(row["id"], {})
    return {
        "id": row["id"],
        "name": row["name"],
        "portrait_url": portrait_url,
        "tags": tags_by_char.get(row["id"], []),
        **{k: values.get(k) for k in CARD_SUMMARY_KEYS},
    }


def build_export(conn: sqlite3.Connection) -> dict:
    fields = _all_fields(conn)
    char_rows = conn.execute("SELECT * FROM characters ORDER BY id").fetchall()
    characters = [_character_detail(conn, r["id"]) for r in char_rows]
    return {
        "exported_at": _now(),
        "fields": fields,
        "characters": characters,
    }


# ---------------------------------------------------------------------------
# Debounced + daily Drive backup (never blocks core use — all failures are
# logged, never raised back into a request handler)
# ---------------------------------------------------------------------------

DEBOUNCE_SECONDS = 45
_debounce_task: Optional[asyncio.Task] = None
_last_backup_at: Optional[str] = None
_last_backup_error: Optional[str] = None


async def run_backup() -> bool:
    global _last_backup_at, _last_backup_error
    if not drive_backup.is_ready():
        return False
    conn = get_conn()
    try:
        export = build_export(conn)
    finally:
        conn.close()
    try:
        await asyncio.to_thread(drive_backup.push_backup, export)
        _last_backup_at = _now()
        _last_backup_error = None
        logger.info("Drive backup pushed")
        return True
    except Exception as e:
        _last_backup_error = str(e)
        logger.warning(f"Drive backup failed: {e}")
        return False


async def _debounced_backup():
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    await run_backup()


def schedule_backup():
    global _debounce_task
    if not drive_backup.is_ready():
        return
    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(_debounced_backup())


async def _daily_backup_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await run_backup()
        except Exception as e:
            logger.warning(f"Daily backup loop error: {e}")


@app.on_event("startup")
async def _on_startup():
    conn = get_conn()
    conn.close()
    asyncio.create_task(_daily_backup_loop())


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse("static/index.html")


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------

@app.get("/characters")
def list_characters():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM characters ORDER BY id DESC").fetchall()
        all_values = conn.execute(
            "SELECT cv.character_id AS character_id, f.key AS key, cv.value AS value "
            "FROM character_values cv JOIN fields f ON f.id = cv.field_id"
        ).fetchall()
        values_by_char: dict = {}
        for v in all_values:
            values_by_char.setdefault(v["character_id"], {})[v["key"]] = v["value"]

        all_tags = conn.execute("SELECT character_id, tag FROM character_tags ORDER BY id").fetchall()
        tags_by_char: dict = {}
        for t in all_tags:
            tags_by_char.setdefault(t["character_id"], []).append(t["tag"])

        return [_character_card(conn, r, values_by_char, tags_by_char) for r in rows]
    finally:
        conn.close()


@app.get("/characters/{character_id}")
def get_character(character_id: int):
    conn = get_conn()
    try:
        detail = _character_detail(conn, character_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Character not found")
        return detail
    finally:
        conn.close()


def _upsert_values(conn: sqlite3.Connection, character_id: int, values: dict):
    if not values:
        return
    field_rows = conn.execute("SELECT id, key FROM fields").fetchall()
    field_id_by_key = {r["key"]: r["id"] for r in field_rows}
    for key, value in values.items():
        field_id = field_id_by_key.get(key)
        if field_id is None:
            continue  # unknown field key, ignore rather than error
        conn.execute(
            "INSERT INTO character_values (character_id, field_id, value) VALUES (?, ?, ?) "
            "ON CONFLICT(character_id, field_id) DO UPDATE SET value = excluded.value",
            (character_id, field_id, value),
        )
    conn.commit()


def _replace_tags(conn: sqlite3.Connection, character_id: int, tags: list):
    conn.execute("DELETE FROM character_tags WHERE character_id = ?", (character_id,))
    seen = set()
    for tag in tags:
        tag = (tag or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        conn.execute(
            "INSERT INTO character_tags (character_id, tag) VALUES (?, ?)", (character_id, tag)
        )
    conn.commit()


@app.post("/characters", status_code=201)
def create_character(body: CharacterIn):
    conn = get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO characters (name, created_at, updated_at) VALUES (?, ?, ?)",
            ((body.name or "").strip(), now, now),
        )
        character_id = cur.lastrowid
        conn.commit()
        _upsert_values(conn, character_id, body.values or {})
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.put("/characters/{character_id}")
def update_character(character_id: int, body: CharacterUpdate):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM characters WHERE id = ?", (character_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Character not found")
        if body.name is not None:
            conn.execute(
                "UPDATE characters SET name = ?, updated_at = ? WHERE id = ?",
                (body.name.strip(), _now(), character_id),
            )
        else:
            conn.execute("UPDATE characters SET updated_at = ? WHERE id = ?", (_now(), character_id))
        conn.commit()
        _upsert_values(conn, character_id, body.values or {})
        if body.tags is not None:
            _replace_tags(conn, character_id, body.tags)
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.delete("/characters/{character_id}", status_code=204)
def delete_character(character_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM characters WHERE id = ?", (character_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Character not found")
        images = conn.execute(
            "SELECT public_id FROM character_images WHERE character_id = ?", (character_id,)
        ).fetchall()
        for img in images:
            try:
                cloudinary_images.destroy_image(img["public_id"])
            except Exception as e:
                logger.warning(f"Could not destroy Cloudinary asset on character delete: {e}")
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


# ---------------------------------------------------------------------------
# Images (multiple per character, one marked primary)
# ---------------------------------------------------------------------------

@app.post("/characters/{character_id}/images")
async def upload_images(character_id: int, files: list[UploadFile] = File(...)):
    conn = get_conn()
    try:
        char_row = conn.execute("SELECT id FROM characters WHERE id = ?", (character_id,)).fetchone()
        if not char_row:
            raise HTTPException(status_code=404, detail="Character not found")

        has_primary = conn.execute(
            "SELECT 1 FROM character_images WHERE character_id = ? AND is_primary = 1", (character_id,)
        ).fetchone() is not None
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM character_images WHERE character_id = ?",
            (character_id,),
        ).fetchone()[0]

        uploaded_any = False
        for f in files:
            raw_bytes = await f.read()
            try:
                result = cloudinary_images.upload_image(raw_bytes, f.content_type or "")
            except cloudinary_images.UploadError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                logger.error(f"Cloudinary upload failed: {e}")
                raise HTTPException(status_code=502, detail=f"Cloudinary upload failed: {e}")
            max_sort += 1
            make_primary = 1 if (not has_primary and not uploaded_any) else 0
            conn.execute(
                "INSERT INTO character_images (character_id, url, public_id, is_primary, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (character_id, result["secure_url"], result["public_id"], make_primary, max_sort),
            )
            uploaded_any = True

        conn.execute("UPDATE characters SET updated_at = ? WHERE id = ?", (_now(), character_id))
        conn.commit()
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.delete("/characters/{character_id}/images/{image_id}")
def delete_image(character_id: int, image_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM character_images WHERE id = ? AND character_id = ?", (image_id, character_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Image not found")
        try:
            cloudinary_images.destroy_image(row["public_id"])
        except Exception as e:
            logger.warning(f"Could not destroy Cloudinary asset: {e}")
        conn.execute("DELETE FROM character_images WHERE id = ?", (image_id,))

        if row["is_primary"]:
            next_row = conn.execute(
                "SELECT id FROM character_images WHERE character_id = ? ORDER BY sort_order, id LIMIT 1",
                (character_id,),
            ).fetchone()
            if next_row:
                conn.execute("UPDATE character_images SET is_primary = 1 WHERE id = ?", (next_row["id"],))

        conn.execute("UPDATE characters SET updated_at = ? WHERE id = ?", (_now(), character_id))
        conn.commit()
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.put("/characters/{character_id}/images/{image_id}/primary")
def set_primary_image(character_id: int, image_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM character_images WHERE id = ? AND character_id = ?", (image_id, character_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Image not found")
        conn.execute("UPDATE character_images SET is_primary = 0 WHERE character_id = ?", (character_id,))
        conn.execute("UPDATE character_images SET is_primary = 1 WHERE id = ?", (image_id,))
        conn.execute("UPDATE characters SET updated_at = ? WHERE id = ?", (_now(), character_id))
        conn.commit()
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


# ---------------------------------------------------------------------------
# Relationships (structured character-to-character links)
# ---------------------------------------------------------------------------

@app.post("/characters/{character_id}/relationships", status_code=201)
def create_relationship(character_id: int, body: RelationshipIn):
    conn = get_conn()
    try:
        char_row = conn.execute("SELECT id FROM characters WHERE id = ?", (character_id,)).fetchone()
        if not char_row:
            raise HTTPException(status_code=404, detail="Character not found")
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM relationships WHERE character_id = ?",
            (character_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO relationships (character_id, related_id, label, sort_order) VALUES (?, ?, ?, ?)",
            (character_id, body.related_id, (body.label or "").strip(), max_sort + 1),
        )
        conn.commit()
        rel = conn.execute(
            "SELECT r.id AS id, r.related_id AS related_id, r.label AS label, c.name AS related_name "
            "FROM relationships r LEFT JOIN characters c ON c.id = r.related_id WHERE r.id = ?",
            (cur.lastrowid,),
        ).fetchone()
    finally:
        conn.close()
    schedule_backup()
    return dict(rel)


@app.put("/characters/{character_id}/relationships/{relationship_id}")
def update_relationship(character_id: int, relationship_id: int, body: RelationshipUpdate):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM relationships WHERE id = ? AND character_id = ?", (relationship_id, character_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Relationship not found")
        related_id = body.related_id if body.related_id is not None else row["related_id"]
        label = body.label.strip() if body.label is not None else row["label"]
        conn.execute(
            "UPDATE relationships SET related_id = ?, label = ? WHERE id = ?",
            (related_id, label, relationship_id),
        )
        conn.commit()
        rel = conn.execute(
            "SELECT r.id AS id, r.related_id AS related_id, r.label AS label, c.name AS related_name "
            "FROM relationships r LEFT JOIN characters c ON c.id = r.related_id WHERE r.id = ?",
            (relationship_id,),
        ).fetchone()
    finally:
        conn.close()
    schedule_backup()
    return dict(rel)


@app.delete("/characters/{character_id}/relationships/{relationship_id}", status_code=204)
def delete_relationship(character_id: int, relationship_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM relationships WHERE id = ? AND character_id = ?", (relationship_id, character_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Relationship not found")
        conn.execute("DELETE FROM relationships WHERE id = ?", (relationship_id,))
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

@app.get("/fields")
def list_fields():
    conn = get_conn()
    try:
        return _all_fields(conn)
    finally:
        conn.close()


@app.post("/fields", status_code=201)
def create_field(body: FieldIn):
    if not body.label or not body.label.strip():
        raise HTTPException(status_code=400, detail="Label is required")
    if body.type not in VALID_FIELD_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type, must be one of {sorted(VALID_FIELD_TYPES)}")
    if body.type == "select" and not body.options:
        raise HTTPException(status_code=400, detail="Select fields require at least one option")

    conn = get_conn()
    try:
        base_key = slugify(body.label)
        key = base_key
        existing_keys = {r["key"] for r in conn.execute("SELECT key FROM fields").fetchall()}
        suffix = 2
        while key in existing_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1

        max_sort = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM fields").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO fields (key, label, type, options, section, is_builtin, sort_order) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                key, body.label.strip(), body.type,
                json.dumps(body.options) if body.options else None,
                (body.section or "Custom").strip() or "Custom",
                max_sort + 1,
            ),
        )
        conn.commit()
        field = _field_row_to_dict(
            conn.execute("SELECT * FROM fields WHERE id = ?", (cur.lastrowid,)).fetchone()
        )
    finally:
        conn.close()
    schedule_backup()
    return field


@app.put("/fields/{field_id}")
def update_field(field_id: int, body: FieldUpdate):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")

        label = body.label.strip() if body.label is not None and body.label.strip() else row["label"]
        options = json.dumps(body.options) if body.options is not None else row["options"]
        sort_order = body.sort_order if body.sort_order is not None else row["sort_order"]
        section = body.section.strip() if body.section is not None and body.section.strip() else row["section"]

        conn.execute(
            "UPDATE fields SET label = ?, options = ?, sort_order = ?, section = ? WHERE id = ?",
            (label, options, sort_order, section, field_id),
        )
        conn.commit()
        field = _field_row_to_dict(conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone())
    finally:
        conn.close()
    schedule_backup()
    return field


@app.delete("/fields/{field_id}", status_code=204)
def delete_field(field_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT is_builtin FROM fields WHERE id = ?", (field_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")
        if row["is_builtin"]:
            raise HTTPException(status_code=400, detail="Built-in fields can't be deleted")
        conn.execute("DELETE FROM character_values WHERE field_id = ?", (field_id,))
        conn.execute("DELETE FROM fields WHERE id = ?", (field_id,))
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


# ---------------------------------------------------------------------------
# Export / backup / restore / status
# ---------------------------------------------------------------------------

@app.get("/export")
def export_data():
    conn = get_conn()
    try:
        return build_export(conn)
    finally:
        conn.close()


@app.post("/backup")
async def trigger_backup():
    if not drive_backup.is_ready():
        raise HTTPException(status_code=400, detail="Google Drive is not configured/authorized")
    ok = await run_backup()
    if not ok:
        raise HTTPException(status_code=502, detail=_last_backup_error or "Backup failed")
    return {"backed_up_at": _last_backup_at}


@app.post("/restore")
async def restore_data(body: RestoreIn):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Restore must be confirmed (confirm: true)")
    if not drive_backup.is_ready():
        raise HTTPException(status_code=400, detail="Google Drive is not configured/authorized")
    try:
        export = await asyncio.to_thread(drive_backup.pull_latest_backup)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch backup from Drive: {e}")
    if not export:
        raise HTTPException(status_code=404, detail="No backup found on Drive")

    conn = get_conn()
    try:
        conn.execute("DELETE FROM relationships")
        conn.execute("DELETE FROM character_tags")
        conn.execute("DELETE FROM character_images")
        conn.execute("DELETE FROM character_values")
        conn.execute("DELETE FROM characters")
        conn.execute("DELETE FROM fields")
        conn.commit()

        field_id_by_key = {}
        for f in export.get("fields", []):
            cur = conn.execute(
                "INSERT INTO fields (key, label, type, options, section, is_builtin, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f["key"], f["label"], f["type"],
                    json.dumps(f["options"]) if f.get("options") else None,
                    f.get("section", "Custom"),
                    1 if f.get("is_builtin") else 0,
                    f.get("sort_order", 0),
                ),
            )
            field_id_by_key[f["key"]] = cur.lastrowid

        old_to_new_char_id = {}
        for c in export.get("characters", []):
            cur = conn.execute(
                "INSERT INTO characters (name, created_at, updated_at) VALUES (?, ?, ?)",
                (c["name"], c.get("created_at", _now()), c.get("updated_at", _now())),
            )
            character_id = cur.lastrowid
            old_to_new_char_id[c["id"]] = character_id
            for key, value in (c.get("values") or {}).items():
                field_id = field_id_by_key.get(key)
                if field_id is not None and value is not None:
                    conn.execute(
                        "INSERT INTO character_values (character_id, field_id, value) VALUES (?, ?, ?)",
                        (character_id, field_id, value),
                    )
            for tag in c.get("tags") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO character_tags (character_id, tag) VALUES (?, ?)",
                    (character_id, tag),
                )
            for i, img in enumerate(c.get("images") or []):
                if not img.get("public_id"):
                    continue
                conn.execute(
                    "INSERT INTO character_images (character_id, url, public_id, is_primary, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (character_id, img.get("url", ""), img["public_id"], 1 if img.get("is_primary") else 0, i),
                )

        for c in export.get("characters", []):
            new_character_id = old_to_new_char_id.get(c["id"])
            for rel in c.get("relationships") or []:
                new_related_id = old_to_new_char_id.get(rel.get("related_id"))
                conn.execute(
                    "INSERT INTO relationships (character_id, related_id, label) VALUES (?, ?, ?)",
                    (new_character_id, new_related_id, rel.get("label", "")),
                )

        conn.commit()
        _seed_builtin_fields(conn)  # no-op if fields already restored, safety net if export was empty
    finally:
        conn.close()
    return {"restored": True, "exported_at": export.get("exported_at")}


@app.get("/status")
def status():
    conn = get_conn()
    try:
        char_count = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        field_count = conn.execute("SELECT COUNT(*) FROM fields").fetchone()[0]
    finally:
        conn.close()
    return {
        "cloudinary_configured": cloudinary_images.is_configured(),
        "drive_configured": drive_backup.is_configured(),
        "drive_authorized": drive_backup.is_authorized(),
        "last_backup_at": _last_backup_at,
        "last_backup_error": _last_backup_error,
        "counts": {"characters": char_count, "fields": field_count},
    }
