"""
Correlation Management Engine (spec §18).

Tracks directional exposure per underlying currency across open/pending
signals so the system can warn about excessive correlated exposure
(e.g. EUR/USD BUY + GBP/USD BUY + XAU/USD BUY all lean on USD weakness).

# TODO: XAU/USD and XAG/USD correlation to USD is treated here as a simple
# fixed weight; consider computing rolling correlation from price data
# instead of a static assumption for production use.
"""
from dataclasses import dataclass

_INSTRUMENT_CURRENCY_EXPOSURE: dict[str, dict[str, int]] = {
    # base currency: +1 exposure if BUY, -1 if SELL; quote currency: opposite
    "EUR/USD": {"EUR": 1, "USD": -1},
    "GBP/USD": {"GBP": 1, "USD": -1},
    "AUD/USD": {"AUD": 1, "USD": -1},
    "NZD/USD": {"NZD": 1, "USD": -1},
    "USD/JPY": {"USD": 1, "JPY": -1},
    "USD/CHF": {"USD": 1, "CHF": -1},
    "USD/CAD": {"USD": 1, "CAD": -1},
    "XAU/USD": {"USD": -1},  # gold up typically implies USD weakness bias
    "XAG/USD": {"USD": -1},
}


@dataclass
class OpenPosition:
    instrument: str
    direction: str  # "BUY" | "SELL"
    risk_units: float = 1.0  # normalized risk (e.g. % of equity at risk)


def compute_currency_exposure(positions: list[OpenPosition]) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for pos in positions:
        weights = _INSTRUMENT_CURRENCY_EXPOSURE.get(pos.instrument, {})
        direction_sign = 1 if pos.direction == "BUY" else -1
        for currency, weight in weights.items():
            exposure[currency] = exposure.get(currency, 0.0) + weight * direction_sign * pos.risk_units
    return exposure


def check_correlated_exposure_warning(
    existing_positions: list[OpenPosition],
    new_instrument: str,
    new_direction: str,
    max_combined_exposure: float = 3.0,
) -> str | None:
    """Returns a warning string if adding the new position would push any
    single currency's net exposure beyond `max_combined_exposure` units of risk.
    """
    hypothetical = existing_positions + [OpenPosition(new_instrument, new_direction)]
    exposure = compute_currency_exposure(hypothetical)

    for currency, value in exposure.items():
        if abs(value) > max_combined_exposure:
            return (
                f"New signal may create excessive correlated exposure: net {currency} "
                f"exposure would be {value:+.1f} units (limit: ±{max_combined_exposure})."
            )
    return None
