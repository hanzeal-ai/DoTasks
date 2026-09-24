from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODEL_ID = "convaiinnovations/laya-multilingual"
MODEL_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"
POLICY_VERSION = "dotasks-decisions-v3"


@dataclass(frozen=True)
class Settings:
    token: str
    model_path: Path
    device: str = "cpu"
    threads: int = 2

    @classmethod
    def from_environment(cls) -> "Settings":
        token_file = Path(os.environ["LAYA_DECISION_TOKEN_FILE"]).expanduser()
        token = token_file.read_text().strip()
        if len(token) < 32 or any(char.isspace() for char in token):
            raise ValueError("Decision token must contain at least 32 non-whitespace characters")
        threads = int(os.environ.get("LAYA_DECISION_THREADS", "2"))
        if not 1 <= threads <= 16:
            raise ValueError("LAYA_DECISION_THREADS must be between 1 and 16")
        device = os.environ.get("LAYA_DECISION_DEVICE", "cpu")
        if device not in {"cpu", "mps"}:
            raise ValueError("LAYA_DECISION_DEVICE must be cpu or mps")
        return cls(token, Path(os.environ["LAYA_DECISION_MODEL_PATH"]).expanduser(), device, threads)
