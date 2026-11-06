import logging

from aiogram import Bot, F, Router, types
from aiogram.types import BufferedInputFile

from app.config import WIFE_ALLOWED_CHAT_IDS
from app.gemini import ping_gemini_quota, text_to_speech_bytes
from app.state import SYSTEM_STATE, save_state_pairs

logger = logging.getLogger("wife_bots")

common_router = Router(name="common")


def is_allowed_chat(chat_id: int) -> bool:
    if not WIFE_ALLOWED_CHAT_IDS:
        return True
    return chat_id in WIFE_ALLOWED_CHAT_IDS


async def send_cross_notification(from_role: str, text: str, trainer_bot: Bot, nutrition_bot: Bot) -> None:
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
    await save_state_pairs({key: str(chat_id)})


@common_router.callback_query(F.data == "check_gemini_quota")
async def cb_check_gemini_quota(call: types.CallbackQuery):
    await call.answer(await ping_gemini_quota(), show_alert=True)


@common_router.callback_query(F.data == "tts_speak_msg")
async def cb_tts_speak_msg(call: types.CallbackQuery):
    await call.answer("Озвучиваю…")
    raw = call.message.text or call.message.caption or ""
    audio_b = await text_to_speech_bytes(raw)
    if audio_b:
        await call.message.answer_voice(voice=BufferedInputFile(audio_b, filename="voice.mp3"))
    else:
        await call.message.answer("TTS недоступен для этой модели.")


def register_acl(dp):
    from aiogram.dispatcher.middlewares.base import BaseMiddleware

    class ACLMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            chat = getattr(event, "chat", None)
            uid = getattr(chat, "id", None) if chat else None
            if uid is not None and not is_allowed_chat(uid):
                if isinstance(event, types.CallbackQuery):
                    await event.answer("Нет доступа.", show_alert=True)
                elif isinstance(event, types.Message):
                    await event.answer("Нет доступа.")
                return None
            return await handler(event, data)

    dp.message.middleware(ACLMiddleware())
    dp.callback_query.middleware(ACLMiddleware())
