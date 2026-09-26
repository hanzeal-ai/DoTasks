from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
import os
import tempfile
from pathlib import Path
from typing import Any


MAX_VISUAL_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_VISUAL_REFERENCES = 8
VISUAL_UPLOAD_BODY_LIMIT = 15 * 1024 * 1024


class TaskVisualMixin:
    """Store intake attachments and release them after their last task finishes.

    The existing visual_references contract carries both images and opaque files.
    """

    @staticmethod
    def _detect_image(content: bytes) -> tuple[str, str]:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", ".gif"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp", ".webp"
        raise ValueError("Visual artifact must be PNG, JPEG, GIF, or WebP")

    def _visual_artifact_path(self, artifact_id: str) -> Path:
        value = str(artifact_id or "").strip()
        if not value.startswith("artifact://"):
            raise ValueError("visual artifact_id must use artifact://")
        relative = Path(value.removeprefix("artifact://"))
        root = (self.data_home / "artifacts").resolve()
        target = (root / relative).resolve()
        if target == root or root not in target.parents:
            raise ValueError("visual artifact path escapes managed storage")
        return target

    def _visual_reference_record(
        self,
        artifact_id: str,
        purpose: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        target = self._visual_artifact_path(artifact_id)
        if not target.is_file():
            raise ValueError(f"Managed visual artifact does not exist: {artifact_id}")
        content = target.read_bytes()
        if artifact_id.startswith("artifact://attachments/"):
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        else:
            content_type, _suffix = self._detect_image(content)
        return {
            "artifact_id": artifact_id,
            "path": str(target),
            "filename": Path(filename or target.name).name[:255],
            "content_type": content_type,
            "size": len(content),
            "purpose": str(purpose or "visual acceptance reference").strip(),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def upload_visual_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist an image or explicit opaque attachment as a managed reference."""
        if not isinstance(payload, dict):
            raise ValueError("visual artifact payload must be an object")
        encoded = str(payload.get("content_base64") or "").strip()
        source_path = str(payload.get("path") or "").strip()
        if bool(encoded) == bool(source_path):
            raise ValueError("Provide exactly one of path or content_base64")
        if encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 is invalid") from exc
            filename = Path(str(payload.get("filename") or "visual")).name
        else:
            source = Path(source_path).expanduser().resolve()
            if not source.is_file():
                raise ValueError(f"Visual artifact file does not exist: {source}")
            if source.stat().st_size > MAX_VISUAL_ARTIFACT_BYTES:
                raise ValueError("Visual artifact exceeds 10 MiB")
            content = source.read_bytes()
            filename = source.name
        if not content:
            raise ValueError("Visual artifact must not be empty")
        if len(content) > MAX_VISUAL_ARTIFACT_BYTES:
            raise ValueError("Visual artifact exceeds 10 MiB")
        attachment = payload.get("kind") == "attachment"
        try:
            content_type, suffix = self._detect_image(content)
            attachment = False
        except ValueError:
            if not attachment:
                raise
            suffix = Path(filename).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            # Files are opaque input, never served inline or executed by the server.
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        digest = hashlib.sha256(content).hexdigest()
        supplied_digest = str(payload.get("sha256") or "").strip().lower()
        if supplied_digest and supplied_digest != digest:
            raise ValueError("Visual artifact SHA-256 does not match")
        bucket = "attachments" if attachment else "visuals"
        destination = self.data_home / "artifacts" / bucket
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{digest}{suffix}"
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=destination, delete=False) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            try:
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
        target.chmod(0o600)
        artifact_id = f"artifact://{bucket}/{target.name}"
        return self._visual_reference_record(
            artifact_id,
            str(payload.get("purpose") or ""),
            filename,
        )

    def read_visual_artifact(self, artifact_id: str) -> dict[str, Any]:
        reference = self._visual_reference_record(artifact_id)
        content = Path(reference["path"]).read_bytes()
        return {
            **{key: value for key, value in reference.items() if key != "path"},
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    def _manage_visual_references(
        self, namespace: str, references: Any,
    ) -> list[dict[str, Any]]:
        del namespace  # Stable content-addressed storage is shared across intake kinds.
        if references in (None, []):
            return []
        if not isinstance(references, list) or any(
            not isinstance(item, dict) for item in references
        ):
            raise ValueError("visual_references must be an array of objects")
        if len(references) > MAX_VISUAL_REFERENCES:
            raise ValueError("At most 8 visual references are allowed")
        managed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in references:
            artifact_id = str(item.get("artifact_id") or "").strip()
            if artifact_id:
                reference = self._visual_reference_record(
                    artifact_id,
                    str(item.get("purpose") or ""),
                    str(item.get("filename") or ""),
                )
            else:
                reference = self.upload_visual_artifact(item)
            if reference["artifact_id"] in seen:
                continue
            seen.add(reference["artifact_id"])
            managed.append(reference)
        return managed

    @staticmethod
    def _visual_ids(references: Any) -> set[str]:
        if not isinstance(references, list):
            return set()
        return {
            str(item.get("artifact_id") or "").strip()
            for item in references
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip()
        }

    def _all_referenced_visual_ids(self) -> set[str]:
        referenced: set[str] = set()
        with self.db.connection() as connection:
            for row in connection.execute(
                "SELECT visual_references FROM requirements"
            ).fetchall():
                referenced.update(self._visual_ids(json.loads(row[0] or "[]")))
            for row in connection.execute(
                "SELECT implementation_contract FROM tasks"
            ).fetchall():
                contract = json.loads(row[0] or "{}")
                referenced.update(self._visual_ids(contract.get("visual_references")))
        return referenced

    def _delete_unreferenced_visuals(self, candidates: list[dict[str, Any]]) -> None:
        referenced = self._all_referenced_visual_ids()
        for artifact_id in self._visual_ids(candidates) - referenced:
            try:
                self._visual_artifact_path(artifact_id).unlink(missing_ok=True)
            except ValueError:
                continue

    def _release_task_visual_references(self, task_id: str) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT implementation_contract FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return
            contract = json.loads(row["implementation_contract"] or "{}")
            references = list(contract.get("visual_references") or [])
            if not references:
                return
            contract["visual_references"] = []
            connection.execute(
                "UPDATE tasks SET implementation_contract=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(contract, ensure_ascii=False), task_id),
            )
        self._delete_unreferenced_visuals(references)

    def _release_requirement_visual_references(self, requirement_id: str) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT visual_references FROM requirements WHERE id=?",
                (requirement_id,),
            ).fetchone()
            if not row:
                return
            references = json.loads(row["visual_references"] or "[]")
            if not references:
                return
            connection.execute(
                "UPDATE requirements SET visual_references='[]', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (requirement_id,),
            )
        self._delete_unreferenced_visuals(references)

    def release_visuals_for_terminal_task(self, task_id: str) -> None:
        """Release one terminal task and its parent requirement after all children end."""
        with self.db.connection() as connection:
            task = connection.execute(
                "SELECT status, requirement_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not task or task["status"] not in {"done", "cancelled"}:
            return
        requirement_id = str(task["requirement_id"] or "")
        self._release_task_visual_references(task_id)
        if not requirement_id:
            return
        with self.db.connection() as connection:
            children = connection.execute(
                "SELECT id, status FROM tasks WHERE requirement_id=?",
                (requirement_id,),
            ).fetchall()
        if not children or any(
            row["status"] not in {"done", "cancelled"} for row in children
        ):
            return
        for child in children:
            self._release_task_visual_references(child["id"])
        self._release_requirement_visual_references(requirement_id)
