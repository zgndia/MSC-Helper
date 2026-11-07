"""Movement key-hold gate used by auto-cast."""
from __future__ import annotations

import time

class MovementGate:
    """Arms only after a movement-key group is held continuously."""

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.armed = False

    def update(self, held: bool, now: float, hold_seconds: float) -> tuple[bool, bool]:
        """Return (armed_now, disarmed_now)."""
        if not held:
            was_armed = self.armed
            self.started_at = None
            self.armed = False
            return False, was_armed
        if self.started_at is None:
            self.started_at = now
        if not self.armed and now - self.started_at >= hold_seconds:
            self.armed = True
            return True, False
        return False, False


