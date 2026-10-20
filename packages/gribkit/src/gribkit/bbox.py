from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BBox:
    """Geografischer Ausschnitt in Grad. ``west``/``east`` dürfen negativ sein;
    ``west > east`` ist nicht erlaubt (für Ausschnitte über die Datumsgrenze
    ``east`` größer 180 angeben)."""

    north: float
    south: float
    west: float
    east: float

    def __post_init__(self) -> None:
        if not -90 <= self.south < self.north <= 90:
            raise ValueError(f"Ungültige Breiten: south={self.south}, north={self.north}")
        if not self.west < self.east or self.east - self.west > 360:
            raise ValueError(f"Ungültige Längen: west={self.west}, east={self.east}")

    @property
    def width(self) -> float:
        return self.east - self.west
