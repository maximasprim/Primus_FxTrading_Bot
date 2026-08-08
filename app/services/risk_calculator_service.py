"""
Capital & Risk Calculator.

Deliberately does NOT predict whether a trade will win or lose -- it answers
a different, honest question: "given my capital and risk tolerance, what
does this specific setup cost me if it hits stop, and what does it pay if
it hits target?" That's arithmetic, not a forecast, and it's what the
"predefined risk management" language in the original spec actually means
in practice.
"""
from dataclasses import dataclass

from app.core.pip_utils import get_pip_size
from app.core.risk import position_size_from_risk


@dataclass
class RiskCalculation:
    instrument: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    capital: float
    risk_pct: float

    position_size_units: float
    risk_amount: float
    reward_amount: float
    risk_reward_ratio: float
    risk_pips: float
    reward_pips: float


def calculate_risk(
    instrument: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    capital: float,
    risk_pct: float,
) -> RiskCalculation:
    risk_distance = abs(entry - stop_loss)
    reward_distance = abs(take_profit - entry)

    if risk_distance <= 0:
        raise ValueError("Stop-loss distance must be greater than zero.")

    risk_reward_ratio = reward_distance / risk_distance
    risk_amount = capital * (risk_pct / 100)
    reward_amount = risk_amount * risk_reward_ratio
    position_size = position_size_from_risk(capital, risk_pct, entry, stop_loss)

    pip_size = get_pip_size(instrument)

    return RiskCalculation(
        instrument=instrument,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        capital=capital,
        risk_pct=risk_pct,
        position_size_units=position_size,
        risk_amount=round(risk_amount, 2),
        reward_amount=round(reward_amount, 2),
        risk_reward_ratio=round(risk_reward_ratio, 2),
        risk_pips=round(risk_distance / pip_size, 1),
        reward_pips=round(reward_distance / pip_size, 1),
    )