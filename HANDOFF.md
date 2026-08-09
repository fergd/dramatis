# Dramatis — Project Handoff

**Point in time:** 2026-08-08. Supersedes the original 2026-07-19 handoff below
this line's history — that version described the very first build, before
auth, themes, relationships, the map, multi-project support, and document
import existed. Everything in this file reflects the app as actually deployed
and verified today.

**Repo:** `git@github.com:fergd/dramatis.git`, public, `main` branch, latest
commit `7b74c70`.

**Deployed and live** on `backupbox` at
`https://backupbox.tailfb9f14.ts.net:8421/` — own systemd service
(`dramatis.service`), own dedicated `tailscale serve --https=8421` endpoint.
Working dir `/home/christan/Projects/dramatis`. Two people use it (Fergus,
Esme) via a password-less "who's logging in?" picker — Tailscale is the real
access boundary, the login only separates each person's data.

## What this is now

A self-hosted, multi-project character-profile catalog for novel writing.
Each person has their own **projects** (one per book/novella); each project
has its own character field set, metadata, and relationship graph. Within a
project: create characters, upload multiple portraits (one primary), fill in
built-in + custom fields, tag them, and link them together with structured,
reciprocal relationships (a fixed role catalog — Parent/Child, Rival, Fiancé(e),
etc. — plus custom roles) visualized as a force-directed map. A whole project
can also be bootstrapped by uploading a prose dossier — Claude reads it and
proposes characters/fields/relationships for review before anything is saved.

## Architecture

```
Browser -> static/index.html (buildless, vanilla JS, 5 themes via CSS custom properties)
              |
            app.py (FastAPI)
              |
    +---------+----------+------------------+------------------+
    |         |          |                  |                  |
cloudinary_  drive_    import_extract.py  schema.sql        profiles.db
images.py   backup.py  (Claude/Anthropic  (canonical        (projects, project_meta,
(portraits)  (Drive     dossier            schema)           fields, characters,
             OAuth +    extraction)                          character_values,
             backup/                                         character_images,
             restore)                                        character_tags,
                                                               relationships)
```

## Data model (current)

See `schema.sql` for full column comments — this is the authoritative source,
kept meticulously commented on purpose. Summary of the current shape:

- **`projects`** — `id`, `owner`, `title`, timestamps, `last_opened_at` (drives
  the "resume where I left off" fallback when no active-project cookie is
  set). One project per book; not shared between people.
- **`project_meta`** — flexible key/value per project (setting, form,
  viewpoint, logline, notes seeded as builtins; authors can add custom keys
  the same way they add character fields). `is_builtin` flag is informational
  only, like `fields.is_builtin` — doesn't block deletion.
- **`fields`** — now **project-scoped** (`project_id`, not `owner` — that
  column was dropped in the Projects migration). `key` unique per project,
  not globally. type: text/textarea/number/select/date/color.
- **`characters`** — `owner` (kept, cheap and avoids a join for owner-scoped
  queries) **and** `project_id` (which book). A character belongs to exactly
  one project.
- **`character_values`** — EAV table, `(character_id, field_id)`, unchanged
  in shape since the original build.
- **`character_images`**, **`character_tags`** — unchanged since the
  original build (multi-portrait-with-primary; free-text tags).
- **`relationships`** — one **undirected** record per pair-and-tie (reworked
  from the original directional/free-text model). `char_a_id < char_b_id`
  (CHECK-enforced canonical ordering), `role_a_to_b`/`role_b_to_a` are
  catalog keys (`RELATIONSHIP_ROLES` in `app.py`, ~50 entries with real
  inverses and categories) or `custom:<slug>` for author-defined ties.
  `UNIQUE(char_a_id, char_b_id, role_a_to_b)` allows multiple distinct ties
  per pair (e.g. Sibling *and* Rival) but not the same role twice. Two DB
  triggers (`trg_relationships_same_project_ins`/`_upd`) reject any
  relationship whose two characters don't share a `project_id` — a
  defense-in-depth backstop on top of the application-level checks, since a
  `CHECK` constraint can't do that cross-row comparison itself.

**Role catalog is global**, not per-project — the vocabulary (Parent/Child,
Rival, ...) is universal; only *fields* and *project_meta* are per-project.

## API surface

Full CRUD for `projects` (+ `/activate`, `/meta` sub-resource), `characters`,
`fields`; sub-resources for `images` and `relationships`; `/relationship_roles`
for the catalog; `/import/extract` + `/import/commit` for document import;
`/export`, `/backup`, `/restore`, `/status` for backup. Every
project-scoped route takes `project_id: int = Depends(get_current_project)`,
which resolves the active project from the `dramatis_project` cookie with a
three-case fallback (resume most-recently-opened if no cookie and projects
exist; auto-provision a first project if none exist; hard 404 if the cookie
doesn't resolve for the current owner — see `app.py`'s `get_current_project`
docstring). See `app.py` route definitions for the full surface.

## File inventory

| File | Purpose |
|---|---|
| `app.py` | FastAPI app — all routes, DB connection + auto-migration chain, debounced/daily Drive backup scheduling |
| `schema.sql` | Canonical schema: `projects`, `project_meta`, `fields`, `characters`, `character_values`, `character_images`, `character_tags`, `relationships` (+ triggers) |
| `cloudinary_images.py` | Upload/destroy/derived-URL helpers for portraits |
| `drive_backup.py` | Google Drive OAuth + backup/restore/prune, plus a CLI (`--authorize`) for the one-time local consent flow |
| `import_extract.py` | Claude/Anthropic dossier extraction — text extraction (md/txt/docx/pdf) + the structured-JSON extraction prompt/call |
| `static/index.html` | Full frontend — project switcher, gallery, detail (combined view+edit, autosave), relationship map, import flow, Add Field/Manage Fields/Project Settings/Backup modals, 5 themes |
| `requirements.txt` | Python deps |
| `.env.example` | Documents the required/optional env vars, including `ANTHROPIC_API_KEY` |
| `README.md` | Setup, run, credential steps, deploy loop |
| `HANDOFF.md` | This file |

## Auth model

Not a security boundary — Tailscale is (see README). `dramatis_owner` cookie
picks which person's data a request sees (`get_current_owner` dependency);
`dramatis_project` cookie picks which of that person's projects
(`get_current_project`). `ALLOWED_OWNERS`/`DRAMATIS_USERS` env var lists who
can log in.

## Frontend architecture notes

Single `static/index.html`, no build step, five CSS themes (Ember default,
Parchment, Amstrad, Grove, Dracula — matched to the sibling Baretext project's
palette) applied as CSS custom properties on `documentElement`.

**Autosave, no Save button** everywhere it originally applied still applies.
Project title/meta edits in the settings modal also autosave on blur/change,
matching the same pattern.

**View routing** is a flat `STATE.view` switch (`grid`/`detail`/`map`/
`import`), each with its own `go*()` loader + `render*()` function pair — see
`goGrid`/`goDetail`/`goMap`/`goImport` in `static/index.html`. The import flow
has its own two-stage sub-state (`STATE.importState.stage`: `upload` →
`loading` → `review`) since it doesn't fit the single-fetch-then-render shape
the other views use.

**Relationship map**: force-directed simulation (custom, no library),
SVG-rendered, with a fractal-noise grain filter for texture. Edge color is
fixed per category (not theme-derived) so "red = Antagonistic" stays true
across all 5 themes. A pair holding more than one tie renders as offset
parallel lines, not overlapping.

## Verified so far (this point in time)

**Projects migration**: verified against a real downloaded copy of the
production database — exact pre/post character/field counts for both owners,
zero orphaned `character_values`, double-running the migration confirmed
idempotency (no reversion regression), the cross-project relationship trigger
confirmed to reject a forged insert, a forged/guessed `dramatis_project`
cookie confirmed to 404 rather than leak another owner's project, and the
whole-household `build_export()`/Drive-backup path (previously a real crash
risk — see "Known gaps" below) confirmed working post-migration.

**Document import**: verified end-to-end against a real dossier through the
live Anthropic API (not mocked) — extraction matched a canonical worked
example almost exactly (6 characters, correct field reuse against the
project's existing fields, 7 relationships with correct auto-inverse roles
and categories), commit created everything correctly, re-import correctly
updated rather than duplicated catalog-role relationships (matched by role
key). `ANTHROPIC_API_KEY`-unset path confirmed to degrade cleanly: `/status`
reports `import_configured: false`, extraction 400s with a clear message, rest
of the app entirely unaffected.

**One real bug found post-deploy, fixed same day**: the import view's file
dropzone (`<label class="import-dropzone">`) never got a `display` mode set —
`<label>` defaults to `display: inline`, so the dashed box collapsed to a
sliver and its text spilled out beside it instead of centering inside a
proper full-width box. Fixed (`display: block`), frontend-only so no restart
needed. Caught via the owner's own screenshot after first live use, not
caught in this session's own (backend-focused) testing — worth remembering
that CSS layout bugs like this don't show up in curl/API-level verification.

## Known gaps / accepted limitations — needs the owner's follow-up (or doesn't)

1. **Google Drive backup — deliberately deferred**, not an oversight (owner's
   call, 2026-07-19, reconfirmed since): portraits already live durably in
   Cloudinary; the remaining text data isn't worth the OAuth setup right now.
   Code path is fully built and safe when unconfigured. The Projects
   migration changed `/export`'s JSON shape (now `{"projects": [...]}`
   wrapping each project's fields/characters, instead of flat top-level
   `fields`/`characters` arrays) — relevant only if ever restoring an export
   downloaded before 2026-08-08, which would need reshaping first.
2. **Custom (non-catalog) relationship re-import can near-duplicate.**
   Catalog-role relationships dedupe perfectly on re-import (identity is the
   stable role key). A *custom* role's identity is its own label text, which
   an LLM can phrase slightly differently between two independent extraction
   calls on the same document (e.g. "Recipient" vs. "Recipient of
   transmission") — each phrasing hashes to a different dedupe key by design,
   since the schema deliberately supports multiple distinct custom ties per
   pair. Confirmed via live re-import testing; the review screen surfaces the
   near-duplicate visibly before commit either way, so it's a minor
   annoyance rather than silent data corruption. Not planned to be "fixed"
   unless it becomes a real nuisance in practice — there's no obvious fix
   that doesn't sacrifice the multiple-custom-ties-per-pair feature.
3. **True mobile-width layout** only ever spot-checked at ~606px (this
   environment's narrowest reliable `resize_window`), never on an actual
   phone or below ~600px. Standard CSS techniques (`auto-fit`/`auto-fill`
   grids) used throughout, so risk is low, but unconfirmed.
4. **Secret-handling friction, worth remembering for next time**: Claude
   Code's own safety classifier blocked an attempt to programmatically copy
   the `ANTHROPIC_API_KEY` value between files (fetching it from
   zamak-ledger's systemd unit into a local `.env` for testing) — the owner
   added it to `.env` by hand both locally and on backupbox instead. Expect
   the same friction for any future secret that needs moving between
   files/hosts; hand it to the owner as an instruction rather than trying to
   pipe the value through tool calls.

## Outstanding / possible next steps

- [ ] Google Drive backup — deliberately deferred, not currently planned.
      Revisit if the text data ever feels worth automated backup; the code
      is ready, just needs `.env` + `--authorize`.
- [ ] Real-phone check of the mobile layout below ~600px.
- [ ] Nothing else from the Projects & Document Import spec is outstanding —
      both Part A (Projects) and Part B (Import) are shipped and verified.
- [x] ~~Multi-project support~~ — shipped 2026-08-08.
- [x] ~~Claude-assisted document import~~ — shipped 2026-08-08.
- [x] ~~Reciprocal, catalog-based relationships + map~~ — shipped earlier
      in the 2026-08-08 working session, before Projects/Import.
- [x] ~~Per-person login~~, ~~5-theme palette~~, ~~built-in field
      delete/type-edit~~ — all shipped prior to this point in time.

---

<details>
<summary>Original 2026-07-19 handoff (historical — kept for the deploy/
Cloudinary/Chrome-extension troubleshooting notes, which are still accurate)</summary>

**Point in time:** 2026-07-19. Built from `BRIEF.md` plus a high-fidelity
design handoff (`Dramatis.dc.html` + its own `README.md`, bundled as
`Character Profile Builder.zip`), visually verified end-to-end in a real
Chrome session, live-tested against a real Cloudinary account, and deployed
and running on backupbox.

**Memory blocker cleared first** — backupbox was still on `graphical.target`
with 185MB free RAM and swap 100% full; switched to `multi-user.target` and
rebooted (confirmed all of the owner's actual workloads — zamak-ledger, the
`photos-bu.sh`/`videos-bu.sh` cron backups, docker, Plex, tailscaled — run
under `multi-user.target`, not `graphical.target`; only desktop-only units
like `gdm` dropped). Now 1.8GB free, swap empty.

**One deploy bug hit and fixed:** the systemd unit's first version bound
uvicorn to `0.0.0.0:8421` — but `tailscale serve --https=8421` makes
`tailscaled` itself bind port 8421 *on the tailscale interface* to terminate
HTTPS there before proxying to `127.0.0.1:8421`. Binding `0.0.0.0` includes
that same interface, so uvicorn and tailscaled fought over the same port and
the service sat in an `activating (auto-restart)` crash loop. Fixed by
binding uvicorn to `127.0.0.1` only.

`sudo` was required for `systemctl daemon-reload`/`enable`/`restart` and for
the `multi-user.target` reboot — those needed the owner to run the commands
directly (password not available/appropriate to automate). Everything else
— `git clone`, venv setup, `.env`, and `tailscale serve` configuration itself
— needed no elevated privilege. This pattern held for every deploy since.

**Cloudinary**, live-tested against the real `fergd` account (reused from
zamak-ledger, separate `dramatis/` folder namespace, no collision): full
lifecycle confirmed — upload, second image correctly not becoming primary,
deleting the primary promotes the next image, character delete cascades to
destroy all Cloudinary assets, zero orphaned resources left behind. **Found
and fixed a real bug:** `derived_url()` was building plain `http://` URLs —
`cloudinary.CloudinaryImage(...).build_url()` doesn't default to secure.
Fixed with `secure=True`.

**Chrome extension connection:** initially wouldn't connect at all
(`tabs_context_mcp` timed out repeatedly). Root cause was Arc — the
extension was also installed there and the two were conflicting; removing it
from Arc and using Chrome exclusively fixed it immediately, no restart
needed. (Still relevant: in the 2026-08-08 session, the extension was not
connected at all and headless Chrome CLI dump-dom also hung repeatedly in
this sandboxed environment — verification that session leaned on curl/
TestClient-level API testing plus static code review instead of a real
browser, which is *why* the dropzone CSS bug above wasn't caught until the
owner's own first live use.)

</details>
