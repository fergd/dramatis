"""
cloudinary_images.py

Portrait storage on Cloudinary — no local disk, no static image mount (see
HANDOFF.md for why). Unlike zamak-ledger's cloudinary_upload.py, this does
NOT resize locally before upload: Cloudinary handles resizing/transforms via
URL params on read (derived_url below), so raw bytes go up as-is (capped at
MAX_UPLOAD_BYTES to keep the free tier viable).

Requires CLOUDINARY_URL in the environment (cloudinary://key:secret@cloud —
from the Cloudinary dashboard's "API Environment variable" display). The SDK
auto-configures from that on import, no explicit cloudinary.config() call
needed.
"""

import os
import uuid
from typing import Optional

import cloudinary
import cloudinary.uploader

FOLDER = "dramatis"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class UploadError(ValueError):
    """Raised for bad input (wrong type, too large) — a 400, not a 502."""


def is_configured() -> bool:
    return bool(
        os.environ.get("CLOUDINARY_URL")
        or (os.environ.get("CLOUDINARY_CLOUD_NAME") and os.environ.get("CLOUDINARY_API_KEY"))
    )


def upload_image(raw_bytes: bytes, content_type: str) -> dict:
    """Validates and uploads raw image bytes to Cloudinary. Returns
    {secure_url, public_id}. public_id is generated explicitly (raw byte
    uploads carry no real filename) so uploads never collide. Used for every
    image a character has — a character can hold several, one marked
    primary (see character_images in schema.sql)."""
    if not is_configured():
        raise RuntimeError("Cloudinary is not configured (CLOUDINARY_URL missing)")
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadError(f"Unsupported image type '{content_type}' — use JPEG, PNG, or WebP")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise UploadError(f"Image too large ({len(raw_bytes) // 1024}KB) — max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")

    result = cloudinary.uploader.upload(
        raw_bytes, folder=FOLDER, resource_type="image", public_id=uuid.uuid4().hex
    )
    return {"secure_url": result["secure_url"], "public_id": result["public_id"]}


def destroy_image(public_id: str) -> None:
    cloudinary.uploader.destroy(public_id, resource_type="image")


def derived_url(public_id: Optional[str], width: int = 800) -> Optional[str]:
    """Cloudinary-side resize/format — cards request a narrower width than
    the detail view, but both hit the same original asset. Passes through
    None so callers can call this unconditionally on a possibly-absent
    primary image."""
    if not public_id:
        return None
    return cloudinary.CloudinaryImage(public_id).build_url(
        width=width, crop="limit", quality="auto", fetch_format="auto", secure=True
    )
