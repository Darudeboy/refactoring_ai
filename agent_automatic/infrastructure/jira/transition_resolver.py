from __future__ import annotations

from dataclasses import dataclass

from agent_automatic.domain.common.errors import TransitionNotFound
from agent_automatic.domain.release.transitions import ResolvedTransition


@dataclass(frozen=True, slots=True)
class TransitionCandidate:
    id: str
    name: str


class TransitionResolver:
    def resolve(
        self,
        expected_status: str,
        available_transitions: list[dict],
        aliases: dict[str, list[str]] | None = None,
        *,
        preferred_transition_id: str | None = None,
    ) -> ResolvedTransition:
        expected = (expected_status or "").strip()
        if not expected:
            raise TransitionNotFound("Expected status is empty")

        candidates = [
            TransitionCandidate(id=str(t.get("id", "")).strip(), name=str(t.get("name", "")).strip())
            for t in (available_transitions or [])
            if str(t.get("id", "")).strip() and str(t.get("name", "")).strip()
        ]

        if preferred_transition_id:
            preferred = str(preferred_transition_id).strip()
            for c in candidates:
                if c.id == preferred:
                    return ResolvedTransition(id=c.id, name=c.name)

        alias_map = aliases or {}
        names_to_try = [expected]
        names_to_try.extend(alias_map.get(expected, []) or [])

        lowered = {c.name.lower(): c for c in candidates}
        for name in names_to_try:
            direct = lowered.get((name or "").strip().lower())
            if direct:
                return ResolvedTransition(id=direct.id, name=direct.name)

        # Fallback: contains-match (как в refactoring_ai/service.py)
        expected_l = expected.lower()
        for c in candidates:
            if expected_l and expected_l in c.name.lower():
                return ResolvedTransition(id=c.id, name=c.name)

        raise TransitionNotFound(
            f"Transition for '{expected_status}' not found. Available: {', '.join(c.name for c in candidates)}"
        )

