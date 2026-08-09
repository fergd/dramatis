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

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cloudinary_images
import drive_backup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dramatis")

DB_PATH = os.environ.get("PROFILES_DB_PATH", "profiles.db")

# Who's allowed to log in, and who pre-existing (pre-auth) data belongs to.
# Not a security boundary — Tailscale is (see README) — this just separates
# each person's characters/fields from the other's.
ALLOWED_OWNERS = [u.strip() for u in os.environ.get("DRAMATIS_USERS", "").split(",") if u.strip()]
LEGACY_OWNER = os.environ.get("DRAMATIS_LEGACY_OWNER", ALLOWED_OWNERS[0] if ALLOWED_OWNERS else "")
OWNER_COOKIE = "dramatis_owner"
PROJECT_COOKIE = "dramatis_project"

app = FastAPI(title="Dramatis")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")

# Canonical column sets, kept in sync with schema.sql. CREATE TABLE IF NOT
# EXISTS is a no-op on a table that already exists — it does NOT add new
# columns from later schema revisions. Since profiles.db persists across
# deployments (gitignored, never recreated), every schema change that adds
# a column needs an entry here, or existing installs crash with "no such
# column" the moment a feature touching the new column runs.
CHARACTERS_COLUMNS = {
    "owner": "TEXT NOT NULL DEFAULT ''",
    # Nullable here (SQLite's ADD COLUMN can't retroactively enforce NOT
    # NULL without a constant default) — _migrate_fields_to_projects()
    # backfills every existing row's project_id right after this column
    # is added. A fresh install's CREATE TABLE (schema.sql) enforces
    # NOT NULL for real, since no ALTER TABLE is involved there.
    "project_id": "INTEGER",
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

# Built-in project_meta keys seeded on project creation, per the locked
# Projects spec (A1). (key, label) — value starts blank.
BUILTIN_PROJECT_META = [
    ("setting", "Setting / Location"),
    ("form", "Form"),
    ("viewpoint", "Viewpoint"),
    ("logline", "Logline"),
    ("notes", "Notes"),
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


# Fixed, theme-independent colors — "distinct, muted-but-legible hues" the
# author learns once, not derived from whichever of the 5 UI themes is
# active. Antagonistic/Alliance deliberately echo the app's existing
# --danger/--amber semantic colors.
CATEGORY_COLORS = {
    "Family": "#6b9d5c",
    "Romantic": "#d16b8f",
    "Friendship / Social": "#5b8ec9",
    "Professional": "#4fa898",
    "Alliance / Power": "#c99a3a",
    "Antagonistic": "#c86a5f",
    "Other": "#8a8a8a",
}
RELATIONSHIP_CATEGORIES = list(CATEGORY_COLORS.keys())

# The role catalog. Every asymmetric pair gets both directions as their
# own selectable entries (e.g. both "employer" and "employee") even where
# the owner's spec only wrote out one direction per row in prose — a role
# needs to be pickable from *either* character's profile. label/category
# are what the UI shows; inverse must be a real key in this same dict.
RELATIONSHIP_ROLES = {
    # Family
    "parent": {"label": "Parent", "category": "Family", "inverse": "child"},
    "child": {"label": "Child", "category": "Family", "inverse": "parent"},
    "sibling": {"label": "Sibling", "category": "Family", "inverse": "sibling"},
    "spouse": {"label": "Spouse", "category": "Family", "inverse": "spouse"},
    "grandparent": {"label": "Grandparent", "category": "Family", "inverse": "grandchild"},
    "grandchild": {"label": "Grandchild", "category": "Family", "inverse": "grandparent"},
    "aunt_uncle": {"label": "Aunt/Uncle", "category": "Family", "inverse": "niece_nephew"},
    "niece_nephew": {"label": "Niece/Nephew", "category": "Family", "inverse": "aunt_uncle"},
    "cousin": {"label": "Cousin", "category": "Family", "inverse": "cousin"},
    "step_parent": {"label": "Step-parent", "category": "Family", "inverse": "step_child"},
    "step_child": {"label": "Step-child", "category": "Family", "inverse": "step_parent"},
    "guardian": {"label": "Guardian", "category": "Family", "inverse": "ward"},
    "ward": {"label": "Ward", "category": "Family", "inverse": "guardian"},
    "in_law": {"label": "In-law", "category": "Family", "inverse": "in_law"},
    # Romantic
    "partner": {"label": "Partner", "category": "Romantic", "inverse": "partner"},
    "fiance": {"label": "Fiancé(e)", "category": "Romantic", "inverse": "fiance"},
    "lover": {"label": "Lover", "category": "Romantic", "inverse": "lover"},
    "ex_partner": {"label": "Ex-partner", "category": "Romantic", "inverse": "ex_partner"},
    "unrequited": {"label": "Unrequited (pines for)", "category": "Romantic", "inverse": "object_of_affection"},
    "object_of_affection": {"label": "Object of affection", "category": "Romantic", "inverse": "unrequited"},
    # Friendship / Social
    "friend": {"label": "Friend", "category": "Friendship / Social", "inverse": "friend"},
    "best_friend": {"label": "Best friend", "category": "Friendship / Social", "inverse": "best_friend"},
    "confidant": {"label": "Confidant", "category": "Friendship / Social", "inverse": "confidant"},
    "acquaintance": {"label": "Acquaintance", "category": "Friendship / Social", "inverse": "acquaintance"},
    "neighbour": {"label": "Neighbour", "category": "Friendship / Social", "inverse": "neighbour"},
    # Professional
    "colleague": {"label": "Colleague", "category": "Professional", "inverse": "colleague"},
    "employer": {"label": "Employer", "category": "Professional", "inverse": "employee"},
    "employee": {"label": "Employee", "category": "Professional", "inverse": "employer"},
    "superior": {"label": "Superior", "category": "Professional", "inverse": "subordinate"},
    "subordinate": {"label": "Subordinate", "category": "Professional", "inverse": "superior"},
    "mentor": {"label": "Mentor", "category": "Professional", "inverse": "mentee"},
    "mentee": {"label": "Mentee", "category": "Professional", "inverse": "mentor"},
    "teacher": {"label": "Teacher", "category": "Professional", "inverse": "student"},
    "student": {"label": "Student", "category": "Professional", "inverse": "teacher"},
    "business_partner": {"label": "Business partner", "category": "Professional", "inverse": "business_partner"},
    "client": {"label": "Client", "category": "Professional", "inverse": "service_provider"},
    "service_provider": {"label": "Service provider", "category": "Professional", "inverse": "client"},
    # Alliance / Power
    "ally": {"label": "Ally", "category": "Alliance / Power", "inverse": "ally"},
    "leader": {"label": "Leader", "category": "Alliance / Power", "inverse": "follower"},
    "follower": {"label": "Follower", "category": "Alliance / Power", "inverse": "leader"},
    "master": {"label": "Master", "category": "Alliance / Power", "inverse": "servant"},
    "servant": {"label": "Servant", "category": "Alliance / Power", "inverse": "master"},
    "patron": {"label": "Patron", "category": "Alliance / Power", "inverse": "protege"},
    "protege": {"label": "Protégé", "category": "Alliance / Power", "inverse": "patron"},
    "captor": {"label": "Captor", "category": "Alliance / Power", "inverse": "captive"},
    "captive": {"label": "Captive", "category": "Alliance / Power", "inverse": "captor"},
    # Antagonistic
    "rival": {"label": "Rival", "category": "Antagonistic", "inverse": "rival"},
    "enemy": {"label": "Enemy", "category": "Antagonistic", "inverse": "enemy"},
    "nemesis": {"label": "Nemesis", "category": "Antagonistic", "inverse": "nemesis"},
    "betrayer": {"label": "Betrayer", "category": "Antagonistic", "inverse": "betrayed"},
    "betrayed": {"label": "Betrayed", "category": "Antagonistic", "inverse": "betrayer"},
}


def _check_relationship_catalog_integrity():
    """A hand-written ~50-entry catalog is exactly where a typo hides
    silently — crash on startup rather than misfile relationships later."""
    for key, role in RELATIONSHIP_ROLES.items():
        inv_key = role["inverse"]
        if inv_key not in RELATIONSHIP_ROLES:
            raise AssertionError(f"relationship role {key!r} has unknown inverse {inv_key!r}")
        inv_role = RELATIONSHIP_ROLES[inv_key]
        if inv_role["inverse"] != key:
            raise AssertionError(f"relationship role {key!r}/{inv_key!r} inverse is not involutive")
        if inv_role["category"] != role["category"]:
            raise AssertionError(f"relationship role {key!r}/{inv_key!r} disagree on category")
        if role["category"] not in CATEGORY_COLORS:
            raise AssertionError(f"relationship role {key!r} has unknown category {role['category']!r}")


_check_relationship_catalog_integrity()


def relationship_role_label(role_key: str, custom_label: Optional[str]) -> str:
    if role_key.startswith("custom:"):
        return custom_label or "Related"
    role = RELATIONSHIP_ROLES.get(role_key)
    return role["label"] if role else role_key


def relationship_category(role_key: str) -> str:
    role = RELATIONSHIP_ROLES.get(role_key)
    return role["category"] if role else "Other"


def _migrate_table(conn: sqlite3.Connection, table_name: str, columns: dict):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if not existing:
        return  # table doesn't exist yet — the executescript call in get_conn() will create it fully
    for col, coltype in columns.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {coltype}")
                logger.info(f"Migrated {table_name} table: added missing column '{col}'")
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not add column '{col}' to {table_name}: {e}")
    conn.commit()


def _seed_builtin_fields(conn: sqlite3.Connection, project_id: int):
    count = conn.execute("SELECT COUNT(*) FROM fields WHERE project_id = ?", (project_id,)).fetchone()[0]
    if count > 0:
        return
    for i, (label, ftype, section, options) in enumerate(BUILTIN_FIELDS):
        conn.execute(
            "INSERT INTO fields (project_id, key, label, type, options, section, is_builtin, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (project_id, slugify(label), label, ftype, json.dumps(options) if options else None, section, i),
        )
    conn.commit()
    logger.info(f"Seeded {len(BUILTIN_FIELDS)} built-in fields for project_id={project_id}")


def _seed_builtin_project_meta(conn: sqlite3.Connection, project_id: int):
    count = conn.execute("SELECT COUNT(*) FROM project_meta WHERE project_id = ?", (project_id,)).fetchone()[0]
    if count > 0:
        return
    for i, (key, label) in enumerate(BUILTIN_PROJECT_META):
        conn.execute(
            "INSERT INTO project_meta (project_id, key, label, value, is_builtin, sort_order) "
            "VALUES (?, ?, ?, '', 1, ?)",
            (project_id, key, label, i),
        )
    conn.commit()
    logger.info(f"Seeded {len(BUILTIN_PROJECT_META)} built-in project_meta keys for project_id={project_id}")


def _create_project(conn: sqlite3.Connection, owner: str, title: str = "Untitled Project", seed_fields: bool = True) -> int:
    """The one place a project gets created — migration, POST /projects,
    and get_current_project's zero-projects fallback all go through this,
    so builtin-seeding never drifts out of sync between call sites.
    seed_fields=False is used only mid-migration, when `fields` hasn't
    been rebuilt to its project-scoped shape yet (see
    _migrate_fields_to_projects)."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO projects (owner, title, created_at, updated_at, last_opened_at) VALUES (?, ?, ?, ?, ?)",
        (owner, title, now, now, now),
    )
    project_id = cur.lastrowid
    conn.commit()
    _seed_builtin_project_meta(conn, project_id)
    if seed_fields:
        _seed_builtin_fields(conn, project_id)
    return project_id


def _migrate_fields_to_owner(conn: sqlite3.Connection):
    """One-time rebuild: `fields.key` used to be globally UNIQUE; now it's only
    unique per-owner (both people get identically-keyed built-ins). SQLite
    can't ALTER a UNIQUE constraint, so this builds the new table under a
    temp name, copies every row across under LEGACY_OWNER — preserving `id`
    so character_values.field_id stays valid — drops the old `fields` table,
    then renames the temp table into place. Guarded by the presence of the
    `owner` column, so it only runs once.

    Deliberately does NOT rename the old `fields` table directly (e.g. to
    `fields_legacy`): SQLite's RENAME TO silently rewrites *other* tables'
    FK definitions to follow a renamed table — confirmed on this deploy's
    SQLite (3.40.1) that this happens even with `PRAGMA foreign_keys=OFF`,
    permanently pointing character_values.field_id at a name that's about to
    be dropped. Renaming the *new* table into place instead is safe, since
    nothing references "fields_new" yet for SQLite to rewrite. Foreign keys
    are still switched off for the DROP TABLE fields step, since dropping a
    table while something still legitimately references it by name would
    otherwise cascade-delete every character's field values."""
    # Also returns once `project_id` is present: `fields` later dropped
    # `owner` entirely in favour of `project_id` (see
    # _migrate_fields_to_projects). Without this second check, "owner" in
    # existing" would be permanently False the moment that migration
    # runs, and this function would silently re-fire on every subsequent
    # get_conn() call, rebuilding `fields` back into this old owner-based
    # shape and destroying the project migration.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(fields)").fetchall()}
    if not existing or "owner" in existing or "project_id" in existing:
        return  # table doesn't exist yet, already migrated, or migrated further to project_id
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE fields_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "owner TEXT NOT NULL,"
        "key TEXT NOT NULL,"
        "label TEXT NOT NULL,"
        "type TEXT NOT NULL DEFAULT 'text',"
        "options TEXT,"
        "section TEXT NOT NULL DEFAULT 'Custom',"
        "is_builtin INTEGER NOT NULL DEFAULT 0,"
        "sort_order INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "UNIQUE(owner, key))"
    )
    conn.execute(
        "INSERT INTO fields_new (id, owner, key, label, type, options, section, is_builtin, sort_order, created_at) "
        "SELECT id, ?, key, label, type, options, section, is_builtin, sort_order, created_at FROM fields",
        (LEGACY_OWNER,),
    )
    conn.execute("DROP TABLE fields")
    conn.execute("ALTER TABLE fields_new RENAME TO fields")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    logger.info(f"Migrated fields table to per-owner schema; backfilled to owner={LEGACY_OWNER!r}")


def _backfill_character_owner(conn: sqlite3.Connection):
    conn.execute("UPDATE characters SET owner = ? WHERE owner = ''", (LEGACY_OWNER,))
    conn.commit()


def _migrate_fields_to_projects(conn: sqlite3.Connection):
    """One-time rebuild: `fields`/`characters` used to be owner-scoped;
    now they sit inside `projects` (one project per book), and `fields`
    drops `owner` for `project_id` (mirrors the owner-scoping rebuild
    _migrate_fields_to_owner did earlier — same temp-table/drop/rename
    technique, for the same reason: SQLite can't ALTER a UNIQUE
    constraint). Guarded by the presence of `project_id` on `fields`, so
    it only runs once. Must run before schema.sql's executescript, same
    structural reason as always: schema.sql's fields/characters
    definitions (UNIQUE(project_id, key), the project_id index/NOT NULL)
    would crash against tables still in their pre-migration shape on disk.

    `characters.project_id` is expected to already exist (nullable) —
    added by the ADD-COLUMN _migrate_table() call earlier in get_conn()
    — since this function only backfills its values, it doesn't add the
    column itself."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(fields)").fetchall()}
    if not existing or "project_id" in existing:
        return  # table doesn't exist yet, or already migrated

    # `projects`/`project_meta` are brand-new tables with no legacy shape
    # to collide with — safe to create them here, ahead of schema.sql's
    # own executescript, since this migration needs to populate them
    # before that runs.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS projects ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "owner TEXT NOT NULL,"
        "title TEXT NOT NULL DEFAULT 'Untitled Project',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "last_opened_at TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE IF NOT EXISTS project_meta ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "key TEXT NOT NULL,"
        "label TEXT NOT NULL,"
        "value TEXT NOT NULL DEFAULT '',"
        "is_builtin INTEGER NOT NULL DEFAULT 0,"
        "sort_order INTEGER NOT NULL DEFAULT 0,"
        "UNIQUE(project_id, key));"
    )

    # Owners from actual data, not ALLOWED_OWNERS — avoids orphaning data
    # if that env var ever changes. Guard against a stray owner = '' row
    # (shouldn't exist post-_backfill_character_owner, but that function
    # runs *after* this one in get_conn(), so be defensive here too).
    owners = {
        (row[0] or "").strip()
        for row in conn.execute(
            "SELECT DISTINCT owner FROM characters UNION SELECT DISTINCT owner FROM fields"
        ).fetchall()
    }
    owners.discard("")
    if not owners:
        owners = {LEGACY_OWNER}

    project_id_by_owner = {}
    for owner in sorted(owners):
        # seed_fields=False: `fields` is still in its pre-migration
        # owner-scoped shape at this point in the function — the rebuild
        # below is what gives it a project-scoped shape to seed into.
        # project_meta has no such ordering issue (brand-new table), so
        # _create_project's project_meta seeding runs immediately.
        project_id_by_owner[owner] = _create_project(conn, owner, seed_fields=False)

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE fields_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,"
        "key TEXT NOT NULL,"
        "label TEXT NOT NULL,"
        "type TEXT NOT NULL DEFAULT 'text',"
        "options TEXT,"
        "section TEXT NOT NULL DEFAULT 'Custom',"
        "is_builtin INTEGER NOT NULL DEFAULT 0,"
        "sort_order INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "UNIQUE(project_id, key))"
    )
    old_fields = conn.execute("SELECT * FROM fields").fetchall()
    for f in old_fields:
        owner = (f["owner"] or "").strip() or LEGACY_OWNER
        project_id = project_id_by_owner[owner]
        conn.execute(
            "INSERT INTO fields_new "
            "(id, project_id, key, label, type, options, section, is_builtin, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f["id"], project_id, f["key"], f["label"], f["type"], f["options"],
             f["section"], f["is_builtin"], f["sort_order"], f["created_at"]),
        )
    conn.execute("DROP TABLE fields")
    conn.execute("ALTER TABLE fields_new RENAME TO fields")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    for owner, project_id in project_id_by_owner.items():
        conn.execute("UPDATE characters SET project_id = ? WHERE owner = ?", (project_id, owner))
    conn.commit()

    # Safety net: any project that ends up with zero fields (e.g. an
    # owner who had characters but no fields left) gets builtins seeded
    # explicitly now that `fields` is in its final project-scoped shape —
    # mirrors the existing restore-safety-net pattern of re-seeding
    # rather than assuming.
    for project_id in project_id_by_owner.values():
        count = conn.execute("SELECT COUNT(*) FROM fields WHERE project_id = ?", (project_id,)).fetchone()[0]
        if count == 0:
            _seed_builtin_fields(conn, project_id)

    logger.info(
        f"Migrated fields/characters to project-scoped schema; "
        f"created {len(project_id_by_owner)} project(s) for owners {sorted(project_id_by_owner.keys())}"
    )


# Case-insensitive bare-word/phrase -> catalog role key, used only by the
# one-time relationships migration below. Covers the spec's own examples
# plus the rest of the catalog's bare labels.
LEGACY_LABEL_TO_ROLE = {
    "father": "parent", "mother": "parent", "dad": "parent", "mom": "parent", "papa": "parent", "mama": "parent",
    "son": "child", "daughter": "child",
    "brother": "sibling", "sister": "sibling", "sibling": "sibling",
    "husband": "spouse", "wife": "spouse", "spouse": "spouse",
    "grandmother": "grandparent", "grandfather": "grandparent", "grandma": "grandparent", "grandpa": "grandparent",
    "grandson": "grandchild", "granddaughter": "grandchild",
    "aunt": "aunt_uncle", "uncle": "aunt_uncle",
    "niece": "niece_nephew", "nephew": "niece_nephew",
    "cousin": "cousin",
    "stepmother": "step_parent", "stepfather": "step_parent", "step-mother": "step_parent", "step-father": "step_parent",
    "stepson": "step_child", "stepdaughter": "step_child", "step-son": "step_child", "step-daughter": "step_child",
    "guardian": "guardian", "ward": "ward",
    "in-law": "in_law", "inlaw": "in_law",
    "partner": "partner", "fiance": "fiance", "fiancee": "fiance", "fiancé": "fiance", "fiancée": "fiance",
    "lover": "lover", "ex": "ex_partner", "ex-partner": "ex_partner", "expartner": "ex_partner",
    "friend": "friend", "best friend": "best_friend", "bestfriend": "best_friend", "best-friend": "best_friend",
    "confidant": "confidant", "confidante": "confidant",
    "acquaintance": "acquaintance", "neighbour": "neighbour", "neighbor": "neighbour",
    "colleague": "colleague", "coworker": "colleague", "co-worker": "colleague",
    "employer": "employer", "boss": "employer", "employee": "employee",
    "superior": "superior", "subordinate": "subordinate",
    "mentor": "mentor", "mentee": "mentee", "teacher": "teacher", "student": "student", "pupil": "student",
    "business partner": "business_partner", "businesspartner": "business_partner",
    "client": "client", "customer": "client", "service provider": "service_provider",
    "ally": "ally", "leader": "leader", "follower": "follower",
    "master": "master", "servant": "servant",
    "patron": "patron", "protege": "protege", "protégé": "protege",
    "captor": "captor", "captive": "captive", "prisoner": "captive",
    "rival": "rival", "enemy": "enemy", "nemesis": "nemesis",
    "betrayer": "betrayer", "betrayed": "betrayed",
}


def _resolve_legacy_relationship_role(label: str, rtype: str) -> dict:
    """label -> catalog role if it's a bare-word match (nothing lost, the
    label *was* just the role's name); otherwise a custom role carrying
    the original text, with category derived from the old `type` field
    when possible so at least the map color is sensible."""
    bare = (label or "").strip().lower()
    role_key = LEGACY_LABEL_TO_ROLE.get(bare)
    if role_key:
        return {"role_key": role_key, "custom_label": None, "category": RELATIONSHIP_ROLES[role_key]["category"]}
    custom_label = (label or "").strip() or (rtype or "").strip() or "Related"
    rtype_stripped = (rtype or "").strip()
    if rtype_stripped in ("Family", "Romantic"):
        category = rtype_stripped
    elif rtype_stripped.lower() in LEGACY_LABEL_TO_ROLE:
        category = RELATIONSHIP_ROLES[LEGACY_LABEL_TO_ROLE[rtype_stripped.lower()]]["category"]
    else:
        category = "Other"
    return {"role_key": None, "custom_label": custom_label, "category": category}


def _apply_migrated_relationship(conn: sqlite3.Connection, from_id: int, to_id: int, resolved: dict):
    if resolved["role_key"]:
        _upsert_relationship(conn, from_id, to_id, resolved["role_key"])
    else:
        _upsert_relationship(
            conn, from_id, to_id, "custom",
            custom_label=resolved["custom_label"], category_override=resolved["category"],
        )


def _direct_insert_relationship(conn: sqlite3.Connection, char_a: int, char_b: int, a_resolved: dict, b_resolved: dict):
    """Both sides of this tie are already explicit legacy data (one old
    row on each side) — build the record directly instead of running
    catalog-inverse auto-fill, which would silently produce two records
    instead of one whenever the two old labels aren't real inverses of
    each other. a_resolved is char_a's own claim about char_b
    (-> role_a_to_b); b_resolved is char_b's own claim about char_a
    (-> role_b_to_a)."""
    if a_resolved["role_key"]:
        role_a_to_b, custom_label_a_to_b = a_resolved["role_key"], None
    else:
        custom_label_a_to_b = a_resolved["custom_label"]
        role_a_to_b = f"custom:{slugify(custom_label_a_to_b)}"
    if b_resolved["role_key"]:
        role_b_to_a, custom_label_b_to_a = b_resolved["role_key"], None
    else:
        custom_label_b_to_a = b_resolved["custom_label"]
        role_b_to_a = f"custom:{slugify(custom_label_b_to_a)}"
    now = _now()
    conn.execute(
        "INSERT INTO relationships "
        "(char_a_id, char_b_id, role_a_to_b, role_b_to_a, category, custom_label_a_to_b, custom_label_b_to_a, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(char_a_id, char_b_id, role_a_to_b) DO UPDATE SET "
        "role_b_to_a=excluded.role_b_to_a, category=excluded.category, "
        "custom_label_a_to_b=excluded.custom_label_a_to_b, custom_label_b_to_a=excluded.custom_label_b_to_a, "
        "updated_at=excluded.updated_at",
        (char_a, char_b, role_a_to_b, role_b_to_a, a_resolved["category"], custom_label_a_to_b, custom_label_b_to_a, now, now),
    )
    conn.commit()


def _pair_legacy_rows(x_rows: list, y_rows: list):
    """x_rows/y_rows: resolved dicts for legacy rows in each direction of
    one pair. Returns (matched_pairs, unmatched_x, unmatched_y).

    Exactly one row on each side is unambiguous — they describe the same
    tie, merge them regardless of category (no uncertainty: there's
    nothing else on either side they could correspond to). Anything more
    ambiguous (an old pair with more than one row already on some side —
    i.e. multi-tie already present in the legacy data) falls back to
    best-effort category matching; leftovers become independent records
    via the normal catalog-inverse path."""
    if len(x_rows) == 1 and len(y_rows) == 1:
        return [(x_rows[0], y_rows[0])], [], []
    matched, used_y = [], set()
    for xr in x_rows:
        match_i = next((i for i, yr in enumerate(y_rows) if i not in used_y and yr["category"] == xr["category"]), None)
        if match_i is not None:
            used_y.add(match_i)
            matched.append((xr, y_rows[match_i]))
    unmatched_x = [xr for xr in x_rows if not any(xr is m[0] for m in matched)]
    unmatched_y = [yr for i, yr in enumerate(y_rows) if i not in used_y]
    return matched, unmatched_x, unmatched_y


def _migrate_relationships_reciprocal(conn: sqlite3.Connection):
    """One-time rebuild: relationships used to be one directional row per
    character; now it's one undirected record per pair-and-tie with
    catalog roles (see schema.sql's comment on `relationships`). Guarded
    by the presence of `char_a_id`, so it only runs once. Same
    temp-table/drop/rename mechanism as _migrate_fields_to_owner, for the
    same reason: new columns, new constraints, transformed data — not
    something the ADD-COLUMN-only _migrate_table() helper can do."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(relationships)").fetchall()}
    if not existing or "char_a_id" in existing:
        return

    legacy_rows = conn.execute(
        "SELECT character_id, related_id, label, type FROM relationships"
    ).fetchall()

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE relationships_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "char_a_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,"
        "char_b_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,"
        "role_a_to_b TEXT NOT NULL,"
        "role_b_to_a TEXT NOT NULL,"
        "category TEXT NOT NULL,"
        "custom_label_a_to_b TEXT,"
        "custom_label_b_to_a TEXT,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "CHECK(char_a_id < char_b_id),"
        "UNIQUE(char_a_id, char_b_id, role_a_to_b))"
    )
    conn.execute("DROP TABLE relationships")
    conn.execute("ALTER TABLE relationships_new RENAME TO relationships")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    dropped_null = 0
    by_pair: dict = {}
    for row in legacy_rows:
        char_id, related_id = row["character_id"], row["related_id"]
        if related_id is None:
            dropped_null += 1
            continue
        resolved = _resolve_legacy_relationship_role(row["label"], row["type"])
        pair_key = (min(char_id, related_id), max(char_id, related_id))
        by_pair.setdefault(pair_key, {}).setdefault(char_id, []).append(resolved)

    stats = {"clean": 0, "custom": 0, "bidirectional_merged": 0, "multi_tie_fallback": 0}
    for (x, y), by_char in by_pair.items():
        x_rows, y_rows = by_char.get(x, []), by_char.get(y, [])
        simple_pair = len(x_rows) == 1 and len(y_rows) == 1
        ambiguous = len(x_rows) > 1 or len(y_rows) > 1  # more than one old row on a side — genuine uncertainty
        matched, unmatched_x, unmatched_y = _pair_legacy_rows(x_rows, y_rows)
        for a_resolved, b_resolved in matched:
            _direct_insert_relationship(conn, x, y, a_resolved, b_resolved)
            stats["bidirectional_merged" if simple_pair else "multi_tie_fallback"] += 1
            for r in (a_resolved, b_resolved):
                stats["clean" if r["role_key"] else "custom"] += 1
        for r in unmatched_x:
            _apply_migrated_relationship(conn, x, y, r)
            stats["clean" if r["role_key"] else "custom"] += 1
            if ambiguous:
                stats["multi_tie_fallback"] += 1
        for r in unmatched_y:
            _apply_migrated_relationship(conn, y, x, r)
            stats["clean" if r["role_key"] else "custom"] += 1
            if ambiguous:
                stats["multi_tie_fallback"] += 1

    logger.info(
        "Migrated relationships to reciprocal schema: "
        f"{stats['clean']} clean role matches, {stats['custom']} custom roles, "
        f"{stats['bidirectional_merged']} bidirectional pairs merged cleanly, "
        f"{stats['multi_tie_fallback']} ties involved ambiguous multi-directional legacy data "
        "(resolved by best-effort category matching — worth spot-checking), "
        f"{dropped_null} rows dropped (no target character was ever set)"
    )


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # These must run BEFORE executescript below: schema.sql's indexes and
    # constraints reference columns a pre-migration database's `fields`/
    # `characters`/`relationships` tables won't have yet — CREATE
    # INDEX/the table's own CHECK would crash against a table still
    # missing those columns.
    _migrate_fields_to_owner(conn)
    _migrate_table(conn, "characters", CHARACTERS_COLUMNS)
    _migrate_relationships_reciprocal(conn)
    _migrate_fields_to_projects(conn)
    conn.executescript(Path("schema.sql").read_text())
    _backfill_character_owner(conn)
    _migrate_table(conn, "character_images", CHARACTER_IMAGES_COLUMNS)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_current_owner(request: Request) -> str:
    """Not a security check — Tailscale is the access boundary (see README).
    This just resolves which person's data a request should see."""
    owner = request.cookies.get(OWNER_COOKIE)
    if not owner or owner not in ALLOWED_OWNERS:
        raise HTTPException(status_code=401, detail="Not logged in")
    return owner


def get_current_project(request: Request, owner: str = Depends(get_current_owner)) -> int:
    """Resolves which project a request should see, three cases:
    - Cookie present and resolves to one of this owner's projects -> use it.
    - Cookie present but doesn't resolve (wrong id, another owner's
      project, tampering) -> hard 404. Not silently papered over — either
      a real bug or a forged cookie, both worth surfacing.
    - No cookie -> fall back to the most-recently-opened project (covers
      cleared cookies / a new device), or transparently provision a first
      project if this owner has none yet (mirrors the old login-time
      auto-provisioning). This fallback deliberately doesn't set the
      cookie itself — the frontend boot sequence calls
      POST /projects/{id}/activate explicitly; this is just a safety net
      for requests that arrive before that's happened."""
    conn = get_conn()
    try:
        raw = request.cookies.get(PROJECT_COOKIE)
        if raw:
            try:
                project_id = int(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid project")
            row = conn.execute(
                "SELECT id FROM projects WHERE id = ? AND owner = ?", (project_id, owner)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")
            return project_id
        row = conn.execute(
            "SELECT id FROM projects WHERE owner = ? ORDER BY last_opened_at DESC LIMIT 1", (owner,)
        ).fetchone()
        if row:
            return row["id"]
        return _create_project(conn, owner)
    finally:
        conn.close()


def _get_owned_character(conn: sqlite3.Connection, character_id: int, owner: str, project_id: int) -> Optional[sqlite3.Row]:
    """Centralized character-ownership check — every route that reads or
    writes a specific character by id goes through this, so project
    scoping can't silently drift out of sync at one call site while the
    others stay correct (see the Projects plan's stress-test findings)."""
    return conn.execute(
        "SELECT * FROM characters WHERE id = ? AND owner = ? AND project_id = ?",
        (character_id, owner, project_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    username: str


class ProjectIn(BaseModel):
    title: Optional[str] = "Untitled Project"


class ProjectUpdate(BaseModel):
    title: Optional[str] = None


class ProjectMetaIn(BaseModel):
    label: str
    value: Optional[str] = ""


class ProjectMetaUpdate(BaseModel):
    label: Optional[str] = None
    value: Optional[str] = None


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
    type: Optional[str] = None
    options: Optional[list] = None
    sort_order: Optional[int] = None
    section: Optional[str] = None


class RelationshipIn(BaseModel):
    from_id: int
    to_id: int
    role: str  # catalog key, or "custom"
    custom_label: Optional[str] = None
    custom_inverse_label: Optional[str] = None
    category: Optional[str] = None  # only meaningful when role == "custom"


class RelationshipUpdate(BaseModel):
    from_id: int
    role: str
    custom_label: Optional[str] = None
    custom_inverse_label: Optional[str] = None
    category: Optional[str] = None


class RestoreIn(BaseModel):
    confirm: bool = False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _field_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "key": row["key"],
        "label": row["label"],
        "type": row["type"],
        "options": json.loads(row["options"]) if row["options"] else None,
        "section": row["section"],
        "is_builtin": bool(row["is_builtin"]),
        "sort_order": row["sort_order"],
    }


def _all_fields(conn: sqlite3.Connection, project_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM fields WHERE project_id = ? ORDER BY sort_order, id", (project_id,)
    ).fetchall()
    return [_field_row_to_dict(r) for r in rows]


def _create_field(
    conn: sqlite3.Connection, project_id: int, label: str, ftype: str = "text",
    options: Optional[list] = None, section: str = "Custom", is_builtin: bool = False,
) -> dict:
    """Shared by POST /fields and the document-import commit path (Part
    B) — both need "make this label a standard field in this project,
    de-duping the key" and nothing more."""
    base_key = slugify(label)
    key = base_key
    existing_keys = {
        r["key"] for r in conn.execute("SELECT key FROM fields WHERE project_id = ?", (project_id,)).fetchall()
    }
    suffix = 2
    while key in existing_keys:
        key = f"{base_key}_{suffix}"
        suffix += 1
    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM fields WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO fields (project_id, key, label, type, options, section, is_builtin, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id, key, label.strip(), ftype,
            json.dumps(options) if options else None,
            (section or "Custom").strip() or "Custom",
            1 if is_builtin else 0,
            max_sort + 1,
        ),
    )
    conn.commit()
    return _field_row_to_dict(conn.execute("SELECT * FROM fields WHERE id = ?", (cur.lastrowid,)).fetchone())


def _project_row_to_dict(row: sqlite3.Row, char_count: Optional[int] = None) -> dict:
    d = {
        "id": row["id"],
        "owner": row["owner"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_opened_at": row["last_opened_at"],
    }
    if char_count is not None:
        d["character_count"] = char_count
    return d


def _project_meta_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "key": row["key"],
        "label": row["label"],
        "value": row["value"],
        "is_builtin": bool(row["is_builtin"]),
        "sort_order": row["sort_order"],
    }


def _project_meta_list(conn: sqlite3.Connection, project_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM project_meta WHERE project_id = ? ORDER BY sort_order, id", (project_id,)
    ).fetchall()
    return [_project_meta_row_to_dict(r) for r in rows]


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
    """Resolves each shared relationship record to character_id's own
    perspective: if it's char_a, show (char_b, role_a_to_b); if char_b,
    show (char_a, role_b_to_a) — see the schema.sql comment on
    `relationships` for the role_a_to_b/role_b_to_a definitions."""
    rows = conn.execute(
        "SELECT r.id AS id, "
        "CASE WHEN r.char_a_id = ? THEN r.char_b_id ELSE r.char_a_id END AS related_id, "
        "CASE WHEN r.char_a_id = ? THEN r.role_a_to_b ELSE r.role_b_to_a END AS role_key, "
        "CASE WHEN r.char_a_id = ? THEN r.custom_label_a_to_b ELSE r.custom_label_b_to_a END AS custom_label, "
        "r.category AS category, c.name AS related_name "
        "FROM relationships r "
        "JOIN characters c ON c.id = (CASE WHEN r.char_a_id = ? THEN r.char_b_id ELSE r.char_a_id END) "
        "WHERE r.char_a_id = ? OR r.char_b_id = ? ORDER BY r.id",
        (character_id, character_id, character_id, character_id, character_id, character_id),
    ).fetchall()
    return [
        {
            "id": r["id"], "related_id": r["related_id"], "related_name": r["related_name"],
            "role_key": r["role_key"], "role_label": relationship_role_label(r["role_key"], r["custom_label"]),
            "category": r["category"],
        }
        for r in rows
    ]


def _character_detail(conn: sqlite3.Connection, character_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "owner": row["owner"],
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


def build_export(conn: sqlite3.Connection, owner: Optional[str] = None, project_id: Optional[int] = None) -> dict:
    """project_id scopes to a single project (used by a future per-project
    export); owner (with project_id=None) scopes to all of that owner's
    projects (used by the user-facing "download snapshot" export);
    neither dumps every owner's every project (used for the
    whole-household Drive backup). Output shape: {"exported_at", "projects":
    [{"id", "owner", "title", "meta", "fields", "characters"}, ...]}."""
    if project_id is not None:
        project_rows = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchall()
    elif owner is not None:
        project_rows = conn.execute("SELECT * FROM projects WHERE owner = ? ORDER BY id", (owner,)).fetchall()
    else:
        project_rows = conn.execute("SELECT * FROM projects ORDER BY owner, id").fetchall()

    projects = []
    for p in project_rows:
        pid = p["id"]
        char_rows = conn.execute("SELECT * FROM characters WHERE project_id = ? ORDER BY id", (pid,)).fetchall()
        projects.append({
            "id": pid,
            "owner": p["owner"],
            "title": p["title"],
            "meta": _project_meta_list(conn, pid),
            "fields": _all_fields(conn, pid),
            "characters": [_character_detail(conn, r["id"]) for r in char_rows],
        })
    return {
        "exported_at": _now(),
        "projects": projects,
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
# Auth — a "who's logging in?" picker, not a password. Tailscale is the real
# access boundary (see README); this only separates each person's data.
# ---------------------------------------------------------------------------

@app.get("/users")
def list_users():
    return [{"username": u} for u in ALLOWED_OWNERS]


@app.get("/me")
def me(owner: str = Depends(get_current_owner)):
    return {"username": owner}


@app.post("/login")
def login(body: LoginIn, response: Response):
    if body.username not in ALLOWED_OWNERS:
        raise HTTPException(status_code=400, detail="Unknown user")
    # No DB writes here anymore — builtin-field/project_meta seeding now
    # happens at project-creation time (see _create_project), not
    # login-time, since fields are project-scoped rather than
    # owner-scoped. get_current_project's zero-projects fallback
    # transparently provisions a first project on this owner's next
    # request if they don't have one yet.
    response.set_cookie(
        OWNER_COOKIE, body.username, max_age=365 * 24 * 3600, httponly=True, samesite="lax"
    )
    return {"username": body.username}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(OWNER_COOKIE)
    response.delete_cookie(PROJECT_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Projects — one per book/novella (see schema.sql's comment on `projects`
# for how this relates to fields/characters/relationships).
# ---------------------------------------------------------------------------

@app.get("/projects")
def list_projects(owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE owner = ? ORDER BY last_opened_at DESC", (owner,)
        ).fetchall()
        counts = {
            r["project_id"]: r["n"] for r in conn.execute(
                "SELECT project_id, COUNT(*) AS n FROM characters WHERE owner = ? GROUP BY project_id", (owner,)
            ).fetchall()
        }
        return [_project_row_to_dict(r, char_count=counts.get(r["id"], 0)) for r in rows]
    finally:
        conn.close()


@app.post("/projects", status_code=201)
def create_project_route(body: ProjectIn, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        title = (body.title or "").strip() or "Untitled Project"
        project_id = _create_project(conn, owner, title)
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        result = _project_row_to_dict(row, char_count=0)
        result["meta"] = _project_meta_list(conn, project_id)
    finally:
        conn.close()
    return result


def _get_owned_project(conn: sqlite3.Connection, project_id: int, owner: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects WHERE id = ? AND owner = ?", (project_id, owner)
    ).fetchone()


@app.get("/projects/{project_id}")
def get_project(project_id: int, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        row = _get_owned_project(conn, project_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        char_count = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        result = _project_row_to_dict(row, char_count=char_count)
        result["meta"] = _project_meta_list(conn, project_id)
        return result
    finally:
        conn.close()


@app.put("/projects/{project_id}")
def update_project(project_id: int, body: ProjectUpdate, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        row = _get_owned_project(conn, project_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        if body.title is not None and body.title.strip():
            conn.execute(
                "UPDATE projects SET title = ?, updated_at = ? WHERE id = ?",
                (body.title.strip(), _now(), project_id),
            )
            conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        char_count = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        result = _project_row_to_dict(row, char_count=char_count)
        result["meta"] = _project_meta_list(conn, project_id)
        return result
    finally:
        conn.close()


@app.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        row = _get_owned_project(conn, project_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        char_rows = conn.execute(
            "SELECT id FROM characters WHERE project_id = ?", (project_id,)
        ).fetchall()
        for c in char_rows:
            images = conn.execute(
                "SELECT public_id FROM character_images WHERE character_id = ?", (c["id"],)
            ).fetchall()
            for img in images:
                try:
                    cloudinary_images.destroy_image(img["public_id"])
                except Exception as e:
                    logger.warning(f"Could not destroy Cloudinary asset on project delete: {e}")
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))  # cascades characters/fields/relationships
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


@app.post("/projects/{project_id}/activate")
def activate_project(project_id: int, response: Response, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        row = _get_owned_project(conn, project_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        conn.execute("UPDATE projects SET last_opened_at = ? WHERE id = ?", (_now(), project_id))
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        PROJECT_COOKIE, str(project_id), max_age=365 * 24 * 3600, httponly=True, samesite="lax"
    )
    return {"id": project_id}


@app.post("/projects/{project_id}/meta", status_code=201)
def create_project_meta(project_id: int, body: ProjectMetaIn, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        row = _get_owned_project(conn, project_id, owner)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        if not body.label or not body.label.strip():
            raise HTTPException(status_code=400, detail="Label is required")
        base_key = slugify(body.label)
        key = base_key
        existing_keys = {
            r["key"] for r in conn.execute(
                "SELECT key FROM project_meta WHERE project_id = ?", (project_id,)
            ).fetchall()
        }
        suffix = 2
        while key in existing_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM project_meta WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO project_meta (project_id, key, label, value, is_builtin, sort_order) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (project_id, key, body.label.strip(), body.value or "", max_sort + 1),
        )
        conn.commit()
        meta_row = conn.execute("SELECT * FROM project_meta WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    schedule_backup()
    return _project_meta_row_to_dict(meta_row)


@app.put("/projects/{project_id}/meta/{meta_key}")
def update_project_meta(project_id: int, meta_key: str, body: ProjectMetaUpdate, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        proj_row = _get_owned_project(conn, project_id, owner)
        if not proj_row:
            raise HTTPException(status_code=404, detail="Project not found")
        row = conn.execute(
            "SELECT * FROM project_meta WHERE project_id = ? AND key = ?", (project_id, meta_key)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project meta key not found")
        label = body.label.strip() if body.label is not None and body.label.strip() else row["label"]
        value = body.value if body.value is not None else row["value"]
        conn.execute(
            "UPDATE project_meta SET label = ?, value = ? WHERE id = ?", (label, value, row["id"])
        )
        conn.commit()
        meta_row = conn.execute("SELECT * FROM project_meta WHERE id = ?", (row["id"],)).fetchone()
    finally:
        conn.close()
    schedule_backup()
    return _project_meta_row_to_dict(meta_row)


@app.delete("/projects/{project_id}/meta/{meta_key}", status_code=204)
def delete_project_meta(project_id: int, meta_key: str, owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        proj_row = _get_owned_project(conn, project_id, owner)
        if not proj_row:
            raise HTTPException(status_code=404, detail="Project not found")
        row = conn.execute(
            "SELECT id FROM project_meta WHERE project_id = ? AND key = ?", (project_id, meta_key)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project meta key not found")
        conn.execute("DELETE FROM project_meta WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------

@app.get("/characters")
def list_characters(owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM characters WHERE owner = ? AND project_id = ? ORDER BY id DESC", (owner, project_id)
        ).fetchall()
        all_values = conn.execute(
            "SELECT cv.character_id AS character_id, f.key AS key, cv.value AS value "
            "FROM character_values cv "
            "JOIN fields f ON f.id = cv.field_id "
            "JOIN characters c ON c.id = cv.character_id "
            "WHERE c.owner = ? AND c.project_id = ?",
            (owner, project_id),
        ).fetchall()
        values_by_char: dict = {}
        for v in all_values:
            values_by_char.setdefault(v["character_id"], {})[v["key"]] = v["value"]

        all_tags = conn.execute(
            "SELECT ct.character_id AS character_id, ct.tag AS tag "
            "FROM character_tags ct JOIN characters c ON c.id = ct.character_id "
            "WHERE c.owner = ? AND c.project_id = ? ORDER BY ct.id",
            (owner, project_id),
        ).fetchall()
        tags_by_char: dict = {}
        for t in all_tags:
            tags_by_char.setdefault(t["character_id"], []).append(t["tag"])

        return [_character_card(conn, r, values_by_char, tags_by_char) for r in rows]
    finally:
        conn.close()


@app.get("/characters/{character_id}")
def get_character(character_id: int, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = _get_owned_character(conn, character_id, owner, project_id)
        if not row:
            raise HTTPException(status_code=404, detail="Character not found")
        detail = _character_detail(conn, character_id)
        return detail
    finally:
        conn.close()


def _upsert_values(conn: sqlite3.Connection, character_id: int, project_id: int, values: dict):
    """project_id should come from the character's OWN row (not blindly
    from the request's active-project cookie) — defense in depth so a
    caller that already validated ownership elsewhere can't accidentally
    write values against the wrong project's field ids."""
    if not values:
        return
    field_rows = conn.execute("SELECT id, key FROM fields WHERE project_id = ?", (project_id,)).fetchall()
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
def create_character(body: CharacterIn, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO characters (owner, project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (owner, project_id, (body.name or "").strip(), now, now),
        )
        character_id = cur.lastrowid
        conn.commit()
        _upsert_values(conn, character_id, project_id, body.values or {})
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.put("/characters/{character_id}")
def update_character(character_id: int, body: CharacterUpdate, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = _get_owned_character(conn, character_id, owner, project_id)
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
        _upsert_values(conn, character_id, row["project_id"], body.values or {})
        if body.tags is not None:
            _replace_tags(conn, character_id, body.tags)
        detail = _character_detail(conn, character_id)
    finally:
        conn.close()
    schedule_backup()
    return detail


@app.delete("/characters/{character_id}", status_code=204)
def delete_character(character_id: int, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = _get_owned_character(conn, character_id, owner, project_id)
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
async def upload_images(
    character_id: int, files: list[UploadFile] = File(...),
    owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project),
):
    conn = get_conn()
    try:
        char_row = _get_owned_character(conn, character_id, owner, project_id)
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
def delete_image(character_id: int, image_id: int, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        char_row = _get_owned_character(conn, character_id, owner, project_id)
        if not char_row:
            raise HTTPException(status_code=404, detail="Character not found")
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
def set_primary_image(character_id: int, image_id: int, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        char_row = _get_owned_character(conn, character_id, owner, project_id)
        if not char_row:
            raise HTTPException(status_code=404, detail="Character not found")
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
# Relationships — reciprocal, catalog-based (see schema.sql's comment on
# `relationships` for the char_a/char_b + role_a_to_b/role_b_to_a model).
# ---------------------------------------------------------------------------

def _check_same_owner_character(conn: sqlite3.Connection, character_id: Optional[int], owner: str, project_id: int):
    if character_id is None:
        return
    row = conn.execute(
        "SELECT id FROM characters WHERE id = ? AND owner = ? AND project_id = ?", (character_id, owner, project_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Must be one of your own characters")


def _resolve_relationship_sides(
    from_id: int,
    to_id: int,
    role_key: str,
    custom_label: Optional[str] = None,
    custom_inverse_label: Optional[str] = None,
    category_override: Optional[str] = None,
) -> dict:
    """Canonicalizes the pair and derives both sides' roles from a single
    (from_id, to_id, role) input — the write-path algorithm shared by
    create and edit. Custom roles are keyed as "custom:<slug-of-label>",
    not a bare "custom" sentinel — two different custom ties between the
    same pair need distinct keys, or the second one collides with the
    first under the UNIQUE(char_a,char_b,role_a_to_b) constraint."""
    if from_id == to_id:
        raise HTTPException(status_code=400, detail="A character can't have a relationship with themselves")

    if role_key == "custom":
        own_label = (custom_label or "").strip() or "Related"
        own_key = f"custom:{slugify(own_label)}"
        other_label = (custom_inverse_label or "").strip() or own_label
        other_key = f"custom:{slugify(other_label)}"
        category = (category_override or "Other").strip() or "Other"
    else:
        if role_key not in RELATIONSHIP_ROLES:
            raise HTTPException(status_code=400, detail=f"Unknown relationship role {role_key!r}")
        own_key, own_label = role_key, None
        other_key, other_label = RELATIONSHIP_ROLES[role_key]["inverse"], None
        category = RELATIONSHIP_ROLES[role_key]["category"]

    char_a, char_b = (from_id, to_id) if from_id < to_id else (to_id, from_id)
    if from_id == char_a:
        role_a_to_b, role_b_to_a = own_key, other_key
        custom_label_a_to_b, custom_label_b_to_a = own_label, other_label
    else:
        role_b_to_a, role_a_to_b = own_key, other_key
        custom_label_b_to_a, custom_label_a_to_b = own_label, other_label

    return {
        "char_a": char_a, "char_b": char_b,
        "role_a_to_b": role_a_to_b, "role_b_to_a": role_b_to_a, "category": category,
        "custom_label_a_to_b": custom_label_a_to_b, "custom_label_b_to_a": custom_label_b_to_a,
    }


def _upsert_relationship(
    conn: sqlite3.Connection,
    from_id: int,
    to_id: int,
    role_key: str,
    custom_label: Optional[str] = None,
    custom_inverse_label: Optional[str] = None,
    category_override: Optional[str] = None,
) -> int:
    """The reciprocity engine for *creating* ties — reused by POST
    /relationships, the relationships migration, and restore_data().
    Dedupes on (char_a, char_b, computed role_a_to_b): a second call
    describing the same tie edits the existing record instead of
    duplicating it. NOT used for PUT-by-id edits (see update_relationship)
    — dedupe-by-computed-key is wrong there, since changing an existing
    tie's role changes the dedup key itself and would silently create a
    second row instead of updating the one being edited."""
    sides = _resolve_relationship_sides(from_id, to_id, role_key, custom_label, custom_inverse_label, category_override)
    existing = conn.execute(
        "SELECT id FROM relationships WHERE char_a_id = ? AND char_b_id = ? AND role_a_to_b = ?",
        (sides["char_a"], sides["char_b"], sides["role_a_to_b"]),
    ).fetchone()
    now = _now()
    if existing:
        conn.execute(
            "UPDATE relationships SET role_b_to_a = ?, category = ?, "
            "custom_label_a_to_b = ?, custom_label_b_to_a = ?, updated_at = ? WHERE id = ?",
            (sides["role_b_to_a"], sides["category"], sides["custom_label_a_to_b"], sides["custom_label_b_to_a"], now, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO relationships "
        "(char_a_id, char_b_id, role_a_to_b, role_b_to_a, category, custom_label_a_to_b, custom_label_b_to_a, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sides["char_a"], sides["char_b"], sides["role_a_to_b"], sides["role_b_to_a"], sides["category"],
         sides["custom_label_a_to_b"], sides["custom_label_b_to_a"], now, now),
    )
    conn.commit()
    return cur.lastrowid


def _relationship_owner_row(conn: sqlite3.Connection, relationship_id: int, owner: str, project_id: int) -> Optional[sqlite3.Row]:
    """Checks BOTH char_a and char_b's owner+project (not just char_a's)
    — the write-time invariant is that both sides always agree, but this
    is the read-side defense-in-depth for that invariant rather than
    blind trust in it, matching what create_relationship already checks
    on both from_id/to_id via _check_same_owner_character."""
    return conn.execute(
        "SELECT r.* FROM relationships r "
        "JOIN characters ca ON ca.id = r.char_a_id "
        "JOIN characters cb ON cb.id = r.char_b_id "
        "WHERE r.id = ? AND ca.owner = ? AND ca.project_id = ? AND cb.owner = ? AND cb.project_id = ?",
        (relationship_id, owner, project_id, owner, project_id),
    ).fetchone()


@app.get("/relationship_roles")
def list_relationship_roles():
    return {
        "categories": [{"name": name, "color": color} for name, color in CATEGORY_COLORS.items()],
        "roles": [
            {"key": key, "label": r["label"], "category": r["category"], "inverse": r["inverse"]}
            for key, r in RELATIONSHIP_ROLES.items()
        ],
    }


def _relationship_dict(conn: sqlite3.Connection, relationship_id: int, from_perspective: int) -> dict:
    rows = _character_relationships(conn, from_perspective)
    return next(r for r in rows if r["id"] == relationship_id)


@app.post("/relationships", status_code=201)
def create_relationship(body: RelationshipIn, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        _check_same_owner_character(conn, body.from_id, owner, project_id)
        _check_same_owner_character(conn, body.to_id, owner, project_id)
        rel_id = _upsert_relationship(
            conn, body.from_id, body.to_id, body.role,
            custom_label=body.custom_label, custom_inverse_label=body.custom_inverse_label,
            category_override=body.category,
        )
        result = _relationship_dict(conn, rel_id, body.from_id)
    finally:
        conn.close()
    schedule_backup()
    return result


@app.put("/relationships/{relationship_id}")
def update_relationship(relationship_id: int, body: RelationshipUpdate, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = _relationship_owner_row(conn, relationship_id, owner, project_id)
        if not row:
            raise HTTPException(status_code=404, detail="Relationship not found")
        if body.from_id not in (row["char_a_id"], row["char_b_id"]):
            raise HTTPException(status_code=400, detail="from_id must be one of this relationship's two characters")
        to_id = row["char_b_id"] if body.from_id == row["char_a_id"] else row["char_a_id"]
        sides = _resolve_relationship_sides(
            body.from_id, to_id, body.role,
            custom_label=body.custom_label, custom_inverse_label=body.custom_inverse_label,
            category_override=body.category,
        )
        try:
            conn.execute(
                "UPDATE relationships SET role_a_to_b = ?, role_b_to_a = ?, category = ?, "
                "custom_label_a_to_b = ?, custom_label_b_to_a = ?, updated_at = ? WHERE id = ?",
                (sides["role_a_to_b"], sides["role_b_to_a"], sides["category"],
                 sides["custom_label_a_to_b"], sides["custom_label_b_to_a"], _now(), relationship_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail="A relationship with that role already exists between these two characters — edit that one instead.",
            )
        result = _relationship_dict(conn, relationship_id, body.from_id)
    finally:
        conn.close()
    schedule_backup()
    return result


@app.delete("/relationships/{relationship_id}", status_code=204)
def delete_relationship(relationship_id: int, owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = _relationship_owner_row(conn, relationship_id, owner, project_id)
        if not row:
            raise HTTPException(status_code=404, detail="Relationship not found")
        conn.execute("DELETE FROM relationships WHERE id = ?", (relationship_id,))
        conn.commit()
    finally:
        conn.close()
    schedule_backup()


@app.get("/relationships")
def list_relationships(owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    """Every relationship record across the active project's whole cast
    in one call — used by the character map, which needs the full graph
    rather than one character's relationships at a time. Scoped by both
    sides' project_id (not just char_a's) as read-side defense-in-depth
    — see the DB trigger in schema.sql for the write-side backstop."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT r.id AS id, r.char_a_id AS char_a_id, ca.name AS char_a_name, "
            "r.char_b_id AS char_b_id, cb.name AS char_b_name, "
            "r.role_a_to_b AS role_a_to_b, r.role_b_to_a AS role_b_to_a, "
            "r.custom_label_a_to_b AS custom_label_a_to_b, r.custom_label_b_to_a AS custom_label_b_to_a, "
            "r.category AS category "
            "FROM relationships r "
            "JOIN characters ca ON ca.id = r.char_a_id "
            "JOIN characters cb ON cb.id = r.char_b_id "
            "WHERE ca.owner = ? AND ca.project_id = ? AND cb.project_id = ?",
            (owner, project_id, project_id),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "char_a_id": r["char_a_id"], "char_a_name": r["char_a_name"],
                "char_b_id": r["char_b_id"], "char_b_name": r["char_b_name"],
                "role_a_to_b": r["role_a_to_b"], "role_b_to_a": r["role_b_to_a"],
                "role_a_to_b_label": relationship_role_label(r["role_a_to_b"], r["custom_label_a_to_b"]),
                "role_b_to_a_label": relationship_role_label(r["role_b_to_a"], r["custom_label_b_to_a"]),
                "category": r["category"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

@app.get("/fields")
def list_fields(project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        return _all_fields(conn, project_id)
    finally:
        conn.close()


@app.post("/fields", status_code=201)
def create_field(body: FieldIn, project_id: int = Depends(get_current_project)):
    if not body.label or not body.label.strip():
        raise HTTPException(status_code=400, detail="Label is required")
    if body.type not in VALID_FIELD_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type, must be one of {sorted(VALID_FIELD_TYPES)}")
    if body.type == "select" and not body.options:
        raise HTTPException(status_code=400, detail="Select fields require at least one option")

    conn = get_conn()
    try:
        field = _create_field(conn, project_id, body.label, body.type, body.options, body.section or "Custom")
    finally:
        conn.close()
    schedule_backup()
    return field


@app.put("/fields/{field_id}")
def update_field(field_id: int, body: FieldUpdate, project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM fields WHERE id = ? AND project_id = ?", (field_id, project_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")

        label = body.label.strip() if body.label is not None and body.label.strip() else row["label"]
        field_type = row["type"]
        if body.type is not None:
            if body.type not in VALID_FIELD_TYPES:
                raise HTTPException(status_code=400, detail=f"Invalid type, must be one of {sorted(VALID_FIELD_TYPES)}")
            if body.type == "select" and not (body.options or row["options"]):
                raise HTTPException(status_code=400, detail="Select fields require at least one option")
            field_type = body.type
        options = json.dumps(body.options) if body.options is not None else row["options"]
        sort_order = body.sort_order if body.sort_order is not None else row["sort_order"]
        section = body.section.strip() if body.section is not None and body.section.strip() else row["section"]

        conn.execute(
            "UPDATE fields SET label = ?, type = ?, options = ?, sort_order = ?, section = ? WHERE id = ?",
            (label, field_type, options, sort_order, section, field_id),
        )
        conn.commit()
        field = _field_row_to_dict(conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone())
    finally:
        conn.close()
    schedule_backup()
    return field


@app.delete("/fields/{field_id}", status_code=204)
def delete_field(field_id: int, project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM fields WHERE id = ? AND project_id = ?", (field_id, project_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Field not found")
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
def export_data(owner: str = Depends(get_current_owner)):
    conn = get_conn()
    try:
        return build_export(conn, owner)
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
        conn.execute("DELETE FROM project_meta")
        conn.execute("DELETE FROM projects")
        conn.commit()

        restored_project_ids = []
        for proj in export.get("projects", []):
            proj_owner = proj.get("owner") or LEGACY_OWNER
            now = _now()
            cur = conn.execute(
                "INSERT INTO projects (owner, title, created_at, updated_at, last_opened_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (proj_owner, proj.get("title") or "Untitled Project", now, now, now),
            )
            project_id = cur.lastrowid
            restored_project_ids.append(project_id)

            for m in proj.get("meta", []):
                conn.execute(
                    "INSERT INTO project_meta (project_id, key, label, value, is_builtin, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, m["key"], m.get("label", m["key"]), m.get("value", ""),
                     1 if m.get("is_builtin") else 0, m.get("sort_order", 0)),
                )

            field_id_by_key = {}
            for f in proj.get("fields", []):
                cur = conn.execute(
                    "INSERT INTO fields (project_id, key, label, type, options, section, is_builtin, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id, f["key"], f["label"], f["type"],
                        json.dumps(f["options"]) if f.get("options") else None,
                        f.get("section", "Custom"),
                        1 if f.get("is_builtin") else 0,
                        f.get("sort_order", 0),
                    ),
                )
                field_id_by_key[f["key"]] = cur.lastrowid

            old_to_new_char_id = {}
            for c in proj.get("characters", []):
                cur = conn.execute(
                    "INSERT INTO characters (owner, project_id, name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (proj_owner, project_id, c["name"], c.get("created_at", _now()), c.get("updated_at", _now())),
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

            # Both characters' exports describe the same shared
            # relationship record, so _upsert_relationship naturally
            # dedupes processing it from each side rather than creating
            # two rows. Both characters are always in old_to_new_char_id
            # together (same project loop), so this never spans projects.
            for c in proj.get("characters", []):
                new_character_id = old_to_new_char_id.get(c["id"])
                for rel in c.get("relationships") or []:
                    new_related_id = old_to_new_char_id.get(rel.get("related_id"))
                    if new_character_id is None or new_related_id is None:
                        continue
                    role_key = rel.get("role_key") or ""
                    if role_key.startswith("custom:"):
                        _upsert_relationship(
                            conn, new_character_id, new_related_id, "custom",
                            custom_label=rel.get("role_label"), category_override=rel.get("category"),
                        )
                    elif role_key:
                        _upsert_relationship(conn, new_character_id, new_related_id, role_key)

        conn.commit()
        # no-op per project if fields already restored, safety net if a
        # project's export was empty
        for project_id in restored_project_ids:
            count = conn.execute("SELECT COUNT(*) FROM fields WHERE project_id = ?", (project_id,)).fetchone()[0]
            if count == 0:
                _seed_builtin_fields(conn, project_id)
    finally:
        conn.close()
    return {"restored": True, "exported_at": export.get("exported_at")}


@app.get("/status")
def status(owner: str = Depends(get_current_owner), project_id: int = Depends(get_current_project)):
    conn = get_conn()
    try:
        char_count = conn.execute(
            "SELECT COUNT(*) FROM characters WHERE owner = ? AND project_id = ?", (owner, project_id)
        ).fetchone()[0]
        field_count = conn.execute(
            "SELECT COUNT(*) FROM fields WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
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
