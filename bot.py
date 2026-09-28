# -*- coding: utf-8 -*-
"""
Развлекательный бот: игры, квизы, карта дня и сарказм.

Игры: «5 букв», квиз с вариантами, данетки, загадки-эмодзи, «слова из слова».
Плюс: карта дня, титул дня, рулетка и ехидные комментарии к происходящему.

Запуск: токен в token.txt рядом с файлом либо переменная BOT_TOKEN.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ---------------------------------------------------------------- настройки

MAX_ATTEMPTS = 8           # попыток на слово в «5 букв»
NO_DOUBLE_TURN = True      # нельзя ходить два раза подряд
STRICT_DICTIONARY = True   # принимать только слова из словаря
MORNING_HOUR = 11          # час ежедневного поста (по Москве)
SARCASM_CHANCE = 0.08      # как часто бот вставляет шпильку
SARCASM_COOLDOWN_MIN = 30  # не чаще раза в N минут
WORDS_ROUND_SEC = 180      # длительность раунда «слова из слова»

# Бот отвечает только в этих чатах (id через запятую в переменной ALLOWED_CHATS).
# Пока список пуст, бот работает везде — узнайте id командой /chatid и заполните переменную.
ALLOWED_CHATS = {int(x) for x in os.getenv("ALLOWED_CHATS", "").replace(" ", "").split(",")
                 if x.lstrip("-").isdigit()}

MSK = timezone(timedelta(hours=3))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("igra-bot")


def now_msk() -> datetime:
    return datetime.now(MSK)

# ---------------------------------------------------------------- данные


def load_words(name: str) -> list[str]:
    words = (BASE_DIR / name).read_text(encoding="utf-8").split()
    return [w.strip().lower().replace("ё", "е") for w in words if len(w.strip()) == 5]


SALT = b"oushen-aeroport-2026"


def _fernet(password: str):
    """Ключ шифрования из пароля."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode())))


def load_json(name: str, default):
    """Читает файл. Если рядом лежит зашифрованный .enc — расшифровывает ключом DATA_KEY."""
    path = BASE_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Не удалось прочитать %s: %s", name, e)
            return default

    enc = BASE_DIR / (name + ".enc")
    key = os.getenv("DATA_KEY", "").strip()
    if enc.exists():
        if not key:
            log.warning("Есть %s.enc, но не задан DATA_KEY — данные не расшифровать", name)
            return default
        try:
            return json.loads(_fernet(key).decrypt(enc.read_bytes()).decode("utf-8"))
        except Exception as e:
            log.warning("Не удалось расшифровать %s.enc: %s (неверный DATA_KEY?)", name, e)
            return default

    log.warning("Файл %s не найден — часть функций будет отключена", name)
    return default


SECRET_WORDS = load_words("words_secret.txt")
ALLOWED_WORDS = set(load_words("words_all.txt")) | set(SECRET_WORDS)
TAROT = load_json("tarot.json", [])
CONTENT = load_json("content.json", {})
QUIZ = CONTENT.get("quiz", [])
SITUATIONS = CONTENT.get("situations", [])
EMOJI = CONTENT.get("emoji", [])
SARCASM = CONTENT.get("sarcasm", [])
TITLES = CONTENT.get("titles", [])
NOUNS = set(w.strip() for w in (BASE_DIR / "words_nouns.txt").read_text(encoding="utf-8").split()
            if len(w.strip()) >= 3) if (BASE_DIR / "words_nouns.txt").exists() else set()
LONG_WORDS = [w.strip() for w in (BASE_DIR / "longwords.txt").read_text(encoding="utf-8").split()
              if w.strip()] if (BASE_DIR / "longwords.txt").exists() else []

WORD_RE = re.compile(r"^[а-яё]{5}$", re.IGNORECASE)
ALPHABET = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
RANK = {"⬛": 1, "🟨": 2, "🟩": 3}

# ---------------------------------------------------------------- хранилище

DEFAULT_DATA = {
    "scores": {},         # «5 букв»
    "quiz_scores": {},    # квиз
    "words_scores": {},   # слова из слова
    "emoji_scores": {},   # загадки-эмодзи
    "sarcasm_off": [],    # чаты, где сарказм отключён
    "sarcasm_at": {},     # когда бот последний раз шутил
    "morning_chats": [],  # чаты с утренним ритуалом
    "users": {},          # user_id -> имя (для личных сообщений о ДР)
    "last_morning": "",
    "last_birthday": "",
}

data: dict = dict(DEFAULT_DATA)


def load_data() -> None:
    global data
    stored = load_json("data.json", {})
    data = {**DEFAULT_DATA, **stored}


def save_data() -> None:
    try:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("Не удалось сохранить данные: %s", e)


def add_points(table: str, chat_id: int, user_id: int, name: str, points: int) -> int:
    chat = data[table].setdefault(str(chat_id), {})
    rec = chat.setdefault(str(user_id), {"name": name, "points": 0, "wins": 0})
    rec["name"] = name
    rec["points"] += points
    rec["wins"] = rec.get("wins", 0) + 1
    save_data()
    return rec["points"]


def scoreboard(table: str, chat_id: int, title: str) -> str:
    chat = data[table].get(str(chat_id), {})
    if not chat:
        return "Счёт пока пустой."
    rows = sorted(chat.values(), key=lambda r: -r["points"])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>{title}</b>\n"]
    for i, r in enumerate(rows):
        mark = medals[i] if i < len(medals) else "▫️"
        lines.append(f"{mark} {r['name']} — <b>{r['points']}</b>")
    return "\n".join(lines)


def remember_user(message: Message) -> None:
    u = message.from_user
    if not u or u.is_bot:
        return
    name = u.first_name or u.username or "Игрок"
    if data["users"].get(str(u.id)) != name:
        data["users"][str(u.id)] = name
        save_data()


def display_name(message: Message) -> str:
    u = message.from_user
    return u.first_name or u.username or "Игрок"

# ---------------------------------------------------------------- «5 букв»


@dataclass
class Game:
    secret: str
    guesses: list[tuple[str, str, str]] = field(default_factory=list)
    last_player: int | None = None
    finished: bool = False


games: dict[int, Game] = {}


def compare(guess: str, secret: str) -> str:
    result = ["⬛"] * 5
    rest: dict[str, int] = {}
    for i, ch in enumerate(guess):
        if ch == secret[i]:
            result[i] = "🟩"
        else:
            rest[secret[i]] = rest.get(secret[i], 0) + 1
    for i, ch in enumerate(guess):
        if result[i] == "🟩":
            continue
        if rest.get(ch, 0) > 0:
            result[i] = "🟨"
            rest[ch] -= 1
    return "".join(result)


def render_board(game: Game) -> str:
    lines = [f"{p}  <b>{w.upper()}</b>  <i>{n}</i>" for w, p, n in game.guesses]
    lines.append(f"\nОсталось попыток: <b>{MAX_ATTEMPTS - len(game.guesses)}</b>")
    return "\n".join(lines)


def render_letters(game: Game) -> str:
    status: dict[str, str] = {}
    for word, pattern, _ in game.guesses:
        for ch, mark in zip(word, pattern):
            if RANK[mark] > RANK.get(status.get(ch, ""), 0):
                status[ch] = mark
    rows = []
    for i in range(0, len(ALPHABET), 8):
        rows.append(" ".join(f"{status.get(c, '⬜')}{c.upper()}" for c in ALPHABET[i:i + 8]))
    return "\n".join(rows)


def start_game(chat_id: int) -> Game:
    game = Game(secret=random.choice(SECRET_WORDS))
    games[chat_id] = game
    log.info("Новая игра в чате %s: %s", chat_id, game.secret)
    return game

# ---------------------------------------------------------------- тексты


def tarot_card() -> str:
    if not TAROT:
        return ""
    day = now_msk().strftime("%Y-%m-%d")
    card = random.Random(day).choice(TAROT)
    return f"🃏 <b>Карта дня:</b> {card['n']}\n<i>{card['s']}</i>"


MONTHS = ("января февраля марта апреля мая июня июля августа сентября "
          "октября ноября декабря").split()


# ---------------------------------------------------------------- хэндлеры

router = Router()


# ---------------------------------------------------------------- доступ


async def access_guard(handler, event, ctx):
    """Пускает только свои чаты: чужой человек не увидит наш архив."""
    message = event if isinstance(event, Message) else event.message
    chat = message.chat
    user = event.from_user

    text = (message.text or "") if isinstance(event, Message) else ""
    if text.startswith("/chatid"):
        return await handler(event, ctx)

    if not ALLOWED_CHATS:
        return await handler(event, ctx)

    if chat.type in ("group", "supergroup"):
        if chat.id not in ALLOWED_CHATS:
            log.warning("Чужой чат %s (%s) — игнорирую", chat.id, chat.title)
            if isinstance(event, Message):
                await message.answer("Этот бот сделан для одной конкретной компании 🙂")
            return None
    else:
        if str(user.id) not in data["users"]:
            log.warning("Чужая личка от %s (%s) — игнорирую", user.id, user.full_name)
            if isinstance(event, Message):
                await message.answer("Этот бот сделан для одной конкретной компании 🙂")
            return None

    return await handler(event, ctx)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    await message.answer(
        f"id этого чата: <code>{message.chat.id}</code>\n\n"
        f"Впишите его в переменную окружения ALLOWED_CHATS — "
        f"тогда бот будет работать только здесь."
    )


HELP = (
    "🎲 <b>Чем займёмся</b>\n\n"
    "<b>Игры</b>\n"
    "/game — «5 букв»: угадать слово по подсветке букв\n"
    "/квиз — вопрос с вариантами, кто первый\n"
    "/эмодзи — угадать, что зашифровано значками\n"
    "/слова — составить слова из одного длинного за 3 минуты\n"
    "/данетка — загадка-ситуация, разгадка по кнопке\n\n"
    "<b>Развлечения</b>\n"
    "/таро — карта дня\n"
    "/титул — кто сегодня отличился\n"
    "/рулетка идёт за дровами — выбрать жертву случайным образом\n\n"
    "<b>Прочее</b>\n"
    "/топ — все таблицы · /board — поле «5 букв» · /сдаюсь\n"
    "/сарказм выкл — если мои комментарии утомили\n"
    "/morning_on — карта и титул дня каждое утро"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    remember_user(message)
    await message.answer(HELP)


@router.message(Command("help"))
async def cmd_help(message: Message):
    remember_user(message)
    await message.answer(HELP)


@router.message(Command("game", "new", "играть"))
async def cmd_game(message: Message):
    remember_user(message)
    game = games.get(message.chat.id)
    if game and not game.finished:
        await message.answer("Игра уже идёт 🙂\n\n" + render_board(game))
        return
    start_game(message.chat.id)
    await message.answer(
        f"Слово загадано! Пять букв, существительное.\n"
        f"Попыток у команды: <b>{MAX_ATTEMPTS}</b>. Пишите варианты 👇"
    )


@router.message(Command("board"))
async def cmd_board(message: Message):
    game = games.get(message.chat.id)
    if not game or game.finished:
        await message.answer("Сейчас игра не идёт. /game — начать.")
    elif not game.guesses:
        await message.answer("Поле пустое. Пишите слово из пяти букв.")
    else:
        await message.answer(render_board(game) + "\n\n" + render_letters(game))


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    game = games.get(message.chat.id)
    if not game or game.finished:
        await message.answer("Сейчас игра не идёт. /game — начать новую.")
        return
    game.finished = True
    await message.answer(f"Было загадано слово: <b>{game.secret.upper()}</b>\n\n/game — ещё раз")


@router.message(Command("score"))
async def cmd_score(message: Message):
    await message.answer(scoreboard("scores", message.chat.id, "🏆 Пять букв"))


@router.message(Command("taro", "таро"))
async def cmd_taro(message: Message):
    await message.answer(tarot_card() or "Колода не загружена.")



# ---------------------------------------------------------------- квиз


@dataclass
class QuizRound:
    idx: int
    answered: set = field(default_factory=set)
    finished: bool = False


quiz_rounds: dict[int, QuizRound] = {}


@router.message(Command("квиз", "quiz", "викторина"))
async def cmd_quiz(message: Message):
    remember_user(message)
    if not QUIZ:
        await message.answer("Вопросы не загружены.")
        return
    idx = random.randrange(len(QUIZ))
    q = QUIZ[idx]
    order = list(range(len(q["o"])))
    random.shuffle(order)
    rows = [[InlineKeyboardButton(text=q["o"][i], callback_data=f"q:{idx}:{i}")] for i in order]
    sent = await message.answer(
        f"🧠 <b>Вопрос</b>\n\n{q['q']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    quiz_rounds[sent.message_id] = QuizRound(idx=idx)


@router.callback_query(F.data.startswith("q:"))
async def on_quiz(call: CallbackQuery):
    rnd = quiz_rounds.get(call.message.message_id)
    if not rnd:
        await call.answer("Этот вопрос уже в прошлом")
        return
    if rnd.finished:
        await call.answer("Уже ответили")
        return
    if call.from_user.id in rnd.answered:
        await call.answer("Вы уже отвечали")
        return

    _, idx, choice = call.data.split(":")
    q = QUIZ[int(idx)]
    rnd.answered.add(call.from_user.id)
    name = call.from_user.first_name or "Игрок"

    if int(choice) != q["a"]:
        await call.answer("Мимо")
        return

    rnd.finished = True
    total = add_points("quiz_scores", call.message.chat.id, call.from_user.id, name, 1)
    await call.message.edit_text(
        f"🧠 <b>Вопрос</b>\n\n{q['q']}\n\n"
        f"✅ <b>{q['o'][q['a']]}</b>\n<i>{q['why']}</i>\n\n"
        f"Первым ответил(а) <b>{name}</b> — очков: {total}\n\n/квиз — ещё"
    )
    await call.answer("Верно!")


@router.message(Command("квизтоп", "quiztop"))
async def cmd_quiztop(message: Message):
    await message.answer(scoreboard("quiz_scores", message.chat.id, "🧠 Знатоки"))


# ---------------------------------------------------------------- данетки


@router.message(Command("данетка", "загадка"))
async def cmd_situation(message: Message):
    if not SITUATIONS:
        await message.answer("Данетки не загружены.")
        return
    idx = random.randrange(len(SITUATIONS))
    await message.answer(
        f"🕵️ <b>Данетка</b>\n\n{SITUATIONS[idx]['q']}\n\n"
        f"<i>Задавайте вопросы, на которые можно ответить «да» или «нет».</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔍 Показать ответ", callback_data=f"s:{idx}")]]),
    )


@router.callback_query(F.data.startswith("s:"))
async def on_situation(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    await call.message.edit_text(
        f"🕵️ <b>Данетка</b>\n\n{SITUATIONS[idx]['q']}\n\n"
        f"💡 <b>Разгадка:</b> {SITUATIONS[idx]['a']}\n\n/данетка — ещё"
    )
    await call.answer()


# ---------------------------------------------------------------- эмодзи


@dataclass
class EmojiRound:
    answer: str
    tries: int = 0


emoji_rounds: dict[int, EmojiRound] = {}


@router.message(Command("эмодзи", "emoji"))
async def cmd_emoji(message: Message):
    remember_user(message)
    if not EMOJI:
        await message.answer("Загадки не загружены.")
        return
    item = random.choice(EMOJI)
    emoji_rounds[message.chat.id] = EmojiRound(answer=item["a"])
    await message.answer(
        f"🎬 <b>Что зашифровано?</b>\n\n<b>{item['e']}</b>\n\n"
        f"<i>Пишите ответ прямо в чат. Сдаться: /сдаюсь</i>"
    )


@router.message(Command("сдаюсь", "сдаться"))
async def cmd_give_up(message: Message):
    rnd = emoji_rounds.pop(message.chat.id, None)
    if rnd:
        await message.answer(f"Это <b>{rnd.answer}</b>. /эмодзи — ещё")
        return
    game = games.get(message.chat.id)
    if game and not game.finished:
        game.finished = True
        await message.answer(f"Слово было: <b>{game.secret.upper()}</b>\n\n/game — ещё раз")
        return
    await message.answer("Сейчас ничего не загадано.")


# ---------------------------------------------------------------- слова из слова


@dataclass
class WordsRound:
    base: str
    found: dict = field(default_factory=dict)   # слово -> имя
    until: str = ""


words_rounds: dict[int, WordsRound] = {}


def can_make(word: str, base: str) -> bool:
    letters = list(base)
    for ch in word:
        if ch in letters:
            letters.remove(ch)
        else:
            return False
    return True


@router.message(Command("слова", "words"))
async def cmd_words(message: Message):
    remember_user(message)
    if not LONG_WORDS or not NOUNS:
        await message.answer("Словарь не загружен.")
        return
    if message.chat.id in words_rounds:
        await message.answer("Раунд уже идёт! Пишите слова.")
        return

    base = random.choice(LONG_WORDS)
    words_rounds[message.chat.id] = WordsRound(base=base)
    await message.answer(
        f"🔤 <b>Слова из слова</b>\n\nСоставляйте слова из букв слова "
        f"<b>{base.upper()}</b>\n\nСуществительные, от трёх букв, каждая буква — "
        f"сколько раз она есть в слове. У вас {WORDS_ROUND_SEC // 60} минуты!"
    )

    async def finish():
        await asyncio.sleep(WORDS_ROUND_SEC)
        rnd = words_rounds.pop(message.chat.id, None)
        if not rnd:
            return
        if not rnd.found:
            await message.answer(f"Время вышло. Ни одного слова из «{base.upper()}» 🤷")
            return
        by_player: dict[str, list[str]] = {}
        for word, who in rnd.found.items():
            by_player.setdefault(who, []).append(word)
        lines = [f"⏰ <b>Время вышло!</b> Слов собрано: {len(rnd.found)}\n"]
        for who, words in sorted(by_player.items(), key=lambda x: -len(x[1])):
            lines.append(f"<b>{who}</b> — {len(words)}: {', '.join(sorted(words))}")
        await message.answer("\n".join(lines) + "\n\n/слова — ещё раунд")

    asyncio.create_task(finish())


# ---------------------------------------------------------------- карта, титул, рулетка


@router.message(Command("таро", "карта"))
async def cmd_taro(message: Message):
    await message.answer(tarot_card() or "Колода не загружена.")


@router.message(Command("титул"))
async def cmd_title(message: Message):
    people = list(data["users"].values())
    if not people or not TITLES:
        await message.answer("Мне пока некого награждать — напишите что-нибудь в чат.")
        return
    day = now_msk().strftime("%Y-%m-%d") + str(message.chat.id)
    rnd = random.Random(day)
    await message.answer(
        f"🏅 <b>Титул дня</b>\n\n{rnd.choice(people)} — <b>{rnd.choice(TITLES)}</b>"
    )


@router.message(Command("рулетка"))
async def cmd_roulette(message: Message):
    people = list(data["users"].values())
    if len(people) < 2:
        await message.answer("Слишком мало участников. Пусть все что-нибудь напишут.")
        return
    parts = (message.text or "").split(maxsplit=1)
    task = parts[1].strip() if len(parts) > 1 else "выпала честь"
    await message.answer(f"🎯 <b>{random.choice(people)}</b> — {task}")


@router.message(Command("топ"))
async def cmd_top(message: Message):
    chat = str(message.chat.id)
    blocks = []
    for table, title in (("scores", "🏆 Пять букв"), ("quiz_scores", "🧠 Квиз"),
                         ("words_scores", "🔤 Слова"), ("emoji_scores", "🎬 Эмодзи")):
        if data[table].get(chat):
            blocks.append(scoreboard(table, message.chat.id, title))
    await message.answer("\n\n".join(blocks) if blocks else "Ещё никто не играл. /игры — что есть")


@router.message(Command("игры"))
async def cmd_games_list(message: Message):
    await message.answer(HELP)


@router.message(Command("сарказм"))
async def cmd_sarcasm(message: Message):
    chat = message.chat.id
    arg = (message.text or "").split()[-1].lower()
    off = data["sarcasm_off"]
    if arg in ("выкл", "off"):
        if chat not in off:
            off.append(chat)
        save_data()
        await message.answer("Молчу. Хотя мог бы многое сказать.")
    elif arg in ("вкл", "on"):
        if chat in off:
            off.remove(chat)
        save_data()
        await message.answer("Возвращаюсь к своим обязанностям.")
    else:
        await message.answer(
            f"Сарказм {'выключен' if chat in off else 'включён'}. "
            f"<code>/сарказм выкл</code> · <code>/сарказм вкл</code>"
        )


@router.message(Command("morning_on"))
async def cmd_morning_on(message: Message):
    if message.chat.id not in data["morning_chats"]:
        data["morning_chats"].append(message.chat.id)
        save_data()
    await message.answer(
        f"Готово. Каждое утро в {MORNING_HOUR}:00 по Москве — карта дня, "
        f"что было год назад и свежее слово.\n/morning_off — отключить."
    )


@router.message(Command("morning_off"))
async def cmd_morning_off(message: Message):
    if message.chat.id in data["morning_chats"]:
        data["morning_chats"].remove(message.chat.id)
        save_data()
    await message.answer("Утренний ритуал отключён.")


def sarcasm_line(chat_id: int) -> str | None:
    """Изредка бот вставляет свои пять копеек."""
    if chat_id in data["sarcasm_off"] or not SARCASM:
        return None
    last = data["sarcasm_at"].get(str(chat_id))
    if last:
        gap = now_msk() - datetime.strptime(last, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        if gap < timedelta(minutes=SARCASM_COOLDOWN_MIN):
            return None
    if random.random() >= SARCASM_CHANCE:
        return None
    data["sarcasm_at"][str(chat_id)] = now_msk().strftime("%Y-%m-%d %H:%M")
    return random.choice(SARCASM)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    """Ответы в активных играх, попытки в «5 букв» и редкие шпильки."""
    remember_user(message)
    text = (message.text or "").strip()
    chat_id = message.chat.id
    name = display_name(message)
    low = text.lower()

    # 1. загадка-эмодзи
    rnd = emoji_rounds.get(chat_id)
    if rnd:
        rnd.tries += 1
        target = rnd.answer.lower()
        key = target.split()[-1] if len(target.split()) > 1 else target
        if low == target or (len(low) > 3 and (low in target or key in low)):
            emoji_rounds.pop(chat_id, None)
            total = add_points("emoji_scores", chat_id, message.from_user.id, name, 1)
            await message.reply(
                f"🎯 Верно — <b>{rnd.answer}</b>!\n{name}, очков: {total}\n\n/эмодзи — ещё"
            )
            return
        if rnd.tries % 8 == 0:
            await message.reply(f"Подсказка: в ответе {len(rnd.answer.split())} слово(а), "
                                f"первая буква — <b>{rnd.answer[0].upper()}</b>")
            return

    # 2. слова из слова
    wr = words_rounds.get(chat_id)
    if wr and re.fullmatch(r"[а-яё]{3,}", low):
        word = low.replace("ё", "е")
        if word == wr.base:
            await message.reply("Само загаданное слово не считается 🙂")
            return
        if word in wr.found:
            await message.reply(f"«{word}» уже нашёл(ла) {wr.found[word]}")
            return
        if not can_make(word, wr.base):
            await message.reply("Из этих букв такое не собрать.")
            return
        if word not in NOUNS:
            await message.reply("Нет в словаре. Нужно существительное в единственном числе.")
            return
        wr.found[word] = name
        add_points("words_scores", chat_id, message.from_user.id, name, 1)
        await message.reply(f"✅ {word} — засчитано. Всего слов: {len(wr.found)}")
        return

    # 3. «5 букв»
    game = games.get(chat_id)
    if game and not game.finished and WORD_RE.fullmatch(text):
        word = low.replace("ё", "е")
        if any(g[0] == word for g in game.guesses):
            await message.reply("Это слово уже называли 🙂")
            return
        if STRICT_DICTIONARY and word not in ALLOWED_WORDS:
            await message.reply("Такого слова нет в словаре.")
            return
        if NO_DOUBLE_TURN and game.last_player == message.from_user.id and game.guesses:
            await message.reply("Пусть кто-нибудь другой попробует 🙂")
            return

        pattern = compare(word, game.secret)
        game.guesses.append((word, pattern, name))
        game.last_player = message.from_user.id

        if word == game.secret:
            game.finished = True
            points = max(1, MAX_ATTEMPTS - len(game.guesses) + 1)
            total = add_points("scores", chat_id, message.from_user.id, name, points)
            await message.answer(
                f"{pattern}  <b>{word.upper()}</b>\n\n"
                f"🎉 <b>{name}</b> угадал(а) с {len(game.guesses)}-й попытки!\n"
                f"+{points} очков, всего: {total}\n\n/game — ещё · /топ — таблицы"
            )
            return
        if len(game.guesses) >= MAX_ATTEMPTS:
            game.finished = True
            await message.answer(render_board(game) +
                                 f"\n\n😔 Попытки кончились. Слово было: <b>{game.secret.upper()}</b>")
            return
        await message.answer(render_board(game) + "\n\n" + render_letters(game))
        return

    # 4. шпилька
    line = sarcasm_line(chat_id)
    if line:
        save_data()
        await message.reply(line)


# ---------------------------------------------------------------- расписание


async def post_morning(bot: Bot) -> None:
    for chat_id in list(data["morning_chats"]):
        try:
            people = list(data["users"].values())
            rnd = random.Random(now_msk().strftime("%Y-%m-%d") + str(chat_id))
            parts = ["☀️ <b>Доброе утро!</b>", tarot_card()]
            if people and TITLES:
                parts.append(f"🏅 <b>Титул дня:</b> {rnd.choice(people)} — {rnd.choice(TITLES)}")
            parts.append("Чем займёмся: /game · /квиз · /эмодзи · /слова · /данетка")
            await bot.send_message(chat_id, "\n\n".join(p for p in parts if p))
        except Exception as e:
            log.warning("Утренний пост в чат %s не ушёл: %s", chat_id, e)


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            now = now_msk()
            today = now.strftime("%Y-%m-%d")
            if now.hour == MORNING_HOUR:
                if data.get("last_morning") != today:
                    data["last_morning"] = today
                    save_data()
                    await post_morning(bot)
        except Exception as e:
            log.warning("Ошибка в расписании: %s", e)
        await asyncio.sleep(60)

# ---------------------------------------------------------------- запуск


def get_proxy() -> str | None:
    proxy = os.getenv("TG_PROXY", "").strip()
    if not proxy:
        f = BASE_DIR / "proxy.txt"
        if f.exists():
            proxy = f.read_text(encoding="utf-8").strip()
    return proxy or None


def get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        f = BASE_DIR / "token.txt"
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(
            "Не найден токен бота.\n"
            "Положите его в файл token.txt рядом с bot.py "
            "или задайте переменную окружения BOT_TOKEN."
        )
    return token


async def on_error(event: ErrorEvent) -> None:
    if isinstance(event.exception, TelegramNetworkError):
        log.error("Нет связи с Telegram — сообщение не доставлено.")
    else:
        log.exception("Ошибка при обработке: %s", event.exception)


async def main() -> None:
    load_data()
    proxy = get_proxy()
    session = AiohttpSession(proxy=proxy) if proxy else None
    if proxy:
        log.info("Используется прокси: %s", proxy)

    bot = Bot(token=get_token(), session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    router.message.outer_middleware(access_guard)
    router.callback_query.outer_middleware(access_guard)
    dp.include_router(router)
    dp.errors.register(on_error)

    log.info("Слов: %s · цитат: %s · дней в архиве: %s · карт Таро: %s",
             len(SECRET_WORDS), len(QUOTES), len(ARCHIVE), len(TAROT))
    log.info("Разрешённые чаты: %s", ALLOWED_CHATS or "(пока все — задайте ALLOWED_CHATS)")
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
