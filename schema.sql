-- ============================================================
-- PROJECTS — one per book/novella. Fields, characters, and
-- relationships all live inside exactly one project (see below).
-- `owner` is who the project belongs to, e.g. "Fergus" / "Esme" —
-- projects are not shared between people, only characters/fields
-- moved from being owner-scoped to being project-scoped.
-- `last_opened_at` drives the "resume where I left off" fallback
-- in get_current_project() when no active-project cookie is set.
-- ============================================================
CREATE TABLE IF NOT EXISTS projects (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner          TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT 'Untitled Project',
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    last_opened_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner);

-- ============================================================
-- PROJECT_META — flexible key/value for project-level info
-- (setting, form, viewpoint, logline, notes). Same shape/pattern
-- as `fields`+`character_values`, just one row per project instead
-- of per-character. Built-in keys are seeded on project creation
-- (see _create_project() in app.py); authors can add custom ones
-- the same way they add character fields.
-- ============================================================
CREATE TABLE IF NOT EXISTS project_meta (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    label       TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    is_builtin  INTEGER NOT NULL DEFAULT 0, -- informational only, like fields.is_builtin — doesn't block deletion
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, key)
);

CREATE INDEX IF NOT EXISTS idx_project_meta_project ON project_meta(project_id);

-- ============================================================
-- FIELDS — master list of profile fields, built-in and custom.
-- Adding a row here is what makes a new field "standard" across
-- every character *in the same project* (see character_values
-- below) — no ALTER TABLE needed per custom field, that's the
-- whole point of the EAV design. `section` groups fields in the
-- detail view (Identity / Description / Notes / Custom, or any
-- name the owner picks). `project_id` separates each project's
-- field set — keys are only unique within a project, not
-- globally, since every project gets identically-keyed built-ins
-- (e.g. "age") seeded independently.
-- ============================================================
CREATE TABLE IF NOT EXISTS fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,             -- slug, e.g. "physical_description"
    label       TEXT NOT NULL,             -- display name, e.g. "Physical description"
    type        TEXT NOT NULL DEFAULT 'text',  -- text | textarea | number | select | date | color
    options     TEXT,                      -- JSON array of strings, only meaningful for type='select'
    section     TEXT NOT NULL DEFAULT 'Custom',
    is_builtin  INTEGER NOT NULL DEFAULT 0, -- built-ins can't be deleted
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, key)
);

CREATE INDEX IF NOT EXISTS idx_fields_project_sort_order ON fields(project_id, sort_order);

-- ============================================================
-- CHARACTERS — one row per cast member. Everything else about a
-- character (field values, images, tags, relationships) lives in
-- its own table; this one only holds what's true regardless of
-- the current field set. Name is allowed blank (shown as
-- "Unnamed"/"Untitled") — a brand-new character exists the moment
-- "+ New" is clicked, before it's been named. `owner` is who the
-- character belongs to, e.g. "Fergus" / "Esme"; `project_id` is
-- which book it belongs to. `owner` is kept (rather than derived
-- solely via project_id) because it's cheap to keep and lets
-- owner-scoped queries (e.g. login) avoid an extra join — a
-- character's owner and its project's owner must always agree.
-- ============================================================
CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL DEFAULT '',
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_characters_owner ON characters(owner);
CREATE INDEX IF NOT EXISTS idx_characters_project ON characters(project_id);

-- ============================================================
-- CHARACTER_VALUES — EAV table: one row per (character, field).
-- This is what lets a brand-new custom field apply to every
-- existing character instantly — no schema change, no backfill,
-- the row just doesn't exist until a value is saved (reads treat
-- a missing row as an empty value for that field).
-- ============================================================
CREATE TABLE IF NOT EXISTS character_values (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    field_id      INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    value         TEXT,   -- numbers/dates stored as text, parsed on read per field.type
    UNIQUE(character_id, field_id)
);

CREATE INDEX IF NOT EXISTS idx_character_values_character ON character_values(character_id);
CREATE INDEX IF NOT EXISTS idx_character_values_field ON character_values(field_id);

-- ============================================================
-- CHARACTER_IMAGES — a character can have multiple portraits;
-- exactly one (is_primary) is what the card/detail header shows.
-- Deliberate departure from a single portrait_url column on
-- characters (see HANDOFF.md) — the owner asked for "one primary
-- + others". Each row is an independent Cloudinary asset so
-- deleting/replacing one never touches another.
-- ============================================================
CREATE TABLE IF NOT EXISTS character_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,   -- Cloudinary secure_url
    public_id     TEXT NOT NULL,   -- Cloudinary public_id, for delete
    is_primary    INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_character_images_character ON character_images(character_id);

-- ============================================================
-- CHARACTER_TAGS — free-text tags per character (e.g. which
-- story/arc a character belongs to). Used to build the gallery's
-- tag-filter chip row (distinct tags across all characters).
-- ============================================================
CREATE TABLE IF NOT EXISTS character_tags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    tag           TEXT NOT NULL,
    UNIQUE(character_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_character_tags_character ON character_tags(character_id);
CREATE INDEX IF NOT EXISTS idx_character_tags_tag ON character_tags(tag);

-- ============================================================
-- RELATIONSHIPS — one undirected record per pair-and-tie, shared by both
-- characters (a deliberate departure from the original per-character
-- directional design — see HANDOFF.md). char_a_id is always the smaller
-- id (CHECK enforces this); role_a_to_b is "the role B holds toward A"
-- ("B is A's ___"), role_b_to_a is "the role A holds toward B". Roles are
-- catalog keys (see RELATIONSHIP_ROLES in app.py) or "custom:<slug>" for
-- author-defined ties, with the human-readable text in custom_label_*.
-- category drives map edge color. UNIQUE(char_a_id, char_b_id,
-- role_a_to_b) means the same pair can hold several distinct ties (e.g.
-- Sibling AND Rival) but not the same role twice — and since custom ties
-- key off a slug of their own label rather than a bare "custom" sentinel,
-- two different custom ties between the same pair don't collide either.
-- Both FKs cascade, so deleting either character in the pair removes it.
-- ============================================================
CREATE TABLE IF NOT EXISTS relationships (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    char_a_id            INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    char_b_id            INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    role_a_to_b          TEXT NOT NULL,
    role_b_to_a          TEXT NOT NULL,
    category             TEXT NOT NULL,
    custom_label_a_to_b  TEXT,
    custom_label_b_to_a  TEXT,
    created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT DEFAULT CURRENT_TIMESTAMP,
    CHECK(char_a_id < char_b_id),
    UNIQUE(char_a_id, char_b_id, role_a_to_b)
);

CREATE INDEX IF NOT EXISTS idx_relationships_char_a ON relationships(char_a_id);
CREATE INDEX IF NOT EXISTS idx_relationships_char_b ON relationships(char_b_id);

-- ============================================================
-- Database-level backstop (on top of the application-level checks in
-- app.py): a relationship's two characters must always share a
-- project_id. CHECK constraints can't do cross-row subqueries, so this
-- needs triggers. This is defense-in-depth, not the primary mechanism —
-- the primary mechanism is the ownership checks in create/update
-- relationship routes — but it means the invariant holds even if a
-- future code path forgets the check, not only when it doesn't.
-- ============================================================
CREATE TRIGGER IF NOT EXISTS trg_relationships_same_project_ins
BEFORE INSERT ON relationships
BEGIN
    SELECT RAISE(ABORT, 'relationship characters must share a project')
    WHERE (SELECT project_id FROM characters WHERE id = NEW.char_a_id)
       != (SELECT project_id FROM characters WHERE id = NEW.char_b_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_relationships_same_project_upd
BEFORE UPDATE ON relationships
BEGIN
    SELECT RAISE(ABORT, 'relationship characters must share a project')
    WHERE (SELECT project_id FROM characters WHERE id = NEW.char_a_id)
       != (SELECT project_id FROM characters WHERE id = NEW.char_b_id);
END;
