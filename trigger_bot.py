"""
Telegram-бот для групповых чатов с SQLite + AI-генерация через OpenRouter.

Два режима ответов на триггеры:
  - classic : стандартные ответы из базы
  - ai      : GPT-4o-mini генерирует смешной ответ на основе триггера

Установка:
    pip install python-telegram-bot==20.7 httpx==0.27.0

Запуск:
    1. Получи токен у @BotFather
    2. Получи API-ключ на https://openrouter.ai/keys
    3. Заполни .env (BOT_TOKEN, OPENROUTER_API_KEY)
    4. python trigger_bot.py

Команды:
    /add триггер = ответ       — добавить двусторонний триггер
    /add1 триггер = ответ      — добавить односторонний триггер
    /del триггер               — удалить триггер
    /list                      — список триггеров группы
    /clear                     — удалить ВСЕ триггеры (только админы)
    /mode                      — переключить режим (classic ↔ ai)
    /temp 0.0-2.0              — установить температуру AI
    /help                      — справка
"""

import os
import sqlite3
import logging
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── Настройки ───────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "triggers.db"))

# Системный промпт для AI-режима
AI_SYSTEM_PROMPT = (
    "Ты — бот-тролль в русскоязычном групповом чате. "
    "Тебе дают слово-триггер и стандартный ответ на него. "
    "Твоя задача — придумать СМЕШНОЙ, дерзкий, остроумный вариант ответа. "
    "Используй сленг, мемы, абсурдный юмор. "
    "Ответ должен быть КОРОТКИМ — максимум 1-2 предложения. "
    "Не объясняй шутку. Не извиняйся. Просто будь смешным."
)

# Дефолтные пары
DEFAULT_PAIRS = [
    ("да",       "пизда",                True),
    ("нет",      "пидора ответ",         True),
    ("ясно",     "хуясно",              True),
    ("тоже",     "на говно похоже",      True),
    ("300",      "отсоси у тракториста",  True),
    ("здрасьте", "забор покрасьте",      True),
    ("ладно",    "нахуй иди",           True),
]

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── HTTP-клиент для OpenRouter ──────────────────────────────────────────────

http_client = httpx.AsyncClient(timeout=15.0)


async def ai_generate(trigger: str, classic_response: str, temperature: float) -> str | None:
    """Генерирует смешной ответ через OpenRouter."""
    if not OPENROUTER_API_KEY:
        return None

    user_prompt = (
        f"Триггерное слово: «{trigger}»\n"
        f"Классический ответ: «{classic_response}»\n"
        f"Придумай смешную альтернативу:"
    )

    try:
        resp = await http_client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "temperature": temperature,
                "max_tokens": 150,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return None


# ─── База данных ─────────────────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triggers (
            chat_id    INTEGER NOT NULL,
            trigger_w  TEXT    NOT NULL,
            response   TEXT    NOT NULL,
            bidirect   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (chat_id, trigger_w)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS initialized_chats (
            chat_id INTEGER PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id     INTEGER PRIMARY KEY,
            mode        TEXT    NOT NULL DEFAULT 'classic',
            temperature REAL   NOT NULL DEFAULT 1.0
        )
        """
    )
    conn.commit()
    return conn


# ─── Настройки чата ──────────────────────────────────────────────────────────


def get_chat_settings(chat_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT mode, temperature FROM chat_settings WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row:
        return {"mode": row[0], "temperature": row[1]}
    return {"mode": "classic", "temperature": 1.0}


def set_chat_mode(chat_id: int, mode: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_settings (chat_id, mode) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET mode = excluded.mode",
        (chat_id, mode),
    )
    conn.commit()
    conn.close()


def set_chat_temperature(chat_id: int, temp: float) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_settings (chat_id, temperature) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET temperature = excluded.temperature",
        (chat_id, temp),
    )
    conn.commit()
    conn.close()


# ─── Триггеры (CRUD) ────────────────────────────────────────────────────────


def ensure_defaults(chat_id: int) -> None:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM initialized_chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row:
        conn.close()
        return
    for trigger_w, response, bidirect in DEFAULT_PAIRS:
        conn.execute(
            "INSERT OR IGNORE INTO triggers (chat_id, trigger_w, response, bidirect) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, trigger_w.lower(), response, int(bidirect)),
        )
        if bidirect:
            conn.execute(
                "INSERT OR IGNORE INTO triggers (chat_id, trigger_w, response, bidirect) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, response.lower(), trigger_w, int(bidirect)),
            )
    conn.execute("INSERT INTO initialized_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()


def get_triggers(chat_id: int) -> list[tuple[str, str, bool]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT trigger_w, response, bidirect FROM triggers WHERE chat_id = ?",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [(r[0], r[1], bool(r[2])) for r in rows]


def add_trigger(chat_id: int, trigger_w: str, response: str, bidirect: bool) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO triggers (chat_id, trigger_w, response, bidirect) "
        "VALUES (?, ?, ?, ?)",
        (chat_id, trigger_w.lower(), response, int(bidirect)),
    )
    if bidirect:
        conn.execute(
            "INSERT OR REPLACE INTO triggers (chat_id, trigger_w, response, bidirect) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, response.lower(), trigger_w, int(bidirect)),
        )
    conn.commit()
    conn.close()


def del_trigger(chat_id: int, trigger_w: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT response, bidirect FROM triggers WHERE chat_id = ? AND trigger_w = ?",
        (chat_id, trigger_w.lower()),
    ).fetchone()
    deleted = 0
    if row:
        response, bidirect = row[0], bool(row[1])
        conn.execute(
            "DELETE FROM triggers WHERE chat_id = ? AND trigger_w = ?",
            (chat_id, trigger_w.lower()),
        )
        deleted += 1
        if bidirect:
            conn.execute(
                "DELETE FROM triggers WHERE chat_id = ? AND trigger_w = ?",
                (chat_id, response.lower()),
            )
            deleted += 1
    conn.commit()
    conn.close()
    return deleted


def clear_triggers(chat_id: int) -> int:
    conn = get_db()
    cur = conn.execute("DELETE FROM triggers WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM initialized_chats WHERE chat_id = ?", (chat_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ─── Поиск триггера ─────────────────────────────────────────────────────────


def find_trigger(chat_id: int, text: str) -> tuple[str, str] | None:
    """Возвращает (trigger_word, response) или None."""
    triggers = get_triggers(chat_id)
    text_lower = text.lower().strip()
    words = text_lower.split()

    triggers_sorted = sorted(triggers, key=lambda t: len(t[0].split()), reverse=True)

    for key, response, _ in triggers_sorted:
        key_words = key.split()
        key_len = len(key_words)
        if text_lower == key:
            return (key, response)
        for i in range(len(words) - key_len + 1):
            if words[i : i + key_len] == key_words:
                return (key, response)

    return None


# ─── Проверка прав ───────────────────────────────────────────────────────────


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


# ─── Обработчики команд ─────────────────────────────────────────────────────


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_chat_settings(update.effective_chat.id)
    mode_icon = "🤖" if settings["mode"] == "ai" else "📝"
    ai_status = ""
    if not OPENROUTER_API_KEY:
        ai_status = "\n\n⚠️ <i>OPENROUTER_API_KEY не задан — AI-режим недоступен</i>"

    text = (
        "🤖 <b>Триггер-бот — справка</b>\n\n"
        "<b>Триггеры:</b>\n"
        "<code>/add триггер = ответ</code> — добавить (↔️)\n"
        "<code>/add1 триггер = ответ</code> — добавить (➡️)\n"
        "<code>/del триггер</code> — удалить\n"
        "<code>/list</code> — список триггеров\n"
        "<code>/clear</code> — удалить всё (админы)\n\n"
        "<b>AI-режим:</b>\n"
        "<code>/mode</code> — переключить classic ↔ ai\n"
        "<code>/temp 0.0-2.0</code> — температура генерации\n\n"
        f"Текущий режим: {mode_icon} <b>{settings['mode']}</b> "
        f"(temp: {settings['temperature']})"
        f"{ai_status}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    settings = get_chat_settings(chat_id)

    if settings["mode"] == "classic":
        if not OPENROUTER_API_KEY:
            await update.message.reply_text(
                "⚠️ AI-режим недоступен: OPENROUTER_API_KEY не задан."
            )
            return
        set_chat_mode(chat_id, "ai")
        await update.message.reply_text(
            "🤖 Режим переключён на <b>AI</b>\n"
            "Теперь ответы генерирует GPT-4o-mini!",
            parse_mode="HTML",
        )
    else:
        set_chat_mode(chat_id, "classic")
        await update.message.reply_text(
            "📝 Режим переключён на <b>classic</b>\n"
            "Стандартные ответы из базы.",
            parse_mode="HTML",
        )


async def cmd_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    raw = update.message.text.split(maxsplit=1)

    if len(raw) < 2:
        settings = get_chat_settings(chat_id)
        await update.message.reply_text(
            f"Текущая температура: <b>{settings['temperature']}</b>\n"
            f"Формат: <code>/temp 0.0-2.0</code>\n\n"
            f"🧊 0.0 = предсказуемо\n"
            f"🔥 2.0 = полный хаос",
            parse_mode="HTML",
        )
        return

    try:
        temp = float(raw[1].replace(",", "."))
        if not 0.0 <= temp <= 2.0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Температура должна быть числом от 0.0 до 2.0")
        return

    set_chat_temperature(chat_id, temp)
    icons = {0: "🧊", 1: "🌡", 2: "🔥"}
    icon = icons.get(int(temp), "🌡")
    await update.message.reply_text(
        f"{icon} Температура установлена: <b>{temp}</b>", parse_mode="HTML"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add_trigger(update, context, bidirect=True)


async def cmd_add1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add_trigger(update, context, bidirect=False)


async def _add_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE, bidirect: bool
) -> None:
    chat_id = update.effective_chat.id
    ensure_defaults(chat_id)

    raw = update.message.text.split(maxsplit=1)
    if len(raw) < 2 or "=" not in raw[1]:
        await update.message.reply_text(
            "Формат: <code>/add триггер = ответ</code>", parse_mode="HTML"
        )
        return

    parts = raw[1].split("=", maxsplit=1)
    trigger_w = parts[0].strip()
    response = parts[1].strip()

    if not trigger_w or not response:
        await update.message.reply_text("Триггер и ответ не могут быть пустыми.")
        return

    add_trigger(chat_id, trigger_w, response, bidirect)

    direction = "↔️ двусторонний" if bidirect else "➡️ односторонний"
    await update.message.reply_text(
        f"✅ Добавлено ({direction}):\n<b>{trigger_w}</b> → {response}",
        parse_mode="HTML",
    )


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    ensure_defaults(chat_id)

    raw = update.message.text.split(maxsplit=1)
    if len(raw) < 2:
        await update.message.reply_text(
            "Формат: <code>/del триггер</code>", parse_mode="HTML"
        )
        return

    trigger_w = raw[1].strip()
    deleted = del_trigger(chat_id, trigger_w)

    if deleted:
        await update.message.reply_text(
            f"🗑 Удалено: <b>{trigger_w}</b>", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"Триггер «{trigger_w}» не найден.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    ensure_defaults(chat_id)

    triggers = get_triggers(chat_id)
    if not triggers:
        await update.message.reply_text("Список триггеров пуст.")
        return

    seen = set()
    lines = []
    for trigger_w, response, bidirect in sorted(triggers, key=lambda t: t[0]):
        pair_key = tuple(sorted([trigger_w, response.lower()]))
        if bidirect and pair_key in seen:
            continue
        seen.add(pair_key)
        arrow = "↔️" if bidirect else "➡️"
        lines.append(f"  {arrow} <b>{trigger_w}</b> → {response}")

    settings = get_chat_settings(chat_id)
    mode_icon = "🤖" if settings["mode"] == "ai" else "📝"
    header = f"📋 <b>Триггеры ({len(lines)})</b> | режим: {mode_icon} {settings['mode']}\n"
    await update.message.reply_text(header + "\n".join(lines), parse_mode="HTML")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Только админы могут очищать триггеры.")
        return

    chat_id = update.effective_chat.id
    deleted = clear_triggers(chat_id)
    await update.message.reply_text(f"🗑 Удалено триггеров: {deleted}")


# ─── Обработчик сообщений ───────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if update.message.from_user and update.message.from_user.is_bot:
        return

    chat_id = update.effective_chat.id
    ensure_defaults(chat_id)

    result = find_trigger(chat_id, update.message.text)
    if not result:
        return

    trigger_word, classic_response = result
    settings = get_chat_settings(chat_id)

    if settings["mode"] == "ai" and OPENROUTER_API_KEY:
        ai_response = await ai_generate(
            trigger_word, classic_response, settings["temperature"]
        )
        if ai_response:
            await update.message.reply_text(ai_response)
            return
        # Fallback на классику если AI не ответил

    await update.message.reply_text(classic_response)


# ─── Запуск ──────────────────────────────────────────────────────────────────


def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА":
        print("⚠️  Вставь токен бота в переменную BOT_TOKEN!")
        print("   Получить можно у @BotFather в Telegram.")
        return

    get_db().close()
    logger.info(f"База данных: {DB_PATH}")
    logger.info(f"Модель: {OPENROUTER_MODEL}")
    logger.info(f"OpenRouter: {'✅ ключ задан' if OPENROUTER_API_KEY else '❌ ключ не задан'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("add1", cmd_add1))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("temp", cmd_temp))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()