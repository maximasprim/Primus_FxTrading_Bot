"""POST /api/chat/message — live conversation with the trading assistant,
including (confirmed) trade placement/close. See
app/services/chat_agent_service.py for the tool-calling logic and safety
notes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.chat_schemas import ChatRequest, ChatResponse
from app.services.chat_agent_service import run_chat_turn

router = APIRouter()


@router.post("/message", response_model=ChatResponse)
async def send_chat_message(req: ChatRequest, db: AsyncSession = Depends(get_db_session)):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty.")
    if req.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must be from the user.")
    return await run_chat_turn(req.messages, db=db, actor=req.actor)
