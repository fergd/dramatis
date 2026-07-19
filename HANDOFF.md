# Dramatis — Project Handoff

**Point in time:** 2026-07-19, initial build from `BRIEF.md` plus a
high-fidelity design handoff (`Dramatis.dc.html` + its own `README.md`,
bundled as `Character Profile Builder.zip`). Scaffold through backup are
all built and have now been visually verified end-to-end in a real
Chrome session (see "Verified so far" below) — Cloudinary and Google
Drive live credential testing are the remaining gaps.

**Repo:** `git@github.com:fergd/dramatis.git`, public, `main` branch,
root commit `42e1256` pushed 2026-07-19.
**Deployed and live** on `backupbox` as of 2026-07-19:
`systemctl status dramatis` → active, 35MB RSS. Reachable at
`https://backupbox.tailfb9f14.ts.net:8421/` (own dedicated `tailscale
serve --https=8421` endpoint, distinct from zamak's default-443 root
path — confirmed both `/status` and `/` return 200 through the real
HTTPS URL, not just localhost). Working dir `/home/christan/Projects/
dramatis`, venv + deps installed, `.env` has the real `CLOUDINARY_URL`
(reused from zamak, same as local dev).

**Memory blocker cleared first** — backupbox was still on
`graphical.target` with 185MB free RAM and swap 100% full; switched to
`multi-user.target` and rebooted (confirmed all of the owner's actual
workloads — zamak-ledger, the `photos-bu.sh`/`videos-bu.sh` cron backups,
docker, Plex, tailscaled — run under `multi-user.target`, not
`graphical.target`; only desktop-only units like `gdm` dropped). Now
1.8GB free, swap empty.

**One deploy bug hit and fixed:** the systemd unit's first version bound
uvicorn to `0.0.0.0:8421` — but `tailscale serve --https=8421` makes
`tailscaled` itself bind port 8421 *on the tailscale interface* to
terminate HTTPS there before proxying to `127.0.0.1:8421`. Binding
`0.0.0.0` includes that same interface, so uvicorn and tailscaled fought
over the same port and the service sat in an `activating (auto-restart)`
crash loop (`journalctl` showed `[Errno 98] address already in use`).
Zamak never hits this because it uses `tailscale serve`'s default port
443, not a dedicated `--https=` port. Fixed by binding uvicorn to
`127.0.0.1` only — also the more correct choice regardless, since
`tailscale serve` should be the only public-facing entry point.

`sudo` was required for `systemctl daemon-reload`/`enable`/`restart` and
for the `multi-user.target` reboot — those two steps needed the owner to
run the commands directly (password not available/appropriate to
automate). Everything else — `git clone`, venv setup, `.env`, and
`tailscale serve` configuration itself — needed no elevated privilege.

## What this is

A single-user, self-hosted character-profile catalog for the Baretext
novel-writing project — create a character, upload portraits, fill in
fields (built-in + your own), browse the cast as a gallery of editorial
cards. Companion to `zamak-ledger`, deliberately built to mirror its
architecture (see `BRIEF.md` §1 for the specific decisions and why).

## Architecture

```
Browser -> static/index.html (buildless, vanilla JS, 3 themes via CSS custom properties)
              |
            app.py (FastAPI)
              |
    +---------+----------+------------------+
    |         |          |                  |
cloudinary_  drive_    schema.sql        profiles.db
images.py   backup.py  (canonical       (fields, characters,
(portraits)  (Drive     schema)          character_values,
             OAuth +                     character_images,
             backup/                     character_tags,
             restore)                    relationships)
```

## Design deviations from BRIEF.md (resolved, both adopted)

The bundled design reference (`Dramatis.dc.html`) went beyond `BRIEF.md`
in two places; the owner confirmed both should be built as designed
rather than reverted to the brief's simpler model:

1. **Multiple portraits with a selectable primary**, not a single
   `portrait_url`/`portrait_public_id` pair on `characters`. Implemented
   as a `character_images` table (`character_id, url, public_id,
   is_primary, sort_order`) — `POST/DELETE /characters/{id}/images[/...]`
   handle a set, not one image. Deleting the primary promotes the next
   image by `sort_order`; if a character has no images, the primary
   image is simply absent (placeholder initials shown instead).
2. **Structured character-to-character relationships**, not a plain
   textarea. Implemented as a `relationships` table (`character_id,
   related_id, label`), directional — the row lives on `character_id`'s
   detail view and points at `related_id`. Both FKs cascade
   independently, so deleting either character in the pair cleans up the
   row (confirmed via a real delete during testing — see "Verified" below).

Two more additions came from the design, not in `BRIEF.md` at all:
**tags** (`character_tags` table, free-text, drives the gallery's filter
chip row) and **field sections** (`fields.section`, groups the detail
view into Identity/Description/Notes/Custom-or-anything, drives the
"Group under" picker in the Add Field modal).

## Storage locations

Same discipline as zamak — nothing here is committed to git.

- **`profiles.db`** — SQLite, gitignored. Working dir `~/Projects/dramatis/`
  on backupbox once deployed.
- **Portraits → Cloudinary**, folder `dramatis`. No local image storage,
  no static image mount. Each `character_images` row stores the
  Cloudinary `secure_url` + `public_id`; the API derives width-capped
  URLs on read (`derived_url()` in `cloudinary_images.py`) rather than
  storing multiple pre-sized copies.
- **`.env`** — `CLOUDINARY_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
  Gitignored. See `.env.example`.
- **`token.json`** — Google OAuth refresh token, produced by
  `drive_backup.py --authorize` run **locally** (backupbox is headless —
  see README's Google Drive setup section), then copied to the server.
  Gitignored.
- **`schema.sql`** — canonical schema. `app.py`'s `get_conn()` runs
  `executescript` (no-op on existing tables) then a light column-level
  auto-migration (`_migrate_table`), same pattern as zamak.

## File inventory

| File | Purpose |
|---|---|
| `app.py` | FastAPI app — all routes, DB connection + auto-migration, debounced/daily Drive backup scheduling |
| `schema.sql` | Canonical schema: `fields`, `characters`, `character_values`, `character_images`, `character_tags`, `relationships` |
| `cloudinary_images.py` | Upload/destroy/derived-URL helpers — generic (`upload_image`/`destroy_image`), used for every image a character has |
| `drive_backup.py` | Google Drive OAuth + backup/restore/prune. Also a CLI (`--authorize`) for the one-time local consent flow |
| `static/index.html` | Full frontend — gallery, detail (combined view+edit, autosave), Add Field / Manage Fields / Backup modals, 3 themes |
| `requirements.txt` | Python deps |
| `.env.example` | Documents the required/optional env vars |
| `README.md` | Setup, run, credential steps, deploy loop, the memory caveat |
| `HANDOFF.md` | This file |

## Data model

See `schema.sql` for full column comments. Summary:

- **`fields`** — `key` (unique slug), `label`, `type` (text/textarea/
  number/select/date/color), `options` (JSON, select only), `section`,
  `is_builtin`, `sort_order`. Seeded on first run (`BUILTIN_FIELDS` in
  `app.py` — kept in one place per the brief, matches the design's
  `DEFAULT_SCHEMA` exactly, **not** `BRIEF.md`'s original seed list: no
  "Full Name" field (name is a first-class column, not a field), no
  "Relationships" textarea (structured table instead)).
- **`characters`** — `id`, `name` (nullable-empty — a brand-new character
  exists the instant "+ New" is clicked, before it's named), timestamps.
- **`character_values`** — EAV table, one row per `(character_id,
  field_id)`. A new field applies to every character instantly because
  reads treat a missing row as an empty value — no backfill needed.
- **`character_images`**, **`character_tags`**, **`relationships`** — see
  "Design deviations" above.

## API surface

Full CRUD for `characters`, `fields`; sub-resources for `images` and
`relationships` under `/characters/{id}/...`; `/export`, `/backup`,
`/restore`, `/status` for backup. See `app.py` route definitions — the
brief's original API table (`BRIEF.md` §6) is close but doesn't cover the
image-set and relationship endpoints added for the design deviations.

## Frontend architecture notes

Single `static/index.html`, no build step, three CSS themes (Noir default,
Atlas, Archive) applied as CSS custom properties on `documentElement` —
same mechanism the design prototype uses, same exact hex values.

**Autosave, no Save button** — every field/name/tag/relationship/image
edit persists on `change` (blur), matching the design's interaction
model. This required deliberately *not* doing a full-view re-render after
most successful saves (the natural instinct after a save completes),
since the user has often already moved focus to the next field by the
time the network round-trip resolves — a full innerHTML rebuild would
yank focus out from under them. Instead: simple field values update
local state silently (the DOM already shows what the user typed); tags,
relationships, and images get *scoped* redraws (`redrawTagRow()`,
`redrawRelationships()`, `redrawPortraitBlock()`) that only rebuild their
own container; only field-schema changes (add/rename/reorder/delete a
field, closing those modals) trigger a full section rebuild, which is
fine since that's a deliberate, focus-abandoning action anyway.

**Collection numbers** (`№ 001`) are computed client-side from array
position, not stored — same as the design prototype. `GET /characters`
returns newest-first (`ORDER BY id DESC`), matching the prototype's
"prepend new character to the front" behavior, so a freshly-created
character reliably lands at №001.

## Verified so far

**Backend:** all routes exercised end-to-end via curl against a live
local server: fields seeding, characters CRUD, custom field add/rename/
reorder/delete (including cascade-deleting `character_values`), tags
(full-replace semantics), relationships (create-blank → set target → set
label, confirmed the FK cascade when the *target* character is deleted,
not just the owning one), export shape. Graceful-failure paths confirmed:
portrait upload without `CLOUDINARY_URL` returns a clean 502 without
crashing the process; `/backup` and `/restore` return clean 400s when
Drive isn't configured; `/restore` requires `confirm: true`.

**Frontend, in a real Chrome session (once the browser extension
connection was fixed — see below):** gallery empty state, card creation
and rendering (collection numbers, species tag, scrim, accent bar/dot all
correct), the combined detail+editor view at true desktop width (portrait
column + field sections + relationships), autosave on name/text/number/
color/select fields confirmed via direct API checks after each save (no
Save button, no full-view re-render stealing focus), accent colour
propagating live to the name underline and portrait accent bar without a
page reload, all three themes (Noir/Atlas/Archive) applying correct
tokens, tag add via the gallery's "+ tag" input and the resulting filter
chip row, the Add Field modal (including the Select-type options
textarea) and Manage Fields modal (reorder, rename — both confirmed
against a real running app, not just curl), a full relationship
create → assign-target → assign-label round trip against two real
characters, and the Backup & restore modal's copy/disabled-state logic
when Drive/Cloudinary are unconfigured. Found and fixed one real bug this
way: "Download snapshot" was navigating the tab to raw JSON instead of
downloading a file — now does a proper client-side Blob download, matching
the design.

**Chrome extension connection:** initially wouldn't connect at all
(`tabs_context_mcp` timed out repeatedly). Root cause was Arc — the
extension was also installed there and the two were conflicting; removing
it from Arc and using Chrome exclusively fixed it immediately, no restart
needed.

**Not independently re-verified after the fixes above** (low risk, but
say so): the `updateCharCount()` staleness fix on entering detail view,
and the `STATE.characters` refresh added to `goDetail()` for the
relationship-dropdown/collection-number staleness fix — both were code
review fixes made *during* this session's testing, applied and
syntax-checked but not re-clicked-through afterward.

## Known gaps — needs the owner's follow-up

1. ~~Cloudinary untested live~~ — verified 2026-07-19 against the real
   `fergd` Cloudinary account (reused from zamak-ledger, separate `dramatis/`
   folder namespace, no collision — confirmed zero cross-contamination).
   Full lifecycle confirmed against the live API: upload, second image
   correctly not becoming primary, deleting the primary promotes the next
   image, character delete cascades to destroy all Cloudinary assets (not
   just DB rows — confirmed via a direct fetch of the deleted asset's URL
   returning 404), zero orphaned resources left behind afterward (checked
   via `cloudinary.api.resources`). **Found and fixed a real bug in the
   process:** `derived_url()` in `cloudinary_images.py` was building plain
   `http://` URLs — `cloudinary.CloudinaryImage(...).build_url()` doesn't
   default to secure, and nothing was passing `secure=True`. All API
   responses (card thumbnails, detail images, thumbnail strip) were
   serving over http despite the raw stored column correctly holding
   Cloudinary's own `secure_url` from upload. Fixed by adding
   `secure=True` to the `build_url()` call; re-verified both derived URLs
   now return `https://` and are reachable.
2. **Google Drive is still untested live** — no OAuth client was set up
   this session. Same failure-path verification as before (clean 400s
   when unconfigured) but the real `--authorize` → `token.json` →
   debounced-backup-lands-in-Drive-folder flow has not been run.
3. ~~Not yet a git repository~~ — done, see below.
3. ~~True mobile-width layout wasn't visually confirmed~~ — confirmed in
   a follow-up session at ~606px width (the closest this environment's
   `resize_window` would reliably give): header wraps cleanly with no
   overflow, the detail view's two-column layout collapses to one column,
   and the Identity field grid auto-fits down to fewer columns rather
   than a fixed breakpoint (same `auto-fit, minmax(160px,1fr)` behavior
   the design itself uses). Not tested below ~600px or on a real device —
   worth a quick real-phone glance before considering this fully done,
   but the underlying CSS techniques are standard enough that risk here
   is low.

## Outstanding / possible next steps

- [ ] Live-test Cloudinary upload/replace/delete with a real account —
      the portrait drop zone and thumbnail strip were never exercised
      with a real image this session (no credentials available).
- [ ] Live-test the Google Drive OAuth flow end-to-end (`--authorize`
      locally, copy `token.json`, confirm a debounced backup lands in the
      "Character Profile App" folder).
- [x] ~~`git init`, first commit~~ — done 2026-07-19, root commit
      `42e1256` on `main`, pushed to `github.com/fergd/dramatis` (public).
- [x] ~~Deploy to backupbox~~ — done 2026-07-19, see "Deployed and live"
      above. Confirmed via the real `tailscale serve` HTTPS URL, not just
      localhost, per the deploy-loop guidance in §12/README.
