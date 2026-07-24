# Dramatis

A self-hosted character-profile catalog for novel writing — a companion to
**Baretext**. Create a character, upload one or more portraits, fill in a
set of fields, invent your own fields, and browse the whole cast as a
gallery of editorial collectible-style cards.

Deliberately built to mirror `zamak-ledger`'s architecture so both apps
share one deploy loop on the same host.

## What it does

- Catalogs characters: name, portrait(s) with a selectable primary, a set
  of built-in fields (role, age, species, occupation, accent colour,
  physical description, personality, backstory, goals, notes), and any
  custom fields you add
- Custom fields become standard on **every** character, existing and
  future, the moment you add them — no per-character setup
- Structured relationships between characters (pick another character +
  a free-text label, e.g. "Rival / owes a debt")
- Free-text tags per character, with a filter row in the gallery
- Three built-in colour themes (Noir / Atlas / Archive), search by name,
  role, species, or tag
- Automatic Google Drive backup of the database (not the images — those
  live in Cloudinary, which is its own durable store) after every change,
  plus a daily safety backup and a manual "Back up now" / "Restore from
  Drive"

## Stack

- **Backend:** FastAPI + Uvicorn, Python 3.10+, SQLite
- **Frontend:** single `static/index.html`, no build step, vanilla JS
- **Images:** Cloudinary (no local image storage, no static image mount)
- **Backup:** Google Drive (OAuth 2.0, `drive.file` scope — files this
  app creates only)
- **Auth:** none — single user, Tailscale is the security boundary

Nothing here is committed to git — no database, no `.env`, no OAuth
token. See `HANDOFF.md` for full architecture, data model, and current
status.

## Running your own instance

1. **Python setup:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Get your own keys** (copy `.env.example` to `.env` and fill in):
   - `CLOUDINARY_URL` — **required** for portrait uploads. Get it from your
     [Cloudinary](https://cloudinary.com/) dashboard ("API Environment
     variable", looks like `cloudinary://<key>:<secret>@<cloud_name>`).
     Without this, the app works fully — portrait upload just shows
     "storage not configured" instead of a drop zone.
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — **optional**, enables
     automatic Drive backup. See below for the one-time setup. Without
     these, backup/restore stay unavailable and the header's status dot
     stays amber; everything else works.
3. **Run it:**
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8421
   ```
   Open `http://localhost:8421`. Built-in fields seed automatically on
   first run.

### Google Drive setup (one-time, optional)

1. In the [Google Cloud Console](https://console.cloud.google.com/),
   create a project (or use an existing one) and enable the **Google
   Drive API**.
2. Under **APIs & Services → Credentials**, create an **OAuth client ID**
   of type **Desktop app**. Copy the client ID and secret into `.env`.
3. **Authorize once, on a machine with a browser** — if your server is
   headless, this step can't run there:
   ```bash
   python drive_backup.py --authorize
   ```
   This opens a browser, walks through Google's consent screen, and
   writes `token.json`. Copy that file to the server's working directory
   (`scp token.json user@your-server:~/Projects/dramatis/token.json`).
   After that, every backup is silent — no browser involved.

Dramatis creates (or finds) a Drive folder named **"Character Profile
App"** and keeps the last ~10 timestamped JSON backups there.

## Deploying

If your host is memory-constrained, keep an eye on free RAM and swap —
a second uvicorn app costs ~90MB RSS and may not fit comfortably
alongside other services. Booting to `multi-user.target` (drops the
full desktop environment) can reclaim ~1GB if needed, before you try to
trim the app itself.

```bash
# local
git add <files> && git commit -m "..." && git push

# on the server
cd ~/Projects/dramatis
git pull
sudo systemctl restart dramatis        # only strictly needed for .py changes
curl -s http://localhost:8421/status   # confirm new code is live
```

`static/index.html` edits are live on next request (served fresh from
disk); any `.py` change needs a restart. Verify the restart actually
landed — `systemctl show dramatis -p ActiveEnterTimestamp` — a
requested restart can silently not happen, leaving stale code running.
Do a final check from a device against the `tailscale serve` HTTPS URL,
not just `localhost`.

Set up the systemd unit and `tailscale serve` for this app's own port
and service name (`dramatis`).
