"""
Telegram-бот для групповых чатов с SQLite хранилищем.
Триггеры настраиваются отдельно для каждой группы через команды.

Установка:
    pip install python-telegram-bot==20.7

Запуск:
    1. Получи токен у @BotFather в Telegram
    2. Вставь токен в переменную BOT_TOKEN ниже (или задай через переменную окружения)
    3. В настройках бота у @BotFather:
       /mybots → выбери бота → Bot Settings → Group Privacy → Turn off
    4. python trigger_bot.py

Команды:
    /add триггер = ответ       — добавить триггер (двусторонний по умолчанию)
    /add1 триггер = ответ      — добавить односторонний триггер
    /del триггер               — удалить триггер
    /list                      — список всех триггеров группы
    /clear                     — удалить ВСЕ триггеры группы (только админы)
    /help                      — справка
"""

import os
import sqlite3
import logging
from pathlib import Path
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
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "triggers.db"))

# Дефолтные пары (добавляются при первом сообщении в группе)
DEFAULT_PAIRS = [
    ("да",       "пизда",                True),
    ("нет",      "пидора ответ",         True),
    ("ясно",     "хуясно",              True),
    ("тоже",     "на говно похоже",      True),
    ("300",      "отсоси у тракториста",  True),
    ("здрасьте", "забор покрасьте",      True),
    ("ладно",    "нахуй иди",           True),
]

# ─── База данных ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
    conn.commit()
    return conn


def ensure_defaults(chat_id: int) -> None:
    """Добавляет дефолтные триггеры при первом контакте с группой."""
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
    """Удаляет триггер и его обратную пару. Возвращает кол-во удалённых."""
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


def find_trigger(chat_id: int, text: str) -> str | None:
    triggers = get_triggers(chat_id)
    text_lower = text.lower().strip()
    words = text_lower.split()

    # Сортируем по длине (сначала многословные)
    triggers_sorted = sorted(triggers, key=lambda t: len(t[0].split()), reverse=True)

    for key, response, _ in triggers_sorted:
        key_words = key.split()
        key_len = len(key_words)
        # Точное совпадение всего сообщения
        if text_lower == key:
            return response
        # Вхождение как подпоследовательность слов
        for i in range(len(words) - key_len + 1):
            if words[i : i + key_len] == key_words:
                return response

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
    text = (
        "🤖 <b>Триггер-бот — справка</b>\n\n"
        "<b>Команды:</b>\n"
        "<code>/add триггер = ответ</code> — добавить (↔️ двусторонний)\n"
        "<code>/add1 триггер = ответ</code> — добавить (➡️ односторонний)\n"
        "<code>/del триггер</code> — удалить триггер\n"
        "<code>/list</code> — список триггеров\n"
        "<code>/clear</code> — удалить всё (только админы)\n\n"
        "↔️ Двусторонний = работает в обе стороны:\n"
        "  <i>да → пизда</i>  и  <i>пизда → да</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


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
        f"✅ Добавлено ({direction}):\n"
        f"<b>{trigger_w}</b> → {response}",
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

    # Группируем двусторонние, чтобы не дублировать
    seen = set()
    lines = []
    for trigger_w, response, bidirect in sorted(triggers, key=lambda t: t[0]):
        pair_key = tuple(sorted([trigger_w, response.lower()]))
        if bidirect and pair_key in seen:
            continue
        seen.add(pair_key)
        arrow = "↔️" if bidirect else "➡️"
        lines.append(f"  {arrow} <b>{trigger_w}</b> → {response}")

    header = f"📋 <b>Триггеры ({len(lines)}):</b>\n"
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

    response = find_trigger(chat_id, update.message.text)
    if response:
        await update.message.reply_text(response)


# ─── Запуск ──────────────────────────────────────────────────────────────────


def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА":
        print("⚠️  Вставь токен бота в переменную BOT_TOKEN!")
        print("   Получить можно у @BotFather в Telegram.")
        return

    get_db().close()
    logger.info(f"База данных: {DB_PATH}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("add1", cmd_add1))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
