"""Author the exact-input REPLAY fixtures for the controlled demo. No model runs here."""

import hashlib
import json
from datetime import date
from pathlib import Path

from app.common import SLOTS, digest
from app.media import prepare_image
from app.ml import CLIP_REVISION, EXTRACTION_PROMPT, INTENT_PROMPT, MODEL_REVISION, PREPROCESS_VERSION
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
ART = FIXTURES / "replay_assets"
TEXT = "За столом будет 6 гостей."
CAPTION = "Демонстрационная белая тарелка."


def image(path, *, scene):
    canvas = Image.new("RGB", (160, 160), (181, 153, 124) if scene else (240, 239, 236))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((39, 40, 124, 125), fill="#f9f7ed", outline="#8d8276", width=2)
    draw.ellipse((51, 52, 112, 113), outline="#d6c9b9", width=2)
    if scene:
        draw.line((28, 51, 28, 111), fill="#afa8b0", width=3)
    canvas.save(path, format="PNG")


def model_input(values, prompt):
    return values | {
        "preprocessing": PREPROCESS_VERSION,
        "taxonomy": list(SLOTS),
        "model_revision": MODEL_REVISION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }


def prepare(day):
    ART.mkdir(exist_ok=True)
    scene, plate = ART / "scene.png", ART / "plate.png"
    image(scene, scene=True)
    image(plate, scene=False)
    scene_data, _ = prepare_image(scene.read_bytes())
    plate_data, _ = prepare_image(plate.read_bytes())
    intent_input = model_input(
        {"kind": "PARSE_INTENT", "text": TEXT, "business_date": day.isoformat()}, INTENT_PROMPT
    )
    content_input = model_input(
        {
            "kind": "ANALYZE_SCENARIO",
            "caption_sha256": hashlib.sha256(CAPTION.encode()).hexdigest(),
            "media_sha256": hashlib.sha256(scene_data).hexdigest(),
            "retrieval_revision": CLIP_REVISION,
            "catalog": [
                {
                    "sku_id": "PLATE_WHITE1",
                    "slot_id": "dinner_plate",
                    "sha256": hashlib.sha256(plate_data).hexdigest(),
                }
            ],
        },
        EXTRACTION_PROMPT,
    )
    hint = "fixture-authored-1.0.0"
    records = {
        digest(intent_input): {
            "origin_revision": hint,
            "data": {
                "intent_draft": {"guest_count": 6},
                "evidence": [{"field": "guest_count", "quote": "6 гостей"}],
                "unresolved_fields": [],
            },
        },
        digest(content_input): {
            "origin_revision": hint,
            "data": {
                "mapping_suggestions": [
                    {
                        "slot_id": "dinner_plate",
                        "evidence_text": "Белая тарелка на изображении.",
                        "needs_review": True,
                        "candidates": [
                            {
                                "sku_id": "PLATE_WHITE1",
                                "rank": 1,
                                "similarity_score": None,
                                "source_asset_id": None,
                                "frame_ms": None,
                                "bbox_1000": [243, 250, 781, 787],
                            }
                        ],
                    }
                ]
            },
        },
    }
    location = FIXTURES / "replay.json"
    if location.exists():
        records = json.loads(location.read_text())["records"] | records
    location.write_text(
        json.dumps(
            {
                "provenance": "Hand-authored control response for exact-input REPLAY, no live model output.",
                "records": records,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    import sys
    from datetime import datetime

    from app.common import MOSCOW

    prepare(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(MOSCOW).date())
