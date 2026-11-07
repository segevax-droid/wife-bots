from aiogram import Bot
from aiogram.filters import Filter
from aiogram.types import TelegramObject


class IsBotToken(Filter):
    def __init__(self, token: str):
        self.token = token

    async def __call__(self, event: TelegramObject, bot: Bot) -> bool:
        return bot.token == self.token
