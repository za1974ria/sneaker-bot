"""Domain model for market product rows."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Product:
    product: str
    brand: str
    model: str
    country: str
    shops: list[str]
    avg: float | None
    variation: float | None
    trend: str
    signal: str
    signal_kind: str
    score: int
    price_min: dict[str, float]
    price_max: dict[str, float]
    price_avg: dict[str, float]
    arbitrage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
