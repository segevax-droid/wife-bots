import asyncio
import logging

from aiogram import Bot, F, Router, types

from app.config import WIFE_ALLOWED_CHAT_IDS
from app.gemini import ping_gemini_quota
from app.state import SYSTEM_STATE, save_state_pairs

logger = logging.getLogger("wife_bots")

def is_allowed_chat(chat_id: int) -> bool:
    if not WIFE_ALLOWED_CHAT_IDS:
        return True
    return chat_id in WIFE_ALLOWED_CHAT_IDS


async def send_cross_notification(from_role: str, text: str, trainer_bot: Bot, nutrition_bot: Bot) -> None:  # noqa: D401
    if from_role == "trainer":
        cid = SYSTEM_STATE.get("chat_Nutrition")
        bot = nutrition_bot
    else:
        cid = SYSTEM_STATE.get("chat_Trainer")
        bot = trainer_bot
    if cid:
        try:
            await bot.send_message(int(cid), text)
        except Exception as e:
            logger.warning("cross %s: %s", from_role, e)


async def record_user_chat(role: str, chat_id: int) -> None:
    key = "chat_Trainer" if role == "trainer" else "chat_Nutrition"
    if SYSTEM_STATE.get(key) == chat_id:
        return
    SYSTEM_STATE[key] = chat_id
    asyncio.create_task(save_state_pairs({key: str(chat_id)}))


def make_common_router() -> Router:
    """Fresh router instance — aiogram forbids attaching one Router to two Dispatchers."""
    r = Router(name="common")

    @r.callback_query(F.data == "check_gemini_quota")
    async def cb_check_gemini_quota(call: types.CallbackQuery):
        await call.answer("Проверяю Gemini…")
        await call.message.answer(await ping_gemini_quota())

    return r


def register_acl(dp):
    from aiogram.dispatcher.middlewares.base import BaseMiddleware

    class ACLMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            uid = None
            chat = getattr(event, "chat", None)
            if chat is not None:
                uid = getattr(chat, "id", None)
            if uid is None and isinstance(event, types.CallbackQuery) and event.message:
                uid = event.message.chat.id
            if uid is None and hasattr(event, "from_user") and event.from_user:
                uid = event.from_user.id

            if isinstance(event, types.Message) and (event.text or "").strip():
                cmd = (event.text or "").strip().split()[0].split("@")[0].lower()
                if cmd == "/myid":
                    cid = event.chat.id if event.chat else uid
                    await event.answer(
                        f"Твой chat id: <code>{cid}</code>\n"
                        "Добавь это число в <code>WIFE_ALLOWED_CHAT_IDS</code> на Render, если бот не отвечает."
                    )
                    return None

            if uid is not None and not is_allowed_chat(uid):
                hint = f"Нет доступа (id <code>{uid}</code>). Добавь в WIFE_ALLOWED_CHAT_IDS на Render."
                if isinstance(event, types.CallbackQuery):
                    await event.answer("Нет доступа.", show_alert=True)
                    if event.message:
                        await event.message.answer(hint)
                elif isinstance(event, types.Message):
                    await event.answer(hint)
                return None
            return await handler(event, data)

    dp.message.middleware(ACLMiddleware())
    dp.callback_query.middleware(ACLMiddleware())
