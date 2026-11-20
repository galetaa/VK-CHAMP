import hashlib
import io
import json
import subprocess
import tempfile
import warnings
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.common import Problem, field_error

MIB = 1024 * 1024
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def prepare_image(content):
    if len(content) > 10 * MIB:
        raise Problem("PAYLOAD_TOO_LARGE")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=list(IMAGE_TYPES)) as image:
                if image.width > 8192 or image.height > 8192 or image.width * image.height > 24_000_000:
                    raise Problem("INVALID_MEDIA")
                image_type = image.format
                upright = ImageOps.exif_transpose(image)
                clean = upright.convert("RGB" if image_type == "JPEG" else "RGBA")
                clean.info.clear()
                with io.BytesIO() as output:
                    clean.save(output, format=image_type)
                    normalized = output.getvalue()
                if len(normalized) > 10 * MIB:
                    raise Problem("PAYLOAD_TOO_LARGE")
                return normalized, {
                    "content_type": IMAGE_TYPES[image_type],
                    "width": clean.width,
                    "height": clean.height,
                    "duration_ms": None,
                }
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise Problem("INVALID_MEDIA") from None


def prepare_video(content):
    if len(content) > 50 * MIB:
        raise Problem("PAYLOAD_TOO_LARGE")
    if len(content) < 12 or content[4:8] != b"ftyp":
        raise Problem("INVALID_MEDIA")
    try:
        with tempfile.TemporaryDirectory(prefix="casechamp-video-") as folder:
            filename = Path(folder) / "upload.mp4"
            filename.write_bytes(content)
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-max_alloc",
                    "67108864",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height:format=duration",
                    "-of",
                    "json",
                    "-i",
                    str(filename),
                ],
                capture_output=True,
                check=True,
                timeout=8,
            )
            probe = json.loads(completed.stdout)
            width, height = (probe["streams"][0][name] for name in ("width", "height"))
            duration = Decimal(probe["format"]["duration"]) * 1000
            if not (1 <= width <= 1920 and 1 <= height <= 1080 and 1 <= duration <= 60_000):
                raise Problem("INVALID_MEDIA")
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file",
                    "-max_alloc",
                    "67108864",
                    "-threads",
                    "1",
                    "-i",
                    str(filename),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                timeout=8,
            )
            return content, {
                "content_type": "video/mp4",
                "width": width,
                "height": height,
                "duration_ms": int(duration),
            }
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        InvalidOperation,
    ):
        raise Problem("INVALID_MEDIA") from None


def prepare(content, kind):
    if kind not in ("IMAGE", "PRODUCT_IMAGE", "VIDEO"):
        raise Problem("VALIDATION_ERROR", fields=[field_error("kind")])
    data, facts = prepare_video(content) if kind == "VIDEO" else prepare_image(content)
    return data, facts | {"kind": kind, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def asset_view(row):
    return {
        key: row[key]
        for key in ("kind", "content_type", "size_bytes", "sha256", "width", "height", "duration_ms")
    } | {
        "asset_id": str(row["asset_id"]),
        "read_url": f"/api/v1/assets/{row['asset_id']}/content",
    }


def place_file(root, content, facts):
    asset_id = uuid4()
    key = asset_id.hex
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / key
    with target.open("xb") as stream:
        stream.write(content)
    return facts | {"asset_id": asset_id, "storage_key": key}


def save_asset(scope, facts):
    limit = scope.db.first(
        """SELECT count(*) count, COALESCE(sum(size_bytes),0) total FROM vk_demo.assets
           WHERE sandbox_id=%s""",
        scope.sandbox_id,
    )
    if limit["count"] >= 30 or limit["total"] + facts["size_bytes"] > 150 * MIB:
        raise Problem("PAYLOAD_TOO_LARGE", reasons=["SANDBOX_MEDIA_QUOTA"])
    scope.insert("assets", **facts, created_at=scope.now)
    return asset_view(facts)
