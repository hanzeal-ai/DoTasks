from __future__ import annotations

from typing import Any


class LocationAdapter:
    """Build bounded location plans; execution always stays in the Codex agent."""

    def query_plan(self, task: dict[str, Any]) -> dict[str, Any]:
        seeds = [task.get("title", ""), task.get("goal", "")]
        seeds.extend(task.get("modules", []))
        return {
            "purpose": "Locate current symbols, callers, dependencies, impact and tests before editing.",
            "seeds": [seed for seed in seeds if seed][:8],
            "steps": [
                "Probe CodeGraph once and use a current index when available.",
                "Otherwise probe GitNexus once and use its bounded query/context route when indexed.",
                "If neither graph is usable, match the bounded seeds directly against project files and source text.",
                "Collect exact files, stable symbols, direct callers and related tests from the selected route; an absent graph is not a blocker.",
            ],
        }
