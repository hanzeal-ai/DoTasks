"""Download exactly one pinned checkpoint, normalize its tokenizer, and hash it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .settings import MODEL_ID, MODEL_REVISION


def download(destination: Path):
    from huggingface_hub import snapshot_download
    from laya.agent import _fix_tokenizer_config

    if destination.exists():
        raise SystemExit(f"Destination already exists; reuse it or choose a new directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    names = ["rl_agent_config.json", "model.safetensors", "encoder/config.json",
             "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"]
    snapshot = Path(snapshot_download(MODEL_ID, revision=MODEL_REVISION, allow_patterns=names))
    with tempfile.TemporaryDirectory(prefix=".laya-model-", dir=destination.parent) as temporary:
        root = Path(temporary) / "model"
        for name in names:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot / name, target)
        # Laya applies this compatibility transform at load time. Hash the normalized copy,
        # never mutate the HF snapshot; subsequent starts verify the same local artifact.
        _fix_tokenizer_config(str(root))
        hashes = {}
        for name in names:
            with (root / name).open("rb") as source:
                hashes[name] = hashlib.file_digest(source, "sha256").hexdigest()
        (root / "manifest.json").write_text(json.dumps({
            "model_id": MODEL_ID, "revision": MODEL_REVISION, "sha256": hashes,
        }, indent=2) + "\n")
        os.rename(root, destination)
    print(f"Pinned model ready: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    download(parser.parse_args().destination.expanduser().resolve())
