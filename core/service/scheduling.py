from __future__ import annotations

from typing import Any


class TaskSchedulingMixin:
    """Durable, coalesced scheduling wakeups and one leased controller cycle."""

    def scheduler_snapshot(self) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
        if not row:
            raise RuntimeError("scheduler_state is not initialized")
        item = dict(row)
        item["pending"] = bool(item.get("pending"))
        item["dispatcher_enabled"] = self.dispatcher_enabled()
        return item

    @staticmethod
    def _request_schedule(
        connection: Any,
        trigger: str,
        entity_type: str = "",
        entity_id: str = "",
    ) -> None:
        """Coalesce a queue-relevant state change inside its owning transaction."""
        connection.execute(
            """UPDATE scheduler_state
               SET generation=generation+1, pending=1, last_trigger=?,
                   last_entity_type=?, last_entity_id=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=1""",
            (
                str(trigger or "state_changed")[:200],
                str(entity_type or "")[:40],
                str(entity_id or "")[:100],
            ),
        )

    def claim_schedule_cycle(
        self,
        worker_id: str,
        project: str | None = None,
        lease_seconds: int = 1800,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Lease one global cycle, then recover Review and fill development slots."""
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        if not self.dispatcher_enabled():
            return {
                "status": "paused",
                "worker_id": worker_id,
                "scheduler": self.scheduler_snapshot(),
                "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                "development": {"stage": "development", "capacity": 0, "dispatches": []},
            }

        with self.db.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            if not state:
                raise RuntimeError("scheduler_state is not initialized")
            if not force and not bool(state["pending"]):
                return {
                    "status": "idle",
                    "worker_id": worker_id,
                    "scheduler": {**dict(state), "pending": False, "dispatcher_enabled": True},
                    "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                    "development": {"stage": "development", "capacity": 0, "dispatches": []},
                }
            leased = connection.execute(
                """UPDATE scheduler_state
                   SET lease_owner=?, lease_expires_at=datetime('now', ?),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=1 AND (
                     lease_owner='' OR lease_expires_at IS NULL
                     OR lease_expires_at<=CURRENT_TIMESTAMP
                   )""",
                (worker_id, f"+{lease_seconds} seconds"),
            )
            if leased.rowcount != 1:
                return {
                    "status": "busy",
                    "worker_id": worker_id,
                    "scheduler": {**dict(state), "pending": bool(state["pending"]), "dispatcher_enabled": True},
                    "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                    "development": {"stage": "development", "capacity": 0, "dispatches": []},
                }

        try:
            # Review is an independent one-slot lane. Development/rework uses
            # the configured capacity and the existing atomic eligibility gates.
            code_review = self._claim_native_dispatch_batch(
                worker_id, project, lease_seconds, "code_review"
            )
            development = self._claim_native_dispatch_batch(
                worker_id, project, lease_seconds, "development"
            )
        except Exception:
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE scheduler_state SET lease_owner='', lease_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP WHERE id=1 AND lease_owner=?""",
                    (worker_id,),
                )
            raise

        snapshot = self.scheduler_snapshot()
        return {
            "status": "claimed",
            "worker_id": worker_id,
            "cycle_generation": int(snapshot["generation"]),
            "scheduler": snapshot,
            "code_review": code_review,
            "development": development,
        }

    def complete_schedule_cycle(
        self, worker_id: str, generation: int
    ) -> dict[str, Any]:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        generation = max(0, int(generation))
        with self.db.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            if not state:
                raise RuntimeError("scheduler_state is not initialized")
            if state["lease_owner"] != worker_id:
                raise ValueError("Schedule cycle is leased by another worker")
            if generation > int(state["generation"]):
                raise ValueError("Schedule cycle generation is invalid")
            handled = max(int(state["handled_generation"]), generation)
            unresolved_native = connection.execute(
                "SELECT 1 FROM native_dispatches WHERE status='pending_thread' LIMIT 1"
            ).fetchone()
            pending = int(
                int(state["generation"]) > handled or unresolved_native is not None
            )
            connection.execute(
                """UPDATE scheduler_state SET handled_generation=?, pending=?,
                   lease_owner='', lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=1""",
                (handled, pending),
            )
        return self.scheduler_snapshot()
