import asyncio
import logging
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import CallbackQuery, ErrorEvent, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.common import make_common_router, register_acl
from app.config import (
    PORT,
    WIFE_ALLOWED_CHAT_IDS,
    WIFE_NUTRITION_TOKEN,
    WIFE_RENDER_EXTERNAL_URL,
    WIFE_TRAINER_TOKEN,
    WIFE_USE_POLLING,
    WIFE_WEBHOOK_SECRET,
)
from app.nutrition.router import nutrition_router
from app.scheduler import background_loop
from app.sheets import ensure_sheets
from app.state import load_state_from_sheet
from app.trainer.router import trainer_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("wife_bots")

_BOOT_WEBHOOKS_OK = False


class InjectBotsMiddleware(BaseMiddleware):
    def __init__(self, trainer: Bot, nutrition: Bot):
        self.trainer = trainer
        self.nutrition = nutrition

    async def __call__(self, handler, event, data):
        data["bot_trainer"] = self.trainer
        data["bot_nutrition"] = self.nutrition
        return await handler(event, data)


def _register_errors(dp: Dispatcher) -> None:
    @dp.errors()
    async def on_handler_error(event: ErrorEvent):
        logger.exception("update handler failed: %s", event.exception)
        upd = event.update
        text = "⚠️ Что-то пошло не так. Жми /start или /menu — попробуем снова."
        try:
            if upd.callback_query:
                cq: CallbackQuery = upd.callback_query
                if cq.message:
                    await cq.message.answer(text)
                await cq.answer("Ошибка", show_alert=False)
            elif upd.message:
                msg: Message = upd.message
                await msg.answer(text)
        except Exception as notify_err:
            logger.warning("could not notify user about error: %s", notify_err)


def build_trainer_dispatcher(bot_trainer: Bot, bot_nutrition: Bot) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    _register_errors(dp)
    register_acl(dp)
    dp.message.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp.callback_query.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp["bot_trainer"] = bot_trainer
    dp["bot_nutrition"] = bot_nutrition
    dp.include_router(make_common_router())
    dp.include_router(trainer_router)
    return dp


def build_nutrition_dispatcher(bot_trainer: Bot, bot_nutrition: Bot) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    _register_errors(dp)
    register_acl(dp)
    dp.message.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp.callback_query.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp["bot_trainer"] = bot_trainer
    dp["bot_nutrition"] = bot_nutrition
    dp.include_router(make_common_router())
    dp.include_router(nutrition_router)
    return dp


async def _sheets_warmup() -> None:
    try:
        await asyncio.wait_for(load_state_from_sheet(), timeout=45)
    except Exception as e:
        logger.exception("load_state_from_sheet: %s", e)
    try:
        await asyncio.wait_for(ensure_sheets(), timeout=120)
    except Exception as e:
        logger.exception("ensure_sheets: %s", e)


async def run_polling():
    if not WIFE_TRAINER_TOKEN or not WIFE_NUTRITION_TOKEN:
        logger.error("Missing bot tokens")
        sys.exit(1)
    bot_trainer = Bot(WIFE_TRAINER_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_nutrition = Bot(WIFE_NUTRITION_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp_trainer = build_trainer_dispatcher(bot_trainer, bot_nutrition)
    dp_nutrition = build_nutrition_dispatcher(bot_trainer, bot_nutrition)
    await _sheets_warmup()
    asyncio.create_task(background_loop(bot_trainer, bot_nutrition))
    await asyncio.gather(
        dp_trainer.start_polling(bot_trainer, allowed_updates=dp_trainer.resolve_used_update_types()),
        dp_nutrition.start_polling(bot_nutrition, allowed_updates=dp_nutrition.resolve_used_update_types()),
    )


def create_app() -> web.Application:
    if not WIFE_TRAINER_TOKEN or not WIFE_NUTRITION_TOKEN:
        logger.error("Missing bot tokens")
        sys.exit(1)
    if not WIFE_RENDER_EXTERNAL_URL:
        logger.error("WIFE_RENDER_EXTERNAL_URL required for webhook mode")
        sys.exit(1)

    bot_trainer = Bot(WIFE_TRAINER_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_nutrition = Bot(WIFE_NUTRITION_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp_trainer = build_trainer_dispatcher(bot_trainer, bot_nutrition)
    dp_nutrition = build_nutrition_dispatcher(bot_trainer, bot_nutrition)

    secret = WIFE_WEBHOOK_SECRET.strip() or None

    app = web.Application()

    trainer_wh = SimpleRequestHandler(dispatcher=dp_trainer, bot=bot_trainer, secret_token=secret)
    nutrition_wh = SimpleRequestHandler(dispatcher=dp_nutrition, bot=bot_nutrition, secret_token=secret)
    trainer_wh.register(app, path="/webhook/trainer")
    nutrition_wh.register(app, path="/webhook/nutrition")

    async def health(_):
        if _BOOT_WEBHOOKS_OK:
            body = "ok"
        else:
            body = "ok (webhooks pending)"
        return web.Response(text=body)

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    setup_application(app, dp_trainer, bot=bot_trainer)
    setup_application(app, dp_nutrition, bot=bot_nutrition)

    base = WIFE_RENDER_EXTERNAL_URL.rstrip("/")

    async def _boot_background():
        global _BOOT_WEBHOOKS_OK
        wh_kw = {}
        if secret:
            wh_kw["secret_token"] = secret
            logger.info("WIFE_WEBHOOK_SECRET is set — Telegram must send X-Telegram-Bot-Api-Secret-Token")
        try:
            me_t = await bot_trainer.get_me()
            me_n = await bot_nutrition.get_me()
            logger.info("Bots online: @%s @%s", me_t.username, me_n.username)
        except Exception as e:
            logger.exception("getMe failed: %s", e)
            return
        try:
            await bot_trainer.set_webhook(
                f"{base}/webhook/trainer", drop_pending_updates=False, **wh_kw
            )
            await bot_nutrition.set_webhook(
                f"{base}/webhook/nutrition", drop_pending_updates=False, **wh_kw
            )
            for label, bot in (("trainer", bot_trainer), ("nutrition", bot_nutrition)):
                try:
                    info = await bot.get_webhook_info()
                    err = getattr(info, "last_error_message", None) or ""
                    if err:
                        logger.warning("webhook %s last_error: %s", label, err)
                except Exception:
                    pass
            _BOOT_WEBHOOKS_OK = True
            logger.info(
                "Webhooks set: %s/webhook/trainer|nutrition (acl ids=%s)",
                base,
                sorted(WIFE_ALLOWED_CHAT_IDS) if WIFE_ALLOWED_CHAT_IDS else "any",
            )
        except Exception as e:
            logger.exception("set_webhook failed: %s", e)
            return
        await _sheets_warmup()
        asyncio.create_task(background_loop(bot_trainer, bot_nutrition))

    async def _startup(_):
        # Bind port first; sheets + setWebhook in background so Render sees an open port.
        asyncio.create_task(_boot_background())

    async def _cleanup(_):
        # Do not delete_webhook on Render deploy — old instance can race new instance and mute bots.
        await bot_trainer.session.close()
        await bot_nutrition.session.close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


def main():
    # On Render always bind HTTP when public URL is set (webhook). Polling has no open port.
    if WIFE_RENDER_EXTERNAL_URL and not WIFE_USE_POLLING:
        logger.info("Webhook mode on 0.0.0.0:%s url=%s", PORT, WIFE_RENDER_EXTERNAL_URL)
        app = create_app()
        web.run_app(app, host="0.0.0.0", port=PORT)
        return
    if WIFE_USE_POLLING:
        logger.info("Polling mode (dev)")
    else:
        logger.info("No WIFE_RENDER_EXTERNAL_URL — polling fallback")
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
