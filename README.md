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
3. **Authorize once, on a machine with a browser** — backupbox is
   headless, so this step can't run there:
   ```bash
   python drive_backup.py --authorize
   ```
   This opens a browser, walks through Google's consent screen, and
   writes `token.json`. Copy that file to the server's working directory
   (`scp token.json christan@backupbox:~/Projects/dramatis/token.json`).
   After that, every backup is silent — no browser involved.

Dramatis creates (or finds) a Drive folder named **"Character Profile
App"** and keeps the last ~10 timestamped JSON backups there.

## Deploying (backupbox)

**backupbox is memory-bound** — roughly 116MB free, swap already full.
A second uvicorn app costs ~90MB RSS and won't fit comfortably alongside
zamak-ledger and whatever else is running. **Boot backupbox to
`multi-user.target`** (drops the full GNOME desktop, reclaiming ~1GB)
*before* adding this service — don't try to trim this app instead, there
isn't much left to trim.

```bash
# local
git add <files> && git commit -m "..." && git push

# on backupbox
cd ~/Projects/dramatis
git pull
sudo systemctl restart dramatis        # only strictly needed for .py changes
curl -s http://localhost:8421/status   # confirm new code is live
```

`static/index.html` edits are live on next request (served fresh from
disk); any `.py` change needs a restart. Verify the restart actually
landed — `systemctl show dramatis -p ActiveEnterTimestamp` — zamak hit
cases where a requested restart silently didn't happen and stale code
kept running. Do a final check from a device against the `tailscale
serve` HTTPS URL, not just `localhost`.

Set up the systemd unit and `tailscale serve` the same way zamak-ledger's
`HANDOFF.md` documents (own port, own service name — `dramatis`, not
`zamak-ledger`).
