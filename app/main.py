import asyncio
import logging
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.bot_filters import IsBotToken
from app.common import common_router, register_acl
from app.config import (
    PORT,
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


class InjectBotsMiddleware(BaseMiddleware):
    def __init__(self, trainer: Bot, nutrition: Bot):
        self.trainer = trainer
        self.nutrition = nutrition

    async def __call__(self, handler, event, data):
        data["bot_trainer"] = self.trainer
        data["bot_nutrition"] = self.nutrition
        return await handler(event, data)


def build_dispatcher(bot_trainer: Bot, bot_nutrition: Bot) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    register_acl(dp)
    dp.message.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp.callback_query.middleware(InjectBotsMiddleware(bot_trainer, bot_nutrition))
    dp["bot_trainer"] = bot_trainer
    dp["bot_nutrition"] = bot_nutrition
    # One Dispatcher serves both bots — scope each router to its token
    trainer_router.message.filter(IsBotToken(WIFE_TRAINER_TOKEN))
    trainer_router.callback_query.filter(IsBotToken(WIFE_TRAINER_TOKEN))
    nutrition_router.message.filter(IsBotToken(WIFE_NUTRITION_TOKEN))
    nutrition_router.callback_query.filter(IsBotToken(WIFE_NUTRITION_TOKEN))
    dp.include_router(common_router)
    dp.include_router(trainer_router)
    dp.include_router(nutrition_router)
    return dp


async def run_polling():
    if not WIFE_TRAINER_TOKEN or not WIFE_NUTRITION_TOKEN:
        logger.error("Missing bot tokens")
        sys.exit(1)
    bot_trainer = Bot(WIFE_TRAINER_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_nutrition = Bot(WIFE_NUTRITION_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(bot_trainer, bot_nutrition)
    await load_state_from_sheet()
    await ensure_sheets()
    asyncio.create_task(background_loop(bot_trainer, bot_nutrition))
    await dp.start_polling(bot_trainer, bot_nutrition, allowed_updates=dp.resolve_used_update_types())


def create_app() -> web.Application:
    if not WIFE_TRAINER_TOKEN or not WIFE_NUTRITION_TOKEN:
        logger.error("Missing bot tokens")
        sys.exit(1)
    if not WIFE_RENDER_EXTERNAL_URL:
        logger.error("WIFE_RENDER_EXTERNAL_URL required for webhook mode")
        sys.exit(1)

    bot_trainer = Bot(WIFE_TRAINER_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_nutrition = Bot(WIFE_NUTRITION_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(bot_trainer, bot_nutrition)

    secret = WIFE_WEBHOOK_SECRET.strip() or None

    app = web.Application()

    trainer_wh = SimpleRequestHandler(dispatcher=dp, bot=bot_trainer, secret_token=secret)
    nutrition_wh = SimpleRequestHandler(dispatcher=dp, bot=bot_nutrition, secret_token=secret)
    trainer_wh.register(app, path="/webhook/trainer")
    nutrition_wh.register(app, path="/webhook/nutrition")

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    setup_application(app, dp, bot=bot_trainer)

    base = WIFE_RENDER_EXTERNAL_URL.rstrip("/")

    async def _boot_background():
        try:
            await load_state_from_sheet()
            await ensure_sheets()
            wh_kw = {}
            if secret:
                wh_kw["secret_token"] = secret
            await bot_trainer.set_webhook(f"{base}/webhook/trainer", drop_pending_updates=True, **wh_kw)
            await bot_nutrition.set_webhook(f"{base}/webhook/nutrition", drop_pending_updates=True, **wh_kw)
            logger.info("Webhooks set: %s/webhook/trainer|nutrition", base)
            asyncio.create_task(background_loop(bot_trainer, bot_nutrition))
        except Exception as e:
            logger.exception("startup boot failed: %s", e)

    async def _startup(_):
        # Bind port first; sheets + setWebhook in background so Render sees an open port.
        asyncio.create_task(_boot_background())

    async def _cleanup(_):
        try:
            await bot_trainer.delete_webhook(drop_pending_updates=False)
            await bot_nutrition.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
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
