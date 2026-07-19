"""
drive_backup.py

Google Drive backup/restore — the one deliberate addition over zamak-ledger
(see HANDOFF.md). Portraits already live in Cloudinary, so this only ever
needs to protect the SQLite data (fields, characters, character_values):
every backup is a small timestamped JSON export, not image bytes.

Scope is deliberately minimal: drive.file (files this app creates only),
not full Drive access.

--- One-time setup (do this once, not on backupbox) ---
backupbox is headless — there's no browser to complete an OAuth consent
screen there. Run the interactive authorization on a machine that HAS a
browser (your laptop), then copy the resulting token.json to backupbox:

    python drive_backup.py --authorize      # opens a browser locally
    scp token.json christan@backupbox:~/Projects/dramatis/token.json

After that, every backup from the running app is silent — no browser
involved, just a refresh-token exchange.
"""

import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger("dramatis.drive_backup")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")
FOLDER_NAME = "Character Profile App"
KEEP_LAST = 10


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _client_config() -> dict:
    return {
        "installed": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _load_credentials():
    """Returns valid Credentials, refreshing from the stored refresh_token
    if the access token has expired. Returns None if unconfigured, never
    authorized, or the refresh itself fails (e.g. revoked access)."""
    if not is_configured() or not os.path.exists(TOKEN_PATH):
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
            return creds
        except Exception as e:
            logger.warning(f"Drive token refresh failed: {e}")
            return None
    return None


def is_authorized() -> bool:
    return _load_credentials() is not None


def is_ready() -> bool:
    return is_configured() and is_authorized()


def authorize_interactive():
    """Run locally (not on backupbox) to produce token.json. See module
    docstring."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not is_configured():
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the environment first.")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"Saved {TOKEN_PATH} — copy this file to backupbox's dramatis directory.")


def _get_service():
    from googleapiclient.discovery import build

    creds = _load_credentials()
    if not creds:
        raise RuntimeError("Google Drive is not authorized — run drive_backup.py --authorize")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_or_create_folder(service) -> str:
    query = (
        f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    result = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return folder["id"]


def push_backup(export: dict) -> str:
    """Uploads a timestamped JSON export, prunes older backups beyond
    KEEP_LAST. Returns the new file's id. Synchronous/blocking — callers on
    the asyncio event loop should run this via asyncio.to_thread."""
    from googleapiclient.http import MediaIoBaseUpload

    service = _get_service()
    folder_id = _find_or_create_folder(service)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"dramatis-backup-{timestamp}.json"
    payload = json.dumps(export, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json")

    file = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id",
    ).execute()

    _prune_old_backups(service, folder_id)
    return file["id"]


def _prune_old_backups(service, folder_id: str):
    query = f"'{folder_id}' in parents and trashed = false"
    result = service.files().list(
        q=query, spaces="drive", fields="files(id, name, createdTime)",
        orderBy="createdTime desc",
    ).execute()
    files = result.get("files", [])
    for f in files[KEEP_LAST:]:
        try:
            service.files().delete(fileId=f["id"]).execute()
        except Exception as e:
            logger.warning(f"Could not prune old backup {f['name']}: {e}")


def pull_latest_backup() -> dict:
    """Returns the parsed JSON of the most recent backup file, or None if
    the folder is empty/missing."""
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    folder_id = _find_or_create_folder(service)

    query = f"'{folder_id}' in parents and trashed = false"
    result = service.files().list(
        q=query, spaces="drive", fields="files(id, name, createdTime)",
        orderBy="createdTime desc", pageSize=1,
    ).execute()
    files = result.get("files", [])
    if not files:
        return None

    request = service.files().get_media(fileId=files[0]["id"])
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(buf.getvalue().decode("utf-8"))


if __name__ == "__main__":
    if "--authorize" in sys.argv:
        authorize_interactive()
    else:
        print("Usage: python drive_backup.py --authorize")
