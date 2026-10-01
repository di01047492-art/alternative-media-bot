import os
import asyncio
import sqlite3
import hashlib
import secrets
import json
import re
from urllib.parse import urlparse
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
DB_FILE = "accounts.db"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Anti-Nuke
ANTINUKE_WINDOW = 10
ANTINUKE_LIMITS = {
    "channel_delete": 3,
    "role_delete": 3,
    "ban": 5,
    "kick": 5,
}
_action_history = defaultdict(lambda: defaultdict(deque))

async def antinuke_check(guild: discord.Guild, user: discord.Member, action: str):
    if user.id == guild.owner_id or user.guild_permissions.administrator:
        return False
    now = asyncio.get_running_loop().time()
    q = _action_history[user.id][action]
    q.append(now)
    while q and now - q[0] > ANTINUKE_WINDOW:
        q.popleft()
    return len(q) >= ANTINUKE_LIMITS.get(action, 999)

async def antinuke_timeout(guild: discord.Guild, user: discord.Member, action: str):
    try:
        await user.timeout(discord.utils.utcnow() + __import__("datetime").timedelta(hours=1),
                           reason=f"Anti-Nuke: {action}")
    except Exception:
        pass
    try:
        log = discord.utils.get(guild.text_channels, name="anti-nuke-logs")
        if log:
            await log.send(f"🛡️ **Anti-Nuke**: {user.mention} ограничен. Причина: `{action}`.")
    except Exception:
        pass


TICKET_CATEGORIES = {
    "tech": ("🛠️", "Technical Support"),
    "bug": ("🐛", "Bug Report"),
    "suggest": ("💡", "Suggestion"),
    "purchase": ("💳", "Purchase"),
    "partner": ("🤝", "Partnership"),
    "media": ("🎬", "Media"),
    "other": ("❓", "Other"),
}


# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            discord_id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "balance" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN balance REAL NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def hash_password(password: str, salt: bytes | None = None):
    if salt is None:
        salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200_000
    )
    return salt.hex(), password_hash.hex()


def check_password(password: str, salt_hex: str, stored_hash: str):
    salt = bytes.fromhex(salt_hex)
    _, new_hash = hash_password(password, salt)
    return secrets.compare_digest(new_hash, stored_hash)


def get_account(discord_id: int):
    conn = db()
    row = conn.execute(
        "SELECT discord_id, username, password_hash, salt, created_at, balance "
        "FROM accounts WHERE discord_id = ?",
        (discord_id,)
    ).fetchone()
    conn.close()
    return row


def username_taken(username: str):
    conn = db()
    row = conn.execute(
        "SELECT discord_id FROM accounts WHERE username = ?",
        (username,)
    ).fetchone()
    conn.close()
    return row is not None


def create_account(discord_id: int, username: str, password: str):
    salt, password_hash = hash_password(password)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db()
    try:
        conn.execute(
            "INSERT INTO accounts "
            "(discord_id, username, password_hash, salt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (discord_id, username, password_hash, salt, created_at)
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Этот логин уже занят или у тебя уже есть аккаунт."
    finally:
        conn.close()


# ---------------- ACCOUNT MANAGER ----------------

class CreateAccountModal(discord.ui.Modal, title="Создание аккаунта"):
    username = discord.ui.TextInput(
        label="Придумай логин",
        placeholder="Например: Player123",
        min_length=3,
        max_length=24,
        required=True,
    )
    password = discord.ui.TextInput(
        label="Придумай пароль",
        placeholder="Минимум 6 символов",
        min_length=6,
        max_length=128,
        required=True,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = str(self.username.value).strip()
        password = str(self.password.value)

        if not username.replace("_", "").replace("-", "").isalnum():
            return await interaction.response.send_message(
                "❌ Используй только буквы, цифры, `_` или `-`.",
                ephemeral=True
            )

        if username_taken(username):
            return await interaction.response.send_message(
                "❌ Такой логин уже занят. Попробуй другой.",
                ephemeral=True
            )

        if get_account(interaction.user.id):
            return await interaction.response.send_message(
                "❌ У тебя уже есть аккаунт.",
                ephemeral=True
            )

        ok, error = create_account(interaction.user.id, username, password)

        if not ok:
            return await interaction.response.send_message(
                f"❌ {error}",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Аккаунт создан!\\n"
            f"Логин: `{username}`\\n\\n"
            f"Пароль сохранён в защищённом виде.",
            ephemeral=True
        )


class AccountInfoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Создать аккаунт",
        emoji="📝",
        style=discord.ButtonStyle.success,
        custom_id="account:create"
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_account(interaction.user.id):
            return await interaction.response.send_message(
                "❌ У тебя уже есть аккаунт. Нажми «Мой аккаунт».",
                ephemeral=True
            )
        await interaction.response.send_modal(CreateAccountModal())

    @discord.ui.button(
        label="Мой аккаунт",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="account:info"
    )
    async def info(self, interaction: discord.Interaction, button: discord.ui.Button):
        account = get_account(interaction.user.id)

        if not account:
            return await interaction.response.send_message(
                "❌ Аккаунта ещё нет. Нажми «Создать аккаунт».",
                ephemeral=True
            )

        _, username, _, _, created_at, balance = account

        role_names = {
            "User",
            "Media",
            "Owner",
            "Developer",
            "Ticket helper",
            "Moderator",
        }
        user_roles = [
            role.name
            for role in interaction.user.roles
            if role.name in role_names
        ]
        roles_text = ", ".join(user_roles) if user_roles else "Нет"

        embed = discord.Embed(
            title="👤 Сведения об аккаунте",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Логин", value=f"`{username}`", inline=False)
        embed.add_field(name="Discord ID", value=f"`{interaction.user.id}`", inline=False)
        embed.add_field(name="Роль", value=roles_text, inline=False)
        embed.add_field(name="Баланс", value=f"`{balance:.2f} ₽`", inline=False)
        embed.add_field(name="Создан", value=f"`{created_at}`", inline=False)
        embed.add_field(
            name="Пароль",
            value="🔒 Не показывается и не хранится в открытом виде.",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- TICKETS ----------------

def is_ticket_helper(member: discord.Member) -> bool:
    return any(r.name.lower() == "ticket helper" for r in member.roles)

def can_manage_ticket(member: discord.Member) -> bool:
    return (
        member.guild_permissions.manage_channels
        or member.guild_permissions.administrator
        or is_ticket_helper(member)
    )


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Закрыть тикет",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        topic = interaction.channel.topic or ""
        owner_id = topic.removeprefix("alternative-ticket:").split(":", 1)[0]

        if not can_manage_ticket(interaction.user) and str(interaction.user.id) != owner_id:
            return await interaction.response.send_message(
                "❌ Закрыть тикет может только автор или staff.",
                ephemeral=True
            )

        await interaction.response.send_message("🔒 Тикет закрывается...")
        await asyncio.sleep(2)
        await interaction.channel.delete(
            reason=f"Ticket closed by {interaction.user}"
        )


class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=name,
                emoji=emoji,
                value=value
            )
            for value, (emoji, name) in TICKET_CATEGORIES.items()
        ]

        super().__init__(
            placeholder="Выберите категорию тикета...",
            options=options,
            custom_id="ticket:category"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        for channel in guild.text_channels:
            if (channel.topic or "").startswith(
                f"alternative-ticket:{user.id}:"
            ):
                return await interaction.response.send_message(
                    f"❌ У тебя уже есть открытый тикет: {channel.mention}",
                    ephemeral=True
                )

        emoji, category_name = TICKET_CATEGORIES[self.values[0]]

        category = discord.utils.get(
            guild.categories,
            name="🎫 TICKETS"
        )

        if category is None:
            try:
                category = await guild.create_category(
                    "🎫 TICKETS",
                    reason="Ticket system"
                )
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ Боту нужно право Manage Channels.",
                    ephemeral=True
                )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True
            ),
        }

        # Роль Ticket Helper автоматически получает доступ к тикетам.
        helper_role = next((r for r in guild.roles if r.name.lower() == "ticket helper"), None)
        if helper_role:
            overwrites[helper_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            )

        safe_name = "".join(
            c if c.isalnum() or c in "-_" else "-"
            for c in user.name.lower()
        ).strip("-")[:60]

        channel_name = f"ticket-{safe_name or 'user'}"

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                topic=f"alternative-ticket:{user.id}:{self.values[0]}",
                overwrites=overwrites,
                reason=f"Ticket: {category_name}"
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ Не хватает прав. Дай боту Manage Channels.",
                ephemeral=True
            )

        embed = discord.Embed(
            title=f"{emoji} {category_name}",
            description=(
                f"Привет, {user.mention}!\n\n"
                "Опиши вопрос подробно и приложи нужные файлы/скриншоты.\n\n"
                "Когда всё решено, нажми **🔒 Закрыть тикет**."
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Alternative Client • Support")

        await channel.send(
            content=user.mention,
            embed=embed,
            view=CloseView()
        )

        await interaction.response.send_message(
            f"✅ Тикет создан: {channel.mention}",
            ephemeral=True
        )


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())



# ---------------- MEDIA / FAQ / BALANCE ----------------

MEDIA_ROLE_NAMES = {"media": "Media", "media+": "Media+"}

def find_role(guild: discord.Guild, name: str):
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)

def account_balance(user_id: int) -> float:
    account = get_account(user_id)
    return float(account[5]) if account else 0.0

def add_balance(user_id: int, amount: float):
    conn = db()
    conn.execute("UPDATE accounts SET balance = COALESCE(balance, 0) + ? WHERE discord_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

async def fetch_tiktok_views(profile_url: str):
    """Best-effort public-page check. TikTok can block automated requests, so None means manual review."""
    parsed = urlparse(profile_url)
    if parsed.scheme not in {"http", "https"} or "tiktok.com" not in parsed.netloc.lower():
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(profile_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")
        # Public TikTok pages often contain playCount/play_count in embedded JSON.
        raw = re.findall(r'"(?:playCount|play_count)"\s*:\s*(\d+)', html)
        views = []
        for x in raw:
            n = int(x)
            if n not in views:
                views.append(n)
        views.sort(reverse=True)
        return views[:20] if len(views) >= 3 else None
    except Exception:
        return None

async def create_media_ticket(guild: discord.Guild, user: discord.Member, profile_url: str, paid: bool, payment_method: str = ""):
    category = discord.utils.get(guild.categories, name="🎫 TICKETS")
    if category is None:
        category = await guild.create_category("🎫 TICKETS", reason="Media application")
    helper_role = next((r for r in guild.roles if r.name.lower() == "ticket helper"), None)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
    }
    if helper_role:
        overwrites[helper_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in user.name.lower()).strip("-")[:50]
    channel = await guild.create_text_channel(
        name=f"media-{safe or 'user'}",
        category=category,
        topic=f"media-application:{user.id}:{'plus' if paid else 'free'}",
        overwrites=overwrites,
        reason="Media application"
    )
    desc = (
        f"👤 Заявитель: {user.mention}\n"
        f"🎵 TikTok: {profile_url}\n"
        f"💎 Тип: {'Media+' if paid else 'Media'}\n"
    )
    if payment_method:
        desc += f"💳 Удобный способ оплаты: **{payment_method}**\n"
    desc += "\nПроверка выполняется ботом автоматически, если TikTok отдаёт публичную статистику. Если статистика недоступна — Ticket Helper проверяет заявку вручную."
    embed = discord.Embed(title="🎬 Заявка на Media", description=desc, color=discord.Color.blurple())
    await channel.send(content=f"{user.mention} {'@here' if helper_role is None else helper_role.mention}", embed=embed, view=MediaReviewView())
    return channel

class MediaReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _get_owner(self, interaction):
        if not is_ticket_helper(interaction.user) and not interaction.user.guild_permissions.manage_channels and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Только Ticket Helper или staff.", ephemeral=True)
            return None
        topic = interaction.channel.topic or ""
        if not topic.startswith("media-application:"):
            await interaction.response.send_message("❌ Это не Media-заявка.", ephemeral=True)
            return None
        parts = topic.split(":")
        try:
            uid = int(parts[1])
        except Exception:
            await interaction.response.send_message("❌ Не удалось определить автора заявки.", ephemeral=True)
            return None
        return interaction.guild.get_member(uid)

    async def _approve(self, interaction, role_name):
        user = await self._get_owner(interaction)
        if not user:
            return
        role = find_role(interaction.guild, role_name)
        if not role:
            return await interaction.response.send_message(f"❌ Роль `{role_name}` не найдена.", ephemeral=True)
        try:
            await user.add_roles(role, reason=f"Media application approved by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Бот не может выдать роль. Подними его роль выше Media/Media+.", ephemeral=True)
        await interaction.response.send_message(f"✅ {user.mention} получил роль **{role_name}**.")
        try:
            await user.send(f"🎬 Ваша заявка одобрена. Вам выдана роль **{role_name}** на сервере **{interaction.guild.name}**.")
        except Exception:
            pass

    @discord.ui.button(label="Одобрить Media", emoji="🎬", style=discord.ButtonStyle.success, custom_id="media:approve")
    async def approve_media(self, interaction, button):
        await self._approve(interaction, "Media")

    @discord.ui.button(label="Одобрить Media+", emoji="💎", style=discord.ButtonStyle.primary, custom_id="media:approve_plus")
    async def approve_plus(self, interaction, button):
        await self._approve(interaction, "Media+")

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger, custom_id="media:reject")
    async def reject(self, interaction, button):
        user = await self._get_owner(interaction)
        if not user:
            return
        await interaction.response.send_message(f"❌ Заявка {user.mention} отклонена.")
        try:
            await user.send(f"❌ Ваша заявка на Media отклонена на сервере **{interaction.guild.name}**.")
        except Exception:
            pass

class MediaApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Подать на Media", emoji="🎬", style=discord.ButtonStyle.success, custom_id="media:apply_free")
    async def free(self, interaction, button):
        await interaction.response.send_modal(MediaApplyModal(paid=False))

    @discord.ui.button(label="Подать на Media+", emoji="💎", style=discord.ButtonStyle.primary, custom_id="media:apply_plus")
    async def plus(self, interaction, button):
        await interaction.response.send_modal(MediaApplyModal(paid=True))

class MediaApplyModal(discord.ui.Modal):
    def __init__(self, paid=False):
        self.paid = paid
        super().__init__(title="Заявка на Media+" if paid else "Заявка на Media")
        self.tiktok = discord.ui.TextInput(label="Ссылка на TikTok-профиль", placeholder="https://www.tiktok.com/@username", min_length=10, max_length=300)
        self.add_item(self.tiktok)
        if paid:
            self.payment = discord.ui.TextInput(label="Удобный способ оплаты", placeholder="Например: 50кк / 35₽ / другой", min_length=2, max_length=100, required=True)
            self.add_item(self.payment)

    async def on_submit(self, interaction: discord.Interaction):
        profile = str(self.tiktok.value).strip()
        if "tiktok.com" not in profile.lower():
            return await interaction.response.send_message("❌ Нужна ссылка на TikTok-профиль.", ephemeral=True)
        payment = str(self.payment.value).strip() if self.paid else ""
        await interaction.response.defer(ephemeral=True)
        try:
            views = await fetch_tiktok_views(profile)
            tier = None
            if views:
                if len([v for v in views if v >= 1000]) >= 3:
                    tier = "Media+"
                elif len([v for v in views if v >= 500]) >= 3:
                    tier = "Media"
            # Заявка всегда создаёт тикет, чтобы Ticket Helper видел её.
            channel = await create_media_ticket(interaction.guild, interaction.user, profile, self.paid, payment)
            if tier == "Media" and not self.paid:
                role = find_role(interaction.guild, "Media")
                if role:
                    await interaction.user.add_roles(role, reason="Automatic Media qualification")
                await channel.send("🤖 Автопроверка: найдено 3 видео с 500+ просмотров. Роль **Media** выдана автоматически.")
            elif tier == "Media+" and self.paid:
                role = find_role(interaction.guild, "Media+")
                if role:
                    await interaction.user.add_roles(role, reason="Automatic Media+ qualification")
                await channel.send("🤖 Автопроверка: найдено 3 видео с 1000+ просмотров. Роль **Media+** выдана автоматически. Указанный способ оплаты сохранён в заявке.")
            elif tier:
                await channel.send(f"🤖 Автопроверка нашла уровень **{tier}**, но тип заявки не совпадает. Ticket Helper проверит заявку вручную.")
            else:
                await channel.send("⚠️ TikTok не отдал статистику автоматически. Ticket Helper должен проверить заявку вручную.")
            await interaction.followup.send(f"✅ Заявка создана: {channel.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Боту не хватает прав Manage Channels / Manage Roles.", ephemeral=True)
        except Exception as e:
            print("Media application error:", repr(e))
            await interaction.followup.send("❌ Не удалось создать заявку. Проверь права бота.", ephemeral=True)

class FAQView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Media", emoji="🎬", style=discord.ButtonStyle.success, custom_id="faq:media")
    async def media(self, interaction, button):
        await interaction.response.send_message("🎬 Media: 3 публичных видео с 500+ просмотров. Если TikTok не отдаёт статистику, заявку проверяет Ticket Helper.", ephemeral=True)
    @discord.ui.button(label="Media+", emoji="💎", style=discord.ButtonStyle.primary, custom_id="faq:mediaplus")
    async def mediaplus(self, interaction, button):
        await interaction.response.send_message("💎 Media+: 3 видео с 1000+ просмотров. В заявке указывается удобный способ оплаты.", ephemeral=True)
    @discord.ui.button(label="Аккаунт", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="faq:account")
    async def account(self, interaction, button):
        await interaction.response.send_message("👤 Account Manager хранит логин, дату создания и баланс. Пароль в открытом виде не показывается.", ephemeral=True)

# ---------------- COMMANDS ----------------

@bot.tree.command(
    name="account",
    description="Открыть менеджер аккаунта"
)
async def account(interaction: discord.Interaction):
    embed = discord.Embed(
        title="👤 Account Manager",
        description=(
            "Здесь можно создать аккаунт и посмотреть сведения о нём.\n\n"
            "🔐 Пароль не показывается и не хранится в открытом виде."
        ),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(
        embed=embed,
        view=AccountInfoView()
    )


@bot.tree.command(
    name="account-info",
    description="Показать сведения о своём аккаунте"
)
async def account_info(interaction: discord.Interaction):
    account = get_account(interaction.user.id)

    if not account:
        return await interaction.response.send_message(
            "❌ У тебя ещё нет аккаунта. Используй `/account`.",
            ephemeral=True
        )

    _, username, _, _, created_at, balance = account

    embed = discord.Embed(
        title="👤 Твой аккаунт",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Логин", value=f"`{username}`", inline=False)
    embed.add_field(name="Discord ID", value=f"`{interaction.user.id}`", inline=False)
    embed.add_field(name="Баланс", value=f"`{balance:.2f} ₽`", inline=False)
    embed.add_field(name="Создан", value=f"`{created_at}`", inline=False)
    embed.add_field(
        name="Пароль",
        value="🔒 Скрыт.",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@bot.tree.command(
    name="ticket-panel",
    description="Создать панель тикетов"
)
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Alternative Client — Support",
        description=(
            "Выберите категорию ниже.\n\n"
            "Бот создаст приватный тикет.\n"
            "Есть отдельная категория **🎬 Media**."
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Alternative Client")

    await interaction.channel.send(
        embed=embed,
        view=TicketPanel()
    )
    await interaction.response.send_message(
        "✅ Панель тикетов создана.",
        ephemeral=True
    )


@bot.tree.command(name="media-panel", description="Создать панель заявок Media")
@app_commands.checks.has_permissions(manage_channels=True)
async def media_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬 Media",
        description=(
            "Подай заявку на бесплатную Media или Media+.\n\n"
            "🆓 **Media:** 3 видео с 500+ просмотров.\n"
            "💎 **Media+:** 3 видео с 1000+ просмотров + укажи удобный способ оплаты (например, 50кк или 35₽ за 1000 просмотров).\n\n"
            "Если TikTok не отдаёт статистику автоматически, заявку проверит Ticket Helper."
        ),
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=MediaApplicationView())
    await interaction.response.send_message("✅ Панель Media создана.", ephemeral=True)

@bot.tree.command(name="faq", description="Показать FAQ сервера")
async def faq(interaction: discord.Interaction):
    embed = discord.Embed(
        title="❓ FAQ",
        description=(
            "🎬 **Media** — бесплатная медиа при 3 видео с 500+ просмотров.\n"
            "💎 **Media+** — 3 видео с 1000+ просмотров и условия оплаты.\n"
            "🎫 **Тикеты** — приватные каналы, которые видит автор и роль Ticket Helper.\n"
            "👤 **Account Manager** — аккаунт и баланс."
        ),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=FAQView(), ephemeral=True)

@bot.tree.command(name="balance", description="Показать баланс Account Manager")
async def balance(interaction: discord.Interaction):
    if not get_account(interaction.user.id):
        return await interaction.response.send_message("❌ Сначала создай аккаунт через `/account`.", ephemeral=True)
    await interaction.response.send_message(f"💰 Твой баланс: **{account_balance(interaction.user.id):.2f} ₽**", ephemeral=True)

@bot.tree.command(name="add-balance", description="Начислить деньги на баланс пользователю")
@app_commands.describe(user="Пользователь", amount="Сумма в ₽")
async def add_balance_cmd(interaction: discord.Interaction, user: discord.Member, amount: float):
    if not (is_ticket_helper(interaction.user) or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("❌ Только Ticket Helper или Administrator.", ephemeral=True)
    if amount < 0.01 or amount > 1_000_000_000:
        return await interaction.response.send_message("❌ Сумма должна быть от 0.01 до 1 000 000 000 ₽.", ephemeral=True)
    if not get_account(user.id):
        return await interaction.response.send_message("❌ У пользователя нет аккаунта.", ephemeral=True)
    add_balance(user.id, float(amount))
    await interaction.response.send_message(f"✅ {user.mention} начислено **{amount:.2f} ₽**. Новый баланс: **{account_balance(user.id):.2f} ₽**.")

@bot.tree.command(
    name="ping",
    description="Проверить бота"
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! {round(bot.latency * 1000)} ms"
    )


@bot.tree.command(
    name="clear",
    description="Удалить сообщения"
)
@app_commands.describe(amount="Количество сообщений: 1-100")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(
        f"🧹 Удалено сообщений: {len(deleted)}",
        ephemeral=True
    )


# ---------------- STARTUP ----------------

@bot.event
async def setup_hook():
    # Persistent buttons/select menus continue working after restart.
    bot.add_view(TicketPanel())
    bot.add_view(CloseView())
    bot.add_view(AccountInfoView())
    bot.add_view(MediaApplicationView())
    bot.add_view(MediaReviewView())
    bot.add_view(FAQView())

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Slash command sync error:", e)


@bot.event
async def on_member_join(member: discord.Member):
    # Automatically give every new member the "User" role.
    if member.bot:
        return

    role = discord.utils.get(member.guild.roles, name="User")

    if role is None:
        try:
            role = await member.guild.create_role(
                name="User",
                reason="Automatic role for new members"
            )
        except discord.Forbidden:
            print(f"Не удалось создать роль User на сервере {member.guild.name}: нет права Manage Roles.")
            return
        except discord.HTTPException as e:
            print(f"Не удалось создать роль User на сервере {member.guild.name}: {e}")
            return

    try:
        await member.add_roles(role, reason="Automatic User role for new member")
        print(f"Роль User выдана: {member} на сервере {member.guild.name}")
    except discord.Forbidden:
        print(
            f"Не удалось выдать роль User пользователю {member}. "
            "Проверь: роль User должна быть ниже роли бота и у бота должно быть Manage Roles."
        )
    except discord.HTTPException as e:
        print(f"Ошибка выдачи роли User пользователю {member}: {e}")


@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        if entry.target and entry.target.id == channel.id and isinstance(entry.user, discord.Member):
            if await antinuke_check(guild, entry.user, "channel_delete"):
                await antinuke_timeout(guild, entry.user, "channel_delete")
            break

@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
        if entry.target and entry.target.id == role.id and isinstance(entry.user, discord.Member):
            if await antinuke_check(guild, entry.user, "role_delete"):
                await antinuke_timeout(guild, entry.user, "role_delete")
            break

@bot.event
async def on_member_ban(guild, user):
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
        if entry.target and entry.target.id == user.id and isinstance(entry.user, discord.Member):
            if await antinuke_check(guild, entry.user, "ban"):
                await antinuke_timeout(guild, entry.user, "ban")
            break

@bot.event
async def on_member_remove(member):
    guild = member.guild
    await asyncio.sleep(0.5)
    try:
        async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.kick):
            if entry.target and entry.target.id == member.id and isinstance(entry.user, discord.Member):
                if await antinuke_check(guild, entry.user, "kick"):
                    await antinuke_timeout(guild, entry.user, "kick")
                break
    except Exception:
        pass

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


if not TOKEN:
    raise RuntimeError(
        "Не найден DISCORD_TOKEN. Добавь токен бота в переменные окружения хостинга."
    )

bot.run(TOKEN)
