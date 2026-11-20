"""Local inference with explicitly prepared, manifest-verified model files."""

import hashlib
import io
import json
import math
import subprocess
from collections import defaultdict
from datetime import date, timedelta

from PIL import Image

from app.common import SLOTS
from app.ml import (
    CLIP_SHA,
    EXTRACTION_PROMPT,
    INTENT_PROMPT,
    QWEN_SHA,
    ProviderFailure,
    normalize_budget,
    validated_result,
)


def verify_model_files(root):
    manifest_path = root / "model-manifest.json"
    if not manifest_path.is_file():
        raise ProviderFailure("MODEL_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text())
        qwen = manifest["qwen_revision"]
        clip = manifest["clip_revision"]
        if (qwen, clip) != (QWEN_SHA, CLIP_SHA):
            raise ValueError
        files = manifest["files"]
        if not files or not any(item.startswith("qwen/") and item.endswith(".safetensors") for item in files):
            raise ValueError
        for relative, expected in files.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError
        if "clip/model.safetensors" not in files:
            raise ValueError
        actual = {str(path.relative_to(root)) for path in (root / "qwen").rglob("*") if path.is_file()}
        if any(path not in files for path in actual):
            raise ValueError
        return qwen
    except (OSError, TypeError, ValueError, KeyError):
        raise ProviderFailure("MODEL_MANIFEST_MISMATCH") from None


class Qwen:
    def __init__(self, settings):
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        self.torch = torch
        self.device = settings.ml_device
        model_path = settings.model_root / "qwen"
        self.processor = AutoProcessor.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
            max_pixels=602112,
        )
        self.model = (
            Qwen2_5_VLForConditionalGeneration.from_pretrained(
                model_path,
                local_files_only=True,
                trust_remote_code=False,
                dtype=torch.bfloat16,
                use_safetensors=True,
            )
            .to(self.device)
            .eval()
        )

    def generate(self, instruction, text="", image=None):
        content = [{"type": "text", "text": text}]
        if image is not None:
            content.insert(0, {"type": "image", "image": image})
        messages = [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": content},
        ]
        prompt = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(
            text=[prompt], images=[image] if image is not None else None, return_tensors="pt"
        ).to(self.device)
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=700, do_sample=False)
        return self.processor.batch_decode(
            output[:, inputs["input_ids"].shape[-1] :], skip_special_tokens=True
        )[0]


def json_output(raw):
    raw = raw.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw.removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderFailure("MODEL_OUTPUT_INVALID", ["JSON"]) from None


def intent_payload(raw, business_date):
    if not isinstance(raw, dict) or set(raw) != {"intent_draft", "evidence", "unresolved_fields"}:
        raise ProviderFailure("MODEL_OUTPUT_INVALID")
    draft = raw["intent_draft"]
    if not isinstance(draft, dict):
        raise ProviderFailure("MODEL_OUTPUT_INVALID")
    if "budget_rub" in draft:
        draft["budget_minor"] = normalize_budget(draft.pop("budget_rub"))
        for item in raw.get("evidence", []):
            if isinstance(item, dict) and item.get("field") == "budget_rub":
                item["field"] = "budget_minor"
    if draft.get("needed_by") in ("сегодня", "завтра"):
        draft["needed_by"] = (
            date.fromisoformat(business_date) + timedelta(days=int(draft["needed_by"] == "завтра"))
        ).isoformat()
    return raw


def frame_offsets(duration):
    return sorted({((2 * i + 1) * duration) // 12 for i in range(6)})


def frames(root, asset):
    path = root / asset["storage_key"]
    if asset["kind"] != "VIDEO":
        with Image.open(path) as image:
            yield None, image.convert("RGB")
        return
    for offset in frame_offsets(asset["duration_ms"]):
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file",
                "-max_alloc",
                "67108864",
                "-threads",
                "1",
                "-ss",
                f"{offset / 1000:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-threads",
                "1",
                "-",
            ],
            capture_output=True,
            timeout=8,
            check=True,
        )
        with Image.open(io.BytesIO(completed.stdout)) as image:
            yield offset, image.convert("RGB")


def objects(raw):
    if not isinstance(raw, list) or len(raw) > 120:
        raise ProviderFailure("MODEL_OUTPUT_INVALID", ["objects"])
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"slot_id", "bbox_1000", "evidence_text"}:
            raise ProviderFailure("MODEL_OUTPUT_INVALID", ["objects"])
        bbox = item["bbox_1000"]
        if (
            item["slot_id"] not in SLOTS
            or not isinstance(item["evidence_text"], str)
            or len(item["evidence_text"]) > 1000
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(x) is not int or not 0 <= x <= 1000 for x in bbox)
            or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])
        ):
            raise ProviderFailure("MODEL_OUTPUT_INVALID", ["objects"])
    return raw


def crop(image, bounds):
    x1, y1, x2, y2 = bounds
    return image.crop(
        (
            x1 * image.width // 1000,
            y1 * image.height // 1000,
            math.ceil(x2 * image.width / 1000),
            math.ceil(y2 * image.height / 1000),
        )
    )


def rank(settings, snapshot, detections):
    import open_clip
    import torch

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32",
        pretrained=str(settings.model_root / "clip/model.safetensors"),
    )
    model = model.to(settings.ml_device).eval()

    def embedding(image):
        with torch.inference_mode():
            encoded = model.encode_image(preprocess(image).unsqueeze(0).to(settings.ml_device))
            return torch.nn.functional.normalize(encoded.float(), dim=-1)[0]

    suggestions = []
    for slot_id in SLOTS:
        if slot_id not in detections:
            continue
        options = sorted(detections[slot_id], key=lambda item: (item[0] or 0, item[1]))[:3]
        queries = torch.stack([embedding(crop(image, bbox)) for _, bbox, _, image in options])
        similar = []
        for reference in snapshot["references"]:
            if reference["slot_id"] == slot_id:
                with Image.open(settings.media_root / reference["storage_key"]) as image:
                    values = (queries @ embedding(image.convert("RGB"))).tolist()
                index = max(range(len(values)), key=values.__getitem__)
                score = max(-1.0, min(1.0, values[index]))
                similar.append((score, reference["sku_id"], options[index]))
        similar.sort(key=lambda result: (-result[0], result[1]))
        suggestions.append(
            {
                "slot_id": slot_id,
                "evidence_text": options[0][2],
                "needs_review": True,
                "candidates": [
                    {
                        "sku_id": sku,
                        "rank": index,
                        "similarity_score": score,
                        "source_asset_id": snapshot["asset"]["asset_id"],
                        "frame_ms": source[0],
                        "bbox_1000": source[1],
                    }
                    for index, (score, sku, source) in enumerate(similar[:5], 1)
                ],
            }
        )
    return {"mapping_suggestions": suggestions}


def local_result(settings, contract, snapshot):
    actual_revision = verify_model_files(settings.model_root)
    qwen = Qwen(settings)
    inputs = snapshot["candidate_input"]
    detections = defaultdict(list)
    if inputs["kind"] == "ANALYZE_SCENARIO":
        repaired = False
        raw_frames = []
        for offset, image in frames(settings.media_root, snapshot["asset"]):
            try:
                response = qwen.generate(EXTRACTION_PROMPT, image=image)
                extracted = objects(json_output(response))
            except ProviderFailure as issue:
                if repaired:
                    raise
                repaired = True
                response = qwen.generate(
                    EXTRACTION_PROMPT + "\nИсправьте формат: " + ", ".join(issue.fields),
                    image=image,
                )
                extracted = objects(json_output(response))
            raw_frames.append(response)
            for item in extracted:
                detections[item["slot_id"]].append((offset, item["bbox_1000"], item["evidence_text"], image))
        result = rank(settings, snapshot, detections) if detections else {"mapping_suggestions": []}
        output = validated_result(contract, snapshot, result, "LOCAL", actual_revision=actual_revision)
        output["raw_output_sha256"] = hashlib.sha256(
            json.dumps(raw_frames, ensure_ascii=False).encode()
        ).hexdigest()
        return output

    text = f"Бизнес-дата {inputs['business_date']}.\nТекст пользователя:\n{inputs['text']}"
    prompt = INTENT_PROMPT
    for attempt in range(2):
        try:
            response = qwen.generate(prompt, text)
            draft = intent_payload(json_output(response), inputs["business_date"])
            output = validated_result(contract, snapshot, draft, "LOCAL", actual_revision=actual_revision)
            output["raw_output_sha256"] = hashlib.sha256(response.encode()).hexdigest()
            return output
        except ProviderFailure as issue:
            if attempt:
                raise
            prompt = INTENT_PROMPT + "\nИсправьте JSON и поля: " + ", ".join(issue.fields)
    raise ProviderFailure("MODEL_OUTPUT_INVALID")
