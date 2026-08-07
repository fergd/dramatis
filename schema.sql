-- ============================================================
-- FIELDS — master list of profile fields, built-in and custom.
-- Adding a row here is what makes a new field "standard" across
-- every character *belonging to the same owner* (see
-- character_values below) — no ALTER TABLE needed per custom
-- field, that's the whole point of the EAV design. `section`
-- groups fields in the detail view (Identity / Description /
-- Notes / Custom, or any name the owner picks). `owner` separates
-- each person's field set — keys are only unique within an owner,
-- not globally, since both people get identically-keyed built-ins
-- (e.g. "age") seeded independently.
-- ============================================================
CREATE TABLE IF NOT EXISTS fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL,             -- who this field belongs to, e.g. "Fergus" / "Esme"
    key         TEXT NOT NULL,             -- slug, e.g. "physical_description"
    label       TEXT NOT NULL,             -- display name, e.g. "Physical description"
    type        TEXT NOT NULL DEFAULT 'text',  -- text | textarea | number | select | date | color
    options     TEXT,                      -- JSON array of strings, only meaningful for type='select'
    section     TEXT NOT NULL DEFAULT 'Custom',
    is_builtin  INTEGER NOT NULL DEFAULT 0, -- built-ins can't be deleted
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner, key)
);

CREATE INDEX IF NOT EXISTS idx_fields_owner_sort_order ON fields(owner, sort_order);

-- ============================================================
-- CHARACTERS — one row per cast member. Everything else about a
-- character (field values, images, tags, relationships) lives in
-- its own table; this one only holds what's true regardless of
-- the current field set. Name is allowed blank (shown as
-- "Unnamed"/"Untitled") — a brand-new character exists the moment
-- "+ New" is clicked, before it's been named. `owner` is who the
-- character belongs to, e.g. "Fergus" / "Esme".
-- ============================================================
CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_characters_owner ON characters(owner);

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
-- RELATIONSHIPS — structured character-to-character links (e.g.
-- "Rival / owes a debt"), a deliberate departure from BRIEF.md's
-- plain textarea field (see HANDOFF.md). Directional: character_id
-- is the character whose detail view the row appears on,
-- related_id is who it points at. related_id is nullable so a
-- freshly-added row can exist before a target is chosen. Both
-- FKs cascade independently, so deleting either character in the
-- pair cleans this row up.
-- ============================================================
CREATE TABLE IF NOT EXISTS relationships (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    related_id    INTEGER REFERENCES characters(id) ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_relationships_character ON relationships(character_id);
CREATE INDEX IF NOT EXISTS idx_relationships_related ON relationships(related_id);
