"""Shared lexical tokens; callers own ranking and filtering."""
import re


def search_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(
        r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+", str(value or "").lower()
    ):
        tokens.add(raw)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) > 2:
            for size in (2, 3, 4):
                if len(raw) >= size:
                    tokens.update(
                        raw[index : index + size]
                        for index in range(len(raw) - size + 1)
                    )
    return tokens
