"""Download the verified upstream revisions only when running the optional LOCAL provider."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from app.ml import CLIP_SHA, QWEN_SHA
from app.settings import ROOT

QWEN = "Qwen/Qwen2.5-VL-7B-Instruct"
CLIP = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"


def download(root):
    from huggingface_hub import hf_hub_download, snapshot_download

    root.mkdir(parents=True, exist_ok=True)
    qwen = Path(
        snapshot_download(
            QWEN,
            revision=QWEN_SHA,
            allow_patterns=["*.json", "*.txt", "*.safetensors"],
        )
    )
    clip = Path(hf_hub_download(CLIP, "open_clip_model.safetensors", revision=CLIP_SHA))
    targets = [
        (source, root / "qwen" / source.name)
        for source in qwen.iterdir()
        if source.suffix in (".json", ".txt", ".safetensors")
    ]
    targets.append((clip, root / "clip/model.safetensors"))
    hashes = {}
    for source, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy a verified cache object as a plain file, keeping the installation portable.
        shutil.copyfile(source, target)
        with target.open("rb") as file:
            hashes[str(target.relative_to(root))] = hashlib.file_digest(file, "sha256").hexdigest()
    manifest = {
        "qwen_repository": QWEN,
        "qwen_revision": QWEN_SHA,
        "clip_repository": CLIP,
        "clip_revision": CLIP_SHA,
        "files": hashes,
    }
    output = root / "model-manifest.json"
    output.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", type=Path, default=ROOT / "var/models")
    args = parser.parse_args()
    download(args.model_root)
