"""
Manual Approval Mode (execution-feature spec §18; notification spec §10-11).

Tracks pending approval requests with an expiry window. Approving an expired
or price-moved-too-far request must fail — the user must never approve an
outdated setup.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import get_settings
from app.data.market_data_engine import MarketDataEngine
from app.models.execution_schemas import ApprovalStatus, TradeApprovalRequest
from app.models.notification_schemas import ApprovalCommand
from app.models.schemas import TradeSetup


def parse_approval_command(text: str) -> ApprovalCommand | None:
    """Parses text commands like 'APPROVE EURUSD SIGNAL-12345' or
    'REJECT EURUSD EURUSD-20260727-5M-LIQUIDITYSWE-MSS' (notification-feature
    spec §10). Returns None if the text doesn't match the expected shape —
    callers should treat that as "not a command" rather than an error.
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return None

    action = parts[0].upper()
    if action not in ("APPROVE", "REJECT"):
        return None

    # Accept both "APPROVE <SIGNAL_ID>" and "APPROVE <INSTRUMENT> <SIGNAL_ID>".
    if len(parts) == 2:
        instrument, signal_id = "", parts[1]
    else:
        instrument, signal_id = parts[1], parts[2]

    return ApprovalCommand(action=action, instrument=instrument, signal_id=signal_id)


@dataclass
class ApprovalDecisionResult:
    success: bool
    reason: str | None = None


class ApprovalService:
    def __init__(self):
        self._pending: dict[str, TradeApprovalRequest] = {}
        self._market_data = MarketDataEngine()

    def create_request(self, signal_id: str, setup: TradeSetup, risk_pct: float) -> TradeApprovalRequest:
        settings = get_settings()
        if setup.entry_zone is None or setup.stop_loss is None or setup.take_profit_1 is None:
            raise ValueError("Cannot request approval for a setup without entry/stop/target levels.")

        request = TradeApprovalRequest(
            signal_id=signal_id,
            instrument=setup.instrument,
            direction=setup.direction.value,
            confluence_score=setup.confluence_score,
            entry_zone_min=setup.entry_zone.min,
            entry_zone_max=setup.entry_zone.max,
            stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1,
            risk_pct=risk_pct,
            expires_at=datetime.utcnow() + timedelta(seconds=settings.approval_expiry_seconds),
        )
        self._pending[signal_id] = request
        return request

    def get(self, signal_id: str) -> TradeApprovalRequest | None:
        return self._pending.get(signal_id)

    def _expire_if_needed(self, request: TradeApprovalRequest) -> bool:
        if request.status == ApprovalStatus.PENDING and datetime.utcnow() > request.expires_at:
            request.status = ApprovalStatus.EXPIRED
            return True
        return False

    async def approve(self, signal_id: str, decided_by: str, price_tolerance_pct: float = 0.0015) -> ApprovalDecisionResult:
        request = self._pending.get(signal_id)
        if request is None:
            return ApprovalDecisionResult(False, "Unknown or already-processed signal ID.")

        if self._expire_if_needed(request):
            return ApprovalDecisionResult(False, "Approval expired. Market conditions must be reanalysed.")

        if request.status != ApprovalStatus.PENDING:
            return ApprovalDecisionResult(False, f"Signal already {request.status.value.lower()}.")

        # Re-validate: has price moved too far from the original entry zone?
        current = await self._market_data.get_current_price(request.instrument)
        mid = (current["bid"] + current["ask"]) / 2
        zone_mid = (request.entry_zone_min + request.entry_zone_max) / 2
        drift_pct = abs(mid - zone_mid) / zone_mid
        if drift_pct > price_tolerance_pct:
            request.status = ApprovalStatus.EXPIRED
            return ApprovalDecisionResult(
                False,
                f"Market moved {drift_pct * 100:.2f}% from the original entry zone "
                f"(original mid {zone_mid}, current {mid}). Setup must be reanalysed.",
            )

        request.status = ApprovalStatus.APPROVED
        request.decided_at = datetime.utcnow()
        request.decided_by = decided_by
        return ApprovalDecisionResult(True)

    def reject(self, signal_id: str, decided_by: str) -> ApprovalDecisionResult:
        request = self._pending.get(signal_id)
        if request is None:
            return ApprovalDecisionResult(False, "Unknown or already-processed signal ID.")
        if self._expire_if_needed(request):
            return ApprovalDecisionResult(False, "Approval already expired.")

        request.status = ApprovalStatus.REJECTED
        request.decided_at = datetime.utcnow()
        request.decided_by = decided_by
        return ApprovalDecisionResult(True)


approval_service = ApprovalService()


async def execute_approval_command(command: ApprovalCommand, decided_by: str) -> ApprovalDecisionResult:
    """Dispatches a parsed APPROVE/REJECT command to the approval service.
    Caller (the inbound WhatsApp/SMS webhook route) is responsible for
    verifying sender identity BEFORE calling this — see
    app/api/routes/notifications.py — since identity depends on the
    transport (verified sender phone number), not on anything in the
    command text itself.
    """
    if command.action == "APPROVE":
        return await approval_service.approve(command.signal_id, decided_by=decided_by)
    return approval_service.reject(command.signal_id, decided_by=decided_by)
