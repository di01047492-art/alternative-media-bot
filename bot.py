import os
import sqlite3
import asyncio
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_MEDIA_ROLE_ID = os.getenv("DISCORD_MEDIA_ROLE_ID", "").strip()

DB_PATH = os.getenv("DB_PATH", "alternative.db")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        discord_id TEXT,
        role TEXT DEFAULT 'User',
        balance REAL DEFAULT 0,
        referral_code TEXT UNIQUE,
        created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        amount REAL,
        method TEXT,
        details TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    con.commit()
    con.close()


def ensure_user(message: Message):
    u = message.from_user
    if not u:
        return
    con = db()
    cur = con.cursor()
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (u.id,))
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO users
            (telegram_id, username, first_name, referral_code, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (u.id, u.username or "", u.first_name or "",
             f"ALT{u.id}", datetime.utcnow().isoformat())
        )
    else:
        cur.execute(
            "UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
            (u.username or "", u.first_name or "", u.id)
        )
    con.commit()
    con.close()


def get_user(tg_id):
    con = db()
    row = con.execute(
        "SELECT * FROM users WHERE telegram_id=?", (tg_id,)
    ).fetchone()
    con.close()
    return row


def set_discord(tg_id, discord_id):
    con = db()
    con.execute(
        "UPDATE users SET discord_id=? WHERE telegram_id=?",
        (discord_id, tg_id)
    )
    con.commit()
    con.close()


def set_role(tg_id, role):
    con = db()
    con.execute(
        "UPDATE users SET role=? WHERE telegram_id=?",
        (role, tg_id)
    )
    con.commit()
    con.close()


def add_balance(tg_id, amount):
    con = db()
    con.execute(
        "UPDATE users SET balance=balance+? WHERE telegram_id=?",
        (amount, tg_id)
    )
    con.commit()
    con.close()


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="💼 Кабинет", callback_data="cabinet"),
        ],
        [
            InlineKeyboardButton(text="🎬 Media", callback_data="media"),
            InlineKeyboardButton(text="💰 Вывод средств", callback_data="withdraw"),
        ],
        [
            InlineKeyboardButton(text="🎁 Промокод", callback_data="promo"),
            InlineKeyboardButton(text="👥 Реферальная программа", callback_data="referrals"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Информация", callback_data="info"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        ],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


def media_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Подать заявку на Media", callback_data="media_apply")],
        [InlineKeyboardButton(text="🔗 Привязать Discord", callback_data="discord_link")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


def withdraw_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Создать заявку на вывод", callback_data="withdraw_create")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])


class Form(StatesGroup):
    discord = State()
    media_tiktok = State()
    promo = State()
    withdraw_amount = State()
    withdraw_method = State()
    withdraw_details = State()
    support = State()


async def check_discord_media_role(discord_id):
    if not (DISCORD_BOT_TOKEN and DISCORD_GUILD_ID and DISCORD_MEDIA_ROLE_ID):
        return False, "not_configured"

    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    return False, "not_found"
                if resp.status != 200:
                    return False, f"http_{resp.status}"
                data = await resp.json()
                roles = {str(x) for x in data.get("roles", [])}
                return DISCORD_MEDIA_ROLE_ID in roles, "ok"
    except Exception:
        return False, "error"


async def send_home(message):
    await message.answer(
        "🟣 <b>Alternative Media Bot</b>\n\n"
        "Добро пожаловать в кабинет Alternative.\n"
        "Выбери нужный раздел:",
        reply_markup=main_kb()
    )


async def edit_callback(callback, text, kb):
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message)
    await send_home(message)


@dp.message(Command("menu"))
async def menu(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message)
    await send_home(message)


@dp.message(Command("reply"))
async def admin_reply(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Использование: <code>/reply TELEGRAM_ID текст</code>")
        return
    try:
        await bot.send_message(
            int(parts[1]),
            f"🆘 <b>Ответ поддержки</b>\n\n{parts[2]}"
        )
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>")


@dp.message(Command("addbalance"))
async def admin_addbalance(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Использование: <code>/addbalance TELEGRAM_ID СУММА</code>")
        return
    try:
        amount = float(parts[2].replace(",", "."))
        add_balance(int(parts[1]), amount)
        await message.answer(f"✅ Баланс пополнен на <b>{amount:.2f}</b>.")
    except ValueError:
        await message.answer("❌ Неверная сумма.")


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await edit_callback(
        callback,
        "🟣 <b>Alternative Media Bot</b>\n\nВыбери нужный раздел:",
        main_kb()
    )


@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    row = get_user(callback.from_user.id)
    balance = row[5] if row else 0
    discord = row[3] if row and row[3] else "не привязан"
    role = row[4] if row else "User"
    await edit_callback(
        callback,
        "👤 <b>ПРОФИЛЬ</b>\n\n"
        f"🆔 Telegram ID: <code>{callback.from_user.id}</code>\n"
        f"👤 Username: @{callback.from_user.username or 'нет'}\n"
        f"🎭 Роль: <b>{role}</b>\n"
        f"💰 Баланс: <b>{balance:.2f}</b>\n"
        f"🔗 Discord: <b>{discord}</b>",
        back_kb()
    )


@dp.callback_query(F.data == "cabinet")
async def cabinet(callback: CallbackQuery):
    row = get_user(callback.from_user.id)
    balance = row[5] if row else 0
    role = row[4] if row else "User"
    await edit_callback(
        callback,
        "💼 <b>КАБИНЕТ</b>\n\n"
        f"🎭 Статус: <b>{role}</b>\n"
        f"💰 Баланс: <b>{balance:.2f}</b>\n\n"
        "Здесь будет основная информация по работе с Alternative.",
        back_kb()
    )


@dp.callback_query(F.data == "info")
async def info(callback: CallbackQuery):
    await edit_callback(
        callback,
        "ℹ️ <b>ИНФОРМАЦИЯ</b>\n\n"
        "🎬 Media — статус после проверки.\n"
        "💎 Media+ — расширенный статус.\n"
        "💰 Оплата за принятые видео зачисляется на баланс.\n"
        "💳 Вывод выполняется через заявку.",
        back_kb()
    )


@dp.callback_query(F.data == "media")
async def media(callback: CallbackQuery):
    row = get_user(callback.from_user.id)
    role = row[4] if row else "User"
    await edit_callback(
        callback,
        "🎬 <b>MEDIA</b>\n\n"
        f"Текущий статус: <b>{role}</b>\n\n"
        "Для заявки понадобится ссылка на TikTok и привязанный Discord.",
        media_kb()
    )


@dp.callback_query(F.data == "discord_link")
async def discord_link(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.discord)
    await callback.message.answer(
        "🔗 <b>Привязка Discord</b>\n\n"
        "Отправь числовой <b>Discord User ID</b>.\n"
        "После этого бот проверит роль Media на сервере."
    )
    await callback.answer()


@dp.message(Form.discord)
async def process_discord(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("❌ Нужен числовой Discord User ID.")
        return

    ok, status = await check_discord_media_role(value)

    if status == "not_configured":
        set_discord(message.from_user.id, value)
        await state.clear()
        await message.answer(
            "⚠️ Discord сохранён, но автоматическая проверка роли пока не настроена.",
            reply_markup=main_kb()
        )
        return

    if status == "not_found":
        await message.answer("❌ Пользователь с таким Discord ID не найден на сервере.")
        return

    if not ok:
        await message.answer(
            "❌ Роль <b>Media</b> не найдена. Проверь, что ты на сервере и роль выдана."
        )
        return

    set_discord(message.from_user.id, value)
    set_role(message.from_user.id, "Media")
    await state.clear()
    await message.answer(
        "✅ <b>Discord привязан!</b>\n\nРоль <b>Media</b> подтверждена.",
        reply_markup=main_kb()
    )


@dp.callback_query(F.data == "media_apply")
async def media_apply(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.media_tiktok)
    await callback.message.answer(
        "🎬 <b>Заявка на Media</b>\n\n"
        "Отправь ссылку на свой TikTok-профиль."
    )
    await callback.answer()


@dp.message(Form.media_tiktok)
async def process_media(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if "tiktok.com" not in link.lower():
        await message.answer("❌ Пришли корректную ссылку на TikTok.")
        return

    row = get_user(message.from_user.id)
    discord = row[3] if row else None
    if not discord:
        await state.clear()
        await message.answer(
            "⚠️ Сначала привяжи Discord.",
            reply_markup=media_kb()
        )
        return

    await state.clear()
    await message.answer(
        "📨 <b>Заявка отправлена.</b>\n\n"
        f"🔗 TikTok: {link}\n"
        f"🎮 Discord ID: <code>{discord}</code>\n\n"
        "Заявка передана на проверку.",
        reply_markup=main_kb()
    )

    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            "🎬 <b>Новая заявка Media</b>\n\n"
            f"👤 TG: <code>{message.from_user.id}</code>\n"
            f"🔗 TikTok: {link}\n"
            f"🎮 Discord: <code>{discord}</code>"
        )


@dp.callback_query(F.data == "withdraw")
async def withdraw(callback: CallbackQuery):
    row = get_user(callback.from_user.id)
    balance = row[5] if row else 0
    await edit_callback(
        callback,
        "💰 <b>ВЫВОД СРЕДСТВ</b>\n\n"
        f"Доступно: <b>{balance:.2f}</b>\n\n"
        "Создай заявку на вывод средств.",
        withdraw_kb()
    )


@dp.callback_query(F.data == "withdraw_create")
async def withdraw_create(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.withdraw_amount)
    await callback.message.answer("💳 Напиши сумму для вывода.")
    await callback.answer()


@dp.message(Form.withdraw_amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи сумму числом.")
        return

    row = get_user(message.from_user.id)
    balance = float(row[5]) if row else 0
    if amount <= 0 or amount > balance:
        await message.answer("❌ Недопустимая сумма или недостаточно средств.")
        return

    await state.update_data(amount=amount)
    await state.set_state(Form.withdraw_method)
    await message.answer("💳 Напиши удобный способ оплаты.")


@dp.message(Form.withdraw_method)
async def withdraw_method(message: Message, state: FSMContext):
    await state.update_data(method=(message.text or "").strip())
    await state.set_state(Form.withdraw_details)
    await message.answer("📝 Отправь реквизиты для выплаты.")


@dp.message(Form.withdraw_details)
async def withdraw_details(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = float(data["amount"])
    method = data["method"]
    details = (message.text or "").strip()

    con = db()
    con.execute(
        """INSERT INTO withdrawals
        (telegram_id, amount, method, details, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (message.from_user.id, amount, method, details, datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()

    await state.clear()
    await message.answer(
        "✅ <b>Заявка на вывод создана.</b>\n\n"
        f"💰 Сумма: <b>{amount:.2f}</b>\n"
        f"💳 Метод: <b>{method}</b>\n\n"
        "После проверки администратор свяжется с тобой.",
        reply_markup=main_kb()
    )

    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            "💰 <b>Новая заявка на вывод</b>\n\n"
            f"👤 TG: <code>{message.from_user.id}</code>\n"
            f"💰 Сумма: <b>{amount:.2f}</b>\n"
            f"💳 Метод: {method}\n"
            f"📝 Реквизиты: <code>{details}</code>"
        )


@dp.callback_query(F.data == "promo")
async def promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.promo)
    await callback.message.answer("🎁 <b>ПРОМОКОД</b>\n\nВведи промокод.")
    await callback.answer()


@dp.message(Form.promo)
async def process_promo(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    await state.clear()
    await message.answer(
        f"🎁 Промокод <code>{code}</code> принят на проверку.",
        reply_markup=main_kb()
    )
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            "🎁 <b>Введён промокод</b>\n\n"
            f"👤 TG: <code>{message.from_user.id}</code>\n"
            f"🏷 Код: <code>{code}</code>"
        )


@dp.callback_query(F.data == "referrals")
async def referrals(callback: CallbackQuery):
    row = get_user(callback.from_user.id)
    code = row[6] if row else f"ALT{callback.from_user.id}"
    await edit_callback(
        callback,
        "👥 <b>РЕФЕРАЛЬНАЯ ПРОГРАММА</b>\n\n"
        f"Твой код: <code>{code}</code>\n\n"
        "Реферальную систему можно подключить к нужным условиям начисления.",
        back_kb()
    )


@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.support)
    await callback.message.answer(
        "🆘 <b>ПОДДЕРЖКА</b>\n\n"
        "Напиши сообщение одним сообщением — оно уйдёт администратору."
    )
    await callback.answer()


@dp.message(Form.support)
async def process_support(message: Message, state: FSMContext):
    text = message.text or "(сообщение без текста)"
    await state.clear()
    await message.answer(
        "✅ Сообщение отправлено в поддержку.\n"
        "Ожидай ответа администратора.",
        reply_markup=main_kb()
    )

    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            "🆘 <b>Новое сообщение в поддержку</b>\n\n"
            f"👤 Telegram ID: <code>{message.from_user.id}</code>\n"
            f"👤 Username: @{message.from_user.username or 'нет'}\n\n"
            f"{text}\n\n"
            f"Ответ: <code>/reply {message.from_user.id} ТЕКСТ</code>"
        )


@dp.message()
async def fallback(message: Message):
    ensure_user(message)
    await message.answer("Используй кнопки меню 👇", reply_markup=main_kb())


async def main():
    init_db()
    print("Alternative Media Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
