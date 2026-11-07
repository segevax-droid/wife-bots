import asyncio
import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot, types
from aiogram.types import BufferedInputFile

from app.config import WIFE_GEMINI_API_KEY, WIFE_GEMINI_MODEL

logger = logging.getLogger("wife_bots")

GEMINI_MODEL = WIFE_GEMINI_MODEL


async def gemini_raw_request(payload: dict) -> Tuple[int, dict, str]:
    global GEMINI_MODEL
    headers = {"Content-Type": "application/json", "x-goog-api-key": WIFE_GEMINI_API_KEY}
    async with ClientSession(timeout=ClientTimeout(total=45)) as session:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={WIFE_GEMINI_API_KEY}"
        async with session.post(url, headers=headers, json=payload) as resp:
            status, raw_text = resp.status, await resp.text()
        if status in (404, 503) and GEMINI_MODEL != "gemini-2.0-flash":
            fb = "gemini-2.0-flash"
            url2 = f"https://generativelanguage.googleapis.com/v1beta/models/{fb}:generateContent?key={WIFE_GEMINI_API_KEY}"
            async with session.post(url2, headers=headers, json=payload) as r2:
                if r2.status == 200:
                    GEMINI_MODEL, status, raw_text = fb, 200, await r2.text()
        try:
            return status, json.loads(raw_text), raw_text
        except Exception:
            return status, {}, raw_text


def _gemini_error_hint(status: int, raw: str) -> str:
    low = (raw or "").lower()
    if status == 400 and ("api key not valid" in low or "invalid" in low and "key" in low):
        return "Ключ Gemini неверный. Проверь WIFE_GEMINI_API_KEY на Render (aistudio.google.com/apikey)."
    if status == 403:
        return "Доступ к Gemini запрещён (403). Проверь ключ и ограничения API key."
    if status == 429:
        return "Лимит Gemini (429). Подожди и повтори."
    return f"Ошибка {status}: {(raw or '')[:80]}"


async def ping_gemini_quota() -> str:
    if not WIFE_GEMINI_API_KEY:
        return "WIFE_GEMINI_API_KEY пуст на сервере."
    t0 = asyncio.get_event_loop().time()
    try:
        status, _, raw = await gemini_raw_request({"contents": [{"parts": [{"text": "ping"}]}]})
        lat = round(asyncio.get_event_loop().time() - t0, 2)
        if status == 200:
            return f"OK. Модель: {GEMINI_MODEL}. Задержка: {lat}s"
        return _gemini_error_hint(status, raw)
    except Exception as e:
        return f"Сеть: {str(e)[:60]}"


async def call_gemini(prompt: str, media_b64: Optional[str] = None, mime_type: Optional[str] = None) -> str:
    if not WIFE_GEMINI_API_KEY:
        return "⚠️ WIFE_GEMINI_API_KEY пуст."
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    if media_b64 and mime_type:
        parts.append({"inline_data": {"mime_type": mime_type, "data": media_b64}})
    try:
        status, data, raw = await gemini_raw_request({"contents": [{"parts": parts}]})
        if status != 200:
            return "⚠️ " + _gemini_error_hint(status, raw)
        cands = data.get("candidates", [])
        if cands:
            return cands[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        return "⚠️ Пустой ответ."
    except Exception as e:
        return f"⚠️ Сеть: {str(e)[:80]}"


async def download_file_as_base64(bot: Bot, file_id: str) -> Optional[str]:
    try:
        f_obj = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{bot.token}/{f_obj.file_path}"
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return base64.b64encode(await resp.read()).decode("utf-8")
    except Exception:
        pass
    return None


async def transcribe_voice_message(bot: Bot, message: types.Message) -> Optional[str]:
    if not message.voice:
        return None
    b64 = await download_file_as_base64(bot, message.voice.file_id)
    if not b64:
        return None
    tr = await call_gemini("Расшифруй голосовое сообщение дословно на русском. Только текст.", media_b64=b64, mime_type="audio/ogg")
    if tr.startswith(("⚠️", "⏳")):
        return None
    return tr


async def text_to_speech_bytes(text: str) -> Optional[bytes]:
    clean = re.sub(r"<[^>]+>", "", text or "")
    clean = re.sub(r"[*`_~#\[\]]", "", clean).strip()
    if not clean:
        return None
    headers = {"Content-Type": "application/json", "x-goog-api-key": WIFE_GEMINI_API_KEY}
    payload = {"contents": [{"parts": [{"text": clean}]}], "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}}}
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={WIFE_GEMINI_API_KEY}"
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    try:
        inline = data["candidates"][0]["content"]["parts"][0].get("inlineData") or data["candidates"][0]["content"]["parts"][0].get("inline_data")
        b64 = inline.get("data", "")
        if b64:
            return base64.b64decode(b64)
    except Exception:
        pass
    return None
