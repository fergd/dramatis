"""
import_extract.py

Sends dossier text to Claude for structured extraction into project
metadata, characters, fields, and relationships — the "propose" half of
Dramatis's import propose/confirm pipeline (app.py's /import/commit is
the "confirm" half, and does the authoritative name-matching/merging;
this module never writes to the database).

Requires ANTHROPIC_API_KEY set in the environment on whatever machine
actually runs this (backupbox), not committed to source control. Mirrors
zamak-ledger's vision_extract.py error-handling/JSON-parsing pattern.
"""

import json
import os

import anthropic

MODEL = "claude-haiku-4-5-20251001"  # extraction is a structured-JSON text task, not
                                       # the kind of close visual read zamak's photo
                                       # extraction needs — Haiku keeps this at a few
                                       # tenths of a cent per dossier (spec's own
                                       # cost guidance), matching zamak's cost model.

MAX_TOKENS = 8192  # generous headroom for a full 6-character dossier's worth of
                    # textarea-length field values — see _check_length below for
                    # the explicit "too long" failure mode instead of silent truncation.


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def extract_text(filename: str, raw_bytes: bytes) -> str:
    """Best-effort plain-text extraction for the formats the import flow
    accepts. Raises RuntimeError with a clean, user-facing message on a
    format-specific failure (corrupt file, unsupported encoding) — same
    translation-to-HTTPException(400) pattern the route layer uses for
    extract_dossier's RuntimeErrors."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in ("md", "txt"):
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RuntimeError(f"Couldn't read {filename!r} as UTF-8 text: {e}") from e

    if ext == "docx":
        try:
            import io

            import docx
        except ImportError as e:
            raise RuntimeError("python-docx isn't installed on the server.") from e
        try:
            doc = docx.Document(io.BytesIO(raw_bytes))
        except Exception as e:
            raise RuntimeError(f"Couldn't read {filename!r} as a .docx file: {e}") from e
        lines = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            # Light heading-style-to-markdown reconstruction, so Claude sees the
            # document's structure rather than a flat wall of paragraphs.
            style = (para.style.name or "") if para.style else ""
            if style.startswith("Heading 1") or style == "Title":
                lines.append(f"# {text}")
            elif style.startswith("Heading 2"):
                lines.append(f"## {text}")
            elif style.startswith("Heading"):
                lines.append(f"### {text}")
            else:
                lines.append(text)
        return "\n\n".join(lines)

    if ext == "pdf":
        try:
            import io

            import pypdf
        except ImportError as e:
            raise RuntimeError("pypdf isn't installed on the server.") from e
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as e:
            raise RuntimeError(f"Couldn't read {filename!r} as a PDF: {e}") from e
        return "\n\n".join(p.strip() for p in pages if p.strip())

    raise RuntimeError(f"Unsupported file type {ext!r} — use .md, .txt, .docx, or .pdf.")


def _catalog_prompt_block(role_catalog: dict) -> str:
    by_category: dict = {}
    for role in role_catalog.values():
        by_category.setdefault(role["category"], []).append(role["label"])
    lines = []
    for category, labels in by_category.items():
        lines.append(f"- {category}: {', '.join(sorted(set(labels)))}")
    return "\n".join(lines)


def _build_prompt(text: str, existing_fields: list, existing_character_names: list, role_catalog: dict) -> str:
    fields_line = ", ".join(existing_fields) if existing_fields else "(none yet)"
    names_line = ", ".join(existing_character_names) if existing_character_names else "(none yet)"
    catalog_block = _catalog_prompt_block(role_catalog)

    return f"""You are extracting structured data from a novel-writing dossier for \
Dramatis, a character-profile catalog. Read the document below and return ONLY a \
JSON object — no preamble, no markdown fences, just the raw JSON — in exactly this \
shape:

{{
  "project_meta": {{
    "setting": "...", "form": "...", "viewpoint": "...", "logline": "...", "notes": "..."
  }},
  "new_fields": [
    {{ "label": "Core belief", "type": "textarea" }}
  ],
  "characters": [
    {{
      "name": "Rachel Mercer",
      "match": "new",
      "fields": {{ "Role/Archetype": "Protagonist; worship leader", "...": "..." }}
    }}
  ],
  "relationships": [
    {{ "from": "Rachel Mercer", "to": "Dean Mercer", "role": "Child", "category": "Family" }},
    {{ "from": "Mikka Humboldt", "to": "Zyanya Mariposa", "role": "custom",
       "custom_label_from_to": "Buyer", "custom_label_to_from": "Supplier",
       "category": "Professional", "note": "the Ascent supply arrangement" }}
  ]
}}

Extraction rules:
- Map recurring per-character headings to fields. This project already has these \
fields: {fields_line}. Reuse an existing label when one fits (case-insensitive) — \
only add a new entry to "new_fields" when nothing matches. Field "type" must be one \
of: text, textarea, number, select, date, color — use "textarea" for anything more \
than a short phrase.
- One-off or project-level sections (a cast list, a pressure-map preamble, drafting \
guardrails) go into "project_meta.notes" or a per-character field as appropriate — \
never drop them silently.
- Extract relationships from BOTH an explicit cast list AND inline prose (e.g. "Dean \
and Joni's daughter; Conner's fiancée"). Map each to one of this project's \
relationship roles where it fits:
{catalog_block}
  Use "role": "custom" with "custom_label_from_to" (the role \"to\" holds toward \
\"from\" — i.e. \"to\" is from's ___) and "custom_label_to_from" (the reverse) when \
nothing in the catalog fits, carrying the dossier's own phrasing rather than forcing \
a wrong standard role. Always include "category" (one of: {', '.join(sorted({r['category'] for r in role_catalog.values()}))}, or "Other").
- This project's existing characters are: {names_line}. Set "match" to "existing" \
for any character in the dossier who is already in that list (matching by name, \
case-insensitive), else "new". A character mentioned only in passing (e.g. named in \
another character's section but never given their own) should still appear in \
"characters" with just a name and whatever fields are known, so relationships \
resolve — don't drop them.
- Never invent facts. If a field isn't present for a character, omit that key \
entirely rather than guessing or writing a placeholder.

Document:
---
{text}
---"""


def extract_dossier(text: str, existing_fields: list, existing_character_names: list, role_catalog: dict) -> dict:
    """existing_fields: list of field labels already in the active project (for
    reuse-not-duplicate prompting). existing_character_names: list of character
    names already in the active project (for match hinting only — the backend
    route re-resolves "match" authoritatively by real name lookup, see app.py's
    /import/extract). role_catalog: RELATIONSHIP_ROLES dict.

    Raises RuntimeError with a clean, user-facing message on API failure or on
    a response too long to fit MAX_TOKENS (chunking is out of scope for this
    pass — see the Import spec's B2 scope note). Retries once on a JSON parse
    failure before raising, per the spec's B6 robustness requirement."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = _build_prompt(text, existing_fields, existing_character_names, role_catalog)

    last_raw = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            detail = e.message
            try:
                detail = e.body.get("error", {}).get("message", e.message)
            except (AttributeError, TypeError):
                pass
            raise RuntimeError(f"Claude API error ({e.status_code}): {detail}") from e
        except anthropic.APIConnectionError as e:
            raise RuntimeError("Couldn't reach the Claude API — check backupbox's internet connection.") from e

        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "This document is too long to extract in one pass. Try splitting it into "
                "smaller sections and importing each separately."
            )

        raw_text = "".join(block.text for block in response.content if block.type == "text").strip()
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        last_raw = raw_text
        try:
            result = json.loads(raw_text)
            result.setdefault("project_meta", {})
            result.setdefault("new_fields", [])
            result.setdefault("characters", [])
            result.setdefault("relationships", [])
            return result
        except json.JSONDecodeError:
            continue  # one retry, same prompt

    raise RuntimeError(
        f"Claude's response wasn't valid JSON after a retry. Raw response started with: {last_raw[:200]!r}"
    )
