"""Your logo: shown at the top of the guest sign-in pages (centred) and the
admin pages (top left), in place of the network name. With no logo, the
name is shown as before.

Kept as one file in the data folder. Only PNG, JPEG, WebP and GIF are
accepted, checked by their contents, not their name. SVG isn't, because an
SVG can carry scripts.
"""
import os

from fastapi import Response
from fastapi.responses import FileResponse

from . import db

BRAND_DIR = os.path.join(db.DATA_DIR, "branding")
MAX_BYTES = 2 * 1024 * 1024
TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def sniff(data: bytes) -> str | None:
    """The image type from the file's first bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def path() -> str | None:
    name = db.get("logo_file")
    p = os.path.join(BRAND_DIR, name) if name else None
    return p if p and os.path.isfile(p) else None


def url() -> str:
    """Where pages load the logo from ('' when there's none). The version
    number makes browsers fetch a new logo right after it's replaced."""
    return f"/brand/logo?v={db.get('logo_version') or 0}" if path() else ""


def save(data: bytes) -> str:
    """Store an uploaded logo. Returns '' on success, or what's wrong."""
    if not data:
        return "Choose an image to upload."
    if len(data) > MAX_BYTES:
        return "That image is over 2 MB. Please use a smaller one."
    kind = sniff(data)
    if not kind:
        return "Please upload a PNG, JPEG, WebP or GIF image."
    os.makedirs(BRAND_DIR, exist_ok=True)
    name = "logo." + kind
    tmp = os.path.join(BRAND_DIR, name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(BRAND_DIR, name))
    old = path()
    if old and os.path.basename(old) != name:
        os.remove(old)
    db.set("logo_file", name)
    db.set("logo_version", str(db.now()))
    db.log("logo_changed", f"{kind}, {len(data)} bytes")
    return ""


def remove():
    p = path()
    if p:
        os.remove(p)
    db.set("logo_file", "")
    db.log("logo_changed", "removed")


def response():
    """The /brand/logo route, shared by the portal and the admin app."""
    p = path()
    if not p:
        return Response(status_code=404)
    kind = p.rsplit(".", 1)[-1]
    return FileResponse(p, media_type=TYPES.get(kind, "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})
