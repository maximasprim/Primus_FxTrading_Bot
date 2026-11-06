"""
Dashboard Chat Agent.

Lets the user hold a live conversation with the same analysis/execution
stack that powers the rest of the dashboard, including asking it to place
or close trades. This module is deliberately just an orchestration layer:

  * It NEVER talks to a broker directly. Every trade action goes through
    app/execution/execution_service.py's process_manual_entry /
    process_manual_close - the exact same functions backing the dashboard's
    "Enter" / "Close" buttons (see app/api/routes/execution.py) - so every
    hard safety gate (duplicate protection, emergency stop, instrument
    DISABLED mode, news risk, daily/weekly loss limits, consecutive losses,
    safety shutdown battery) still applies to an order placed via chat.
  * It never invents market data - every fact it can cite comes from a
    tool call into the real engines/services, same principle as
    app/engines/ai_decision.py's SYSTEM_PROMPT.
  * Placing or closing a trade requires the model to have already asked
    for and received explicit confirmation from the user in the
    conversation (enforced via the system prompt) - the tools themselves
    additionally require an `confirm=true` argument so a slip in the
    model's reasoning can't silently fire an order.

Talks to whichever provider is currently selected (see
app/ai/settings_manager.py) through the provider-agnostic app/ai/base.py
types - this module never imports httpx or knows about any provider's
wire format directly.
"""
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import AIMessage, AIProviderError
from app.ai.factory import get_current_provider, has_api_key_for
from app.ai.settings_manager import ai_settings_manager
from app.data.market_data_engine import MarketDataEngine
from app.engines.market_regime import detect_regime
from app.engines.market_structure import analyze_market_structure
from app.execution.execution_service import get_broker, process_manual_close, process_manual_entry, process_manual_modify
from app.core.trading_mode import trading_mode_manager
from app.models.chat_schemas import ChatMessage, ChatResponse, ChatToolAction
from app.models.schemas import SetupStatus
from app.services.account_state_service import build_account_state
from app.services.signal_service import run_full_analysis
from app.services.trade_journal_service import get_open_trades, get_trade_history

MAX_TOOL_ITERATIONS = 5

SYSTEM_PROMPT = """You are the PriMus trading desk assistant, embedded in a live Forex/XAU \
confluence-analysis dashboard. You help the user understand current setups and, when they \
explicitly want to, place or close trades on their behalf.

Ground rules:
- Never invent prices, scores, or market structure. Always call a tool to get current facts \
before stating them; if a tool call fails or a value is missing, say so plainly.
- Never claim a trade is guaranteed to win. Confluence scores describe how many factors align, \
not a probability of profit.
- Before calling place_order or close_position, you MUST first state the exact instrument, \
direction, entry zone, stop loss, take profit and confluence score (for place_order) or the \
current open position details (for close_position), and explicitly ask the user to confirm. \
Only call the tool with confirm=true in a LATER turn, after the user has clearly said yes/confirm/do it \
to that specific proposal. If the user's message is ambiguous about which trade they're confirming, \
ask again rather than guessing. modify_stop_take_profit does not require this confirm step, but always \
tell the user exactly what you changed the stop/target to.
- If a tool reports the action was blocked or rejected (e.g. emergency stop active, news risk, \
risk-management gate, no confirmed setup), relay the exact reason to the user - do not soften or \
omit it, and do not retry automatically.
- Keep responses concise and use plain language a discretionary trader would expect, not code or JSON.
"""

TOOLS = [
    {
        "name": "get_market_snapshot",
        "description": "Current bid/ask, market bias, and market regime for one instrument.",
        "input_schema": {
            "type": "object",
            "properties": {"instrument": {"type": "string", "description": "e.g. EUR/USD, GBP/USD, XAU/USD"}},
            "required": ["instrument"],
        },
    },
    {
        "name": "get_analysis",
        "description": "Full confluence analysis for one instrument: direction, grade, confidence "
        "level, the full score breakdown with a reason per category, entry/stop/targets, and the "
        "scenario analysis.",
        "input_schema": {
            "type": "object",
            "properties": {"instrument": {"type": "string", "description": "e.g. EUR/USD, GBP/USD, XAU/USD"}},
            "required": ["instrument"],
        },
    },
    {
        "name": "list_open_positions",
        "description": "Every currently open position across all instruments, with entry/stop/target, "
        "P&L, and how it was opened (manual/approved/automatic).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trading_control_status",
        "description": "Current global trading mode, whether auto-trading/emergency-stop are active, "
        "and per-instrument mode overrides.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "place_order",
        "description": "Manually enter a trade on the CURRENT confirmed setup for an instrument. "
        "Only call with confirm=true after the user has explicitly confirmed the exact setup you "
        "described to them (see system prompt).",
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true; the user has just confirmed this exact trade."},
            },
            "required": ["instrument", "confirm"],
        },
    },
    {
        "name": "close_position",
        "description": "Manually close the open position(s) on an instrument at the current market price. "
        "Only call with confirm=true after the user has explicitly confirmed. Pass `fraction` (0-1) to "
        "close only part of the position, e.g. 0.5 to take half off and leave the rest running; omit it "
        "(or use 1.0) for a full close.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true; the user has just confirmed this close."},
                "fraction": {"type": "number", "description": "Fraction of the position to close, 0 < x <= 1.0. Defaults to 1.0 (full close)."},
            },
            "required": ["instrument", "confirm"],
        },
    },
    {
        "name": "modify_stop_take_profit",
        "description": "Move the stop-loss and/or take-profit on the open position for an instrument "
        "(e.g. moving the stop to breakeven). At least one of new_stop_loss/new_take_profit must be given. "
        "Does not require confirm=true - adjusting stop/target on an existing position is lower-risk than "
        "opening or closing one, but you should still tell the user what you changed it to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "new_stop_loss": {"type": "number"},
                "new_take_profit": {"type": "number"},
            },
            "required": ["instrument"],
        },
    },
    {
    "name": "get_account_status",
    "description": "Whether the bot is actually connected to a broker account right now (paper or "
    "live), and if so: broker name, account id, currency, balance, equity, margin used/free, "
    "leverage, and server. Use this whenever the user asks about their balance, equity, margin, "
    "or whether the account/broker is connected.",
    "input_schema": {"type": "object", "properties": {}},
    },
    {
    "name": "get_trade_history",
    "description": "The account's trade history -- open AND closed trades, most recent first, with "
    "entry/exit, direction, result (WIN/LOSS/BE/OPEN), P&L, and how each was opened (manual/chat/"
    "approved/automatic). Use this for questions like 'how has this account done', 'show me my past "
    "trades', or 'what's my win rate on GBP/USD'. Optionally filter by instrument. Capped at 200 "
    "trades per call -- if total_count in the result is larger than what's returned, say so rather "
    "than implying you've seen everything.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument": {"type": "string", "description": "Optional -- e.g. EUR/USD. Omit for all instruments."},
            "limit": {"type": "integer", "description": "Max trades to return, most recent first. Defaults to 50, capped at 200."},
        },
    },
    },
]


def _normalize_instrument(raw: str) -> str:
    return raw.replace("-", "/").upper()


async def _tool_get_market_snapshot(args: dict) -> dict:
    instrument = _normalize_instrument(args["instrument"])
    engine = MarketDataEngine()
    from datetime import timedelta

    candles = await engine.get_validated_candles(instrument, "1H", lookback=timedelta(days=30))
    price = await engine.get_current_price(instrument)
    structure = analyze_market_structure(candles)
    regime = detect_regime(candles)
    return {
        "instrument": instrument,
        "bid": price["bid"],
        "ask": price["ask"],
        "market_bias": structure.bias.value,
        "market_regime": regime.value,
    }


async def _tool_get_analysis(args: dict) -> dict:
    instrument = _normalize_instrument(args["instrument"])
    setup = await run_full_analysis(instrument)
    return json.loads(setup.model_dump_json())


async def _tool_list_open_positions(args: dict, db: AsyncSession) -> dict:
    open_trades = await get_open_trades(db)
    return {
        "positions": [
            {
                "signal_id": t.signal_id,
                "instrument": t.instrument,
                "direction": t.direction,
                "entry": t.entry,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "confluence_score": t.confluence_score,
                "execution_source": t.execution_source,
            }
            for t in open_trades
        ]
    }


async def _tool_get_trading_control_status(args: dict) -> dict:
    state = trading_mode_manager.state
    return {
        "global_mode": state.global_mode.value,
        "auto_trading_enabled": state.auto_trading_enabled,
        "emergency_stop_active": state.emergency_stop_active,
        "instrument_modes": {k: v.value for k, v in state.instrument_modes.items()},
    }

async def _tool_get_account_status(args: dict, db: AsyncSession) -> dict:
    broker = get_broker()
    try:
        info = await broker.get_account_info()
    except Exception as exc:
        return {"connected": False, "error": str(exc)}

    try:
        healthy = await broker.is_connection_healthy()
    except Exception:
        healthy = False

    open_trades = await get_open_trades(db)

    return {
        "connected": info.connected and healthy,
        "broker_name": info.broker_name,
        "account_id": info.account_id,
        "currency": info.currency,
        "balance": info.balance,
        "equity": info.equity,
        "margin_used": info.margin_used,
        "margin_free": info.margin_free,
        "leverage": info.leverage,
        "server": info.server,
        "error": info.error,
        "open_positions_count": len(open_trades),
    }

async def _tool_get_trade_history(args: dict, db: AsyncSession) -> dict:
    instrument = _normalize_instrument(args["instrument"]) if args.get("instrument") else None
    limit = int(args.get("limit") or 50)
    entries, total = await get_trade_history(db, instrument=instrument, limit=limit)
    return {
        "instrument_filter": instrument,
        "returned_count": len(entries),
        "total_count": total,
        "trades": [
            {
                "signal_id": t.signal_id,
                "instrument": t.instrument,
                "direction": t.direction,
                "entry": t.entry,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "position_size": t.position_size,
                "result": t.result,
                "profit_loss": t.profit_loss,
                "r_multiple": t.r_multiple,
                "execution_source": t.execution_source,
                "confluence_score": t.confluence_score,
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            }
            for t in entries
        ],
    }

async def _tool_place_order(args: dict, db: AsyncSession, actor: str) -> dict:
    if not args.get("confirm"):
        return {"placed": False, "reason": "Not confirmed - ask the user to confirm the exact trade first."}
    instrument = _normalize_instrument(args["instrument"])
    setup = await run_full_analysis(instrument)
    if setup.status != SetupStatus.CONFIRMED:
        return {
            "placed": False,
            "reason": f"No confirmed setup for {instrument} right now (status: {setup.status.value}, "
            f"grade: {setup.grade.value}). Nothing to enter.",
        }
    account = await build_account_state(db, get_broker())
    result = await process_manual_entry(setup, db=db, account=account, actor=actor)
    if result.executed:
        return {
            "placed": True,
            "instrument": instrument,
            "direction": setup.direction.value,
            "broker_order_id": result.broker_order_id,
            "position_size": result.position_size,
        }
    return {
        "placed": False,
        "reason": (result.failure.reason if result.failure else "Execution did not complete."),
        "stage": (result.failure.stage if result.failure else None),
    }


async def _tool_close_position(args: dict, db: AsyncSession, actor: str) -> dict:
    if not args.get("confirm"):
        return {"closed": False, "reason": "Not confirmed - ask the user to confirm the close first."}
    instrument = _normalize_instrument(args["instrument"])
    fraction = float(args.get("fraction") or 1.0)
    results = await process_manual_close(instrument, db=db, actor=actor, close_fraction=fraction)
    if not results:
        return {"closed": False, "reason": f"No open position found for {instrument}."}
    return {"closed": True, "instrument": instrument, "fraction": fraction, "results": results}


async def _tool_modify_stop_take_profit(args: dict, db: AsyncSession, actor: str) -> dict:
    instrument = _normalize_instrument(args["instrument"])
    results = await process_manual_modify(
        instrument,
        db=db,
        actor=actor,
        new_stop_loss=args.get("new_stop_loss"),
        new_take_profit=args.get("new_take_profit"),
    )
    return {"instrument": instrument, "results": results}


async def _dispatch_tool(name: str, args: dict, db: AsyncSession, actor: str) -> dict:
    if name == "get_market_snapshot":
        return await _tool_get_market_snapshot(args)
    if name == "get_analysis":
        return await _tool_get_analysis(args)
    if name == "list_open_positions":
        return await _tool_list_open_positions(args, db)
    if name == "get_trading_control_status":
        return await _tool_get_trading_control_status(args)
    if name == "get_account_status":
        return await _tool_get_account_status(args, db)
    if name == "get_trade_history":
        return await _tool_get_trade_history(args, db)
    if name == "place_order":
        return await _tool_place_order(args, db, actor)
    if name == "close_position":
        return await _tool_close_position(args, db, actor)
    if name == "modify_stop_take_profit":
        return await _tool_modify_stop_take_profit(args, db, actor)
    return {"error": f"Unknown tool '{name}'."}


def _summarize_action(name: str, args: dict, result: dict) -> str:
    if name == "place_order":
        if result.get("placed"):
            return f"Placed {result.get('direction')} on {result.get('instrument')}."
        return f"Did not place order on {args.get('instrument')}: {result.get('reason')}"
    if name == "close_position":
        if result.get("closed"):
            frac = result.get("fraction", 1.0)
            return f"{'Partially closed' if frac < 1.0 else 'Closed'} position(s) on {result.get('instrument')}."
        return f"Did not close position on {args.get('instrument')}: {result.get('reason')}"
    if name == "modify_stop_take_profit":
        return f"Updated stop/target on {result.get('instrument')}."
    return f"Called {name}."


async def run_chat_turn(messages: list[ChatMessage], db: AsyncSession, actor: str) -> ChatResponse:
    provider_name = ai_settings_manager.state.provider
    if not has_api_key_for(provider_name):
        return ChatResponse(
            reply=f"Chat isn't configured yet - no API key set for the current provider ({provider_name}). "
            f"Set the matching key in the environment, or pick a different provider in Settings.",
            actions=[],
        )
    provider = get_current_provider()

    history: list[AIMessage] = [AIMessage(role=m.role, content=m.content) for m in messages]
    actions_taken: list[ChatToolAction] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = await provider.complete(system=SYSTEM_PROMPT, messages=history, tools=TOOLS, max_tokens=1500)
        except AIProviderError as exc:
            # Surface the real reason (bad model name, malformed request,
            # invalid key, billing, etc.) instead of a bare 500 with no context.
            return ChatResponse(
                reply=f"The trading assistant hit an error talking to the model API ({provider.name}): {exc}",
                actions=actions_taken,
            )

        if not response.tool_calls:
            return ChatResponse(reply=response.text, actions=actions_taken)

        # The model wants to use one or more tools: run them, then loop back
        # with the results appended so it can produce a final reply.
        history.append(AIMessage(role="assistant", content=response.text or None, tool_calls=response.tool_calls))
        for tc in response.tool_calls:
            try:
                result = await _dispatch_tool(tc.name, tc.input, db, actor)
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
            actions_taken.append(
                ChatToolAction(tool=tc.name, input=tc.input, summary=_summarize_action(tc.name, tc.input, result))
            )
            history.append(
                AIMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=json.dumps(result, default=str))
            )

    return ChatResponse(
        reply="I wasn't able to finish that within the allowed number of steps - could you rephrase or simplify the request?",
        actions=actions_taken,
    )
