"""
╔══════════════════════════════════════════════════════════════╗
║              ESCROW BOT — single-file edition v2             ║
║  Stack: aiogram 3.x · aiosqlite · python-dotenv             ║
╚══════════════════════════════════════════════════════════════╝

.env (рядом с файлом):
    BOT_TOKEN=<token>
    ADMIN_ID=<your_telegram_id>
    DATABASE_PATH=database.db   # опционально

Новое в v2:
  - Таблица admins: выдача / отзыв прав админа по @username
  - Все проверки is_admin теперь смотрят и в таблицу admins
  - Раздел «Сделки» в адмике: детальная карточка каждой сделки
  - Из карточки сделки: подтвердить оплату / завершить / отменить вручную
  - Уведомления участникам при ручном изменении статуса
"""

import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════ #
#  0. CONFIG                                                      #
# ══════════════════════════════════════════════════════════════ #

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID:  int = int(os.environ["ADMIN_ID"])   # Супер-админ из .env (неотзываемый)
DB_PATH:   str = os.getenv("DATABASE_PATH", "database.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════ #
#  1. CONSTANTS                                                   #
# ══════════════════════════════════════════════════════════════ #

WALLET_TYPES: dict[str, str] = {
    "card":   "💳 Банковская карта",
    "crypto": "🔐 Криптовалюта",
    "ton":    "💎 TON кошелёк",
    "stars":  "⭐ Звёзды",
}

DEAL_STATUSES: dict[str, str] = {
    "created":         "🆕 Создана",
    "waiting_payment": "⏳ Ожидает оплаты",
    "paid":            "💰 Оплачена",
    "completed":       "✅ Завершена",
    "cancelled":       "❌ Отменена",
}

ROLE_LABELS: dict[str, str] = {
    "buyer":  "🛒 Покупатель",
    "seller": "🏪 Продавец",
}

WALLET_PROMPTS: dict[str, str] = {
    "card":   "Введите номер карты (16 цифр, только цифры):",
    "crypto": "Введите адрес криптовалютного кошелька:",
    "ton":    "Введите адрес TON кошелька (начинается с UQ или EQ):",
    "stars":  "Введите username Telegram или ID для получения звёзд:",
}


# ══════════════════════════════════════════════════════════════ #
#  2. DATABASE                                                    #
# ══════════════════════════════════════════════════════════════ #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username    TEXT,
    first_name  TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS admins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username    TEXT,
    granted_by  INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS wallets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    type       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS deals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    partner_id INTEGER,
    role       TEXT NOT NULL,
    amount     REAL NOT NULL,
    wallet_id  INTEGER NOT NULL,
    status     TEXT DEFAULT 'created',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (creator_id) REFERENCES users(id),
    FOREIGN KEY (wallet_id)  REFERENCES wallets(id)
);
CREATE TABLE IF NOT EXISTS support_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    message    TEXT NOT NULL,
    is_read    INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ── Users ────────────────────────────────────────────────── #

    async def get_user(self, telegram_id: int):
        async with self._conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_user_by_username(self, username: str):
        """username без символа @"""
        async with self._conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ) as cur:
            return await cur.fetchone()

    async def create_user(self, telegram_id: int, username: str, first_name: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?,?,?)",
            (telegram_id, username or "", first_name or ""),
        )
        await self._conn.commit()

    async def update_user_username(self, telegram_id: int, username: str) -> None:
        """Обновляем username при каждом /start, чтобы поиск был актуальным."""
        await self._conn.execute(
            "UPDATE users SET username=? WHERE telegram_id=?",
            (username or "", telegram_id),
        )
        await self._conn.commit()

    async def get_all_users(self):
        async with self._conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

    # ── Admins ───────────────────────────────────────────────── #

    async def is_admin(self, telegram_id: int) -> bool:
        """Супер-админ из .env всегда True; дополнительные — из таблицы."""
        if telegram_id == ADMIN_ID:
            return True
        async with self._conn.execute(
            "SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def add_admin(self, telegram_id: int, username: str, granted_by: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO admins (telegram_id, username, granted_by) VALUES (?,?,?)",
            (telegram_id, username or "", granted_by),
        )
        await self._conn.commit()

    async def remove_admin(self, telegram_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM admins WHERE telegram_id = ?", (telegram_id,)
        )
        await self._conn.commit()

    async def get_all_admins(self):
        async with self._conn.execute(
            "SELECT * FROM admins ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

    # ── Wallets ──────────────────────────────────────────────── #

    async def add_wallet(self, user_id: int, wtype: str, data: str) -> None:
        await self._conn.execute(
            "INSERT INTO wallets (user_id, type, data) VALUES (?,?,?)",
            (user_id, wtype, data),
        )
        await self._conn.commit()

    async def get_wallet(self, wallet_id: int):
        async with self._conn.execute(
            "SELECT * FROM wallets WHERE id = ?", (wallet_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_user_wallets(self, user_id: int):
        async with self._conn.execute(
            "SELECT * FROM wallets WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            return await cur.fetchall()

    async def get_all_wallets(self):
        async with self._conn.execute(
            """SELECT w.*, u.telegram_id, u.username, u.first_name
               FROM wallets w JOIN users u ON w.user_id = u.id
               ORDER BY w.created_at DESC"""
        ) as cur:
            return await cur.fetchall()

    # ── Deals ────────────────────────────────────────────────── #

    async def create_deal(self, creator_id: int, role: str, amount: float, wallet_id: int) -> int:
        cur = await self._conn.execute(
            "INSERT INTO deals (creator_id, role, amount, wallet_id) VALUES (?,?,?,?)",
            (creator_id, role, amount, wallet_id),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_deal(self, deal_id: int):
        async with self._conn.execute(
            "SELECT * FROM deals WHERE id = ?", (deal_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_deal_with_users(self, deal_id: int):
        """Сделка + telegram_id обоих участников для уведомлений."""
        async with self._conn.execute(
            """
            SELECT d.*,
                   uc.telegram_id AS creator_tg,
                   up.telegram_id AS partner_tg
            FROM deals d
            JOIN users uc ON d.creator_id = uc.id
            LEFT JOIN users up ON d.partner_id = up.id
            WHERE d.id = ?
            """,
            (deal_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_user_deals(self, user_id: int):
        async with self._conn.execute(
            "SELECT * FROM deals WHERE creator_id=? OR partner_id=? ORDER BY created_at DESC",
            (user_id, user_id),
        ) as cur:
            return await cur.fetchall()

    async def get_all_deals(self):
        async with self._conn.execute(
            "SELECT * FROM deals ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

    async def get_deals_by_status(self, status: str):
        async with self._conn.execute(
            "SELECT * FROM deals WHERE status=? ORDER BY created_at DESC", (status,)
        ) as cur:
            return await cur.fetchall()

    async def update_deal_status(self, deal_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE deals SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, deal_id),
        )
        await self._conn.commit()

    async def join_deal(self, deal_id: int, partner_id: int) -> None:
        await self._conn.execute(
            "UPDATE deals SET partner_id=?, status='waiting_payment',"
            " updated_at=datetime('now') WHERE id=?",
            (partner_id, deal_id),
        )
        await self._conn.commit()

    # ── Support ──────────────────────────────────────────────── #

    async def add_support_message(self, user_id: int, message: str) -> None:
        await self._conn.execute(
            "INSERT INTO support_messages (user_id, message) VALUES (?,?)",
            (user_id, message),
        )
        await self._conn.commit()

    async def get_support_messages(self):
        async with self._conn.execute(
            """SELECT sm.*, u.telegram_id, u.username, u.first_name
               FROM support_messages sm JOIN users u ON sm.user_id = u.id
               ORDER BY sm.created_at DESC"""
        ) as cur:
            return await cur.fetchall()


# ══════════════════════════════════════════════════════════════ #
#  3. FSM STATES                                                  #
# ══════════════════════════════════════════════════════════════ #

class AddWalletSt(StatesGroup):
    choosing_type = State()
    entering_data = State()

class CreateDealSt(StatesGroup):
    choosing_role   = State()
    entering_amount = State()
    choosing_wallet = State()


class JoinDealByIdSt(StatesGroup):
    entering_id = State()

class SupportSt(StatesGroup):
    entering_message = State()

class BroadcastSt(StatesGroup):
    entering_message = State()

class GrantAdminSt(StatesGroup):
    entering_username = State()

class RevokeAdminSt(StatesGroup):
    entering_username = State()


# ══════════════════════════════════════════════════════════════ #
#  4. KEYBOARDS                                                   #
# ══════════════════════════════════════════════════════════════ #

def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)

# Переиспользуемые кнопки
BACK_MENU_BTN  = _btn("◀️ В главное меню", "menu:main")
CANCEL_BTN     = _btn("❌ Отмена",          "menu:main")
BACK_ADMIN_BTN = _btn("◀️ В меню админа",  "admin:menu")

# ── User keyboards ───────────────────────────────────────────── #

def main_menu_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("🤝 Создать сделку",        "deals:create")],
        [_btn("🔎 Войти по ID сделки", "deal:enter_id")],
        [_btn("💼 Средства",              "funds:view")],
        [_btn("👛 Управление кошельками", "wallets:menu")],
        [_btn("🆘 Поддержка",             "support:start")],
    )

def wallets_menu_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("➕ Добавить кошелёк", "wallets:add")],
        [_btn("📋 Мои кошельки",     "wallets:list")],
        [BACK_MENU_BTN],
    )

def wallet_types_kb() -> InlineKeyboardMarkup:
    rows = [[_btn(label, f"wallet_type:{key}")] for key, label in WALLET_TYPES.items()]
    rows.append([_btn("◀️ Назад", "wallets:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def wallet_list_kb(wallets) -> InlineKeyboardMarkup:
    rows = [[_btn(
        f"{WALLET_TYPES.get(w['type'], w['type'])}: {w['data'][:20]}",
        f"wallet:view:{w['id']}"
    )] for w in wallets]
    rows.append([_btn("◀️ Назад", "wallets:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def deal_roles_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("🛒 Покупатель", "deal_role:buyer")],
        [_btn("🏪 Продавец",   "deal_role:seller")],
        [BACK_MENU_BTN],
    )

def choose_wallet_kb(wallets) -> InlineKeyboardMarkup:
    rows = [[_btn(
        f"{WALLET_TYPES.get(w['type'], w['type'])}: {w['data'][:20]}",
        f"deal_wallet:{w['id']}"
    )] for w in wallets]
    rows.append([BACK_MENU_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def deal_actions_kb(deal_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "waiting_payment":
        rows.append([_btn("💰 Подтвердить оплату", f"deal:pay:{deal_id}")])
    if status == "paid":
        rows.append([_btn("✅ Завершить сделку",    f"deal:complete:{deal_id}")])
    if status in ("created", "waiting_payment", "paid"):
        rows.append([_btn("❌ Отменить сделку",     f"deal:cancel:{deal_id}")])
    rows.append([BACK_MENU_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def join_deal_kb(deal_id: int) -> InlineKeyboardMarkup:
    return _kb([_btn("🤝 Присоединиться к сделке", f"deal:join:{deal_id}")])

def back_menu_kb() -> InlineKeyboardMarkup:
    return _kb([BACK_MENU_BTN])

def cancel_kb() -> InlineKeyboardMarkup:
    return _kb([CANCEL_BTN])

# ── Admin keyboards ──────────────────────────────────────────── #

def admin_menu_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("👥 Пользователи",  "admin:users")],
        [_btn("👛 Кошельки",       "admin:wallets")],
        [_btn("🤝 Сделки",         "admin:deals")],
        [_btn("🆘 Поддержка",      "admin:support")],
        [_btn("📢 Рассылка",       "admin:broadcast")],
        [_btn("🔑 Управление админами", "admin:admins")],
    )

def admin_deals_filter_kb() -> InlineKeyboardMarkup:
    rows = [[_btn("📋 Все сделки", "admin:deals_list:all")]]
    for status, label in DEAL_STATUSES.items():
        rows.append([_btn(label, f"admin:deals_list:{status}")])
    rows.append([BACK_ADMIN_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_deal_card_kb(deal_id: int, status: str) -> InlineKeyboardMarkup:
    """Карточка сделки в админке — ручное управление статусом."""
    rows = []
    if status == "waiting_payment":
        rows.append([_btn("💰 Подтвердить оплату вручную",  f"admin:deal_pay:{deal_id}")])
    if status in ("waiting_payment", "paid"):
        rows.append([_btn("✅ Завершить сделку вручную",    f"admin:deal_complete:{deal_id}")])
    if status not in ("completed", "cancelled"):
        rows.append([_btn("❌ Отменить сделку вручную",     f"admin:deal_cancel:{deal_id}")])
    rows.append([_btn("◀️ К списку сделок", "admin:deals")])
    rows.append([BACK_ADMIN_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_deals_list_kb(deals, status_filter: str) -> InlineKeyboardMarkup:
    """Список сделок с кнопкой на каждую карточку."""
    rows = []
    for d in deals[:20]:
        sl = DEAL_STATUSES.get(d["status"], d["status"])
        rows.append([_btn(f"#{d['id']} | {d['amount']:.2f} | {sl}", f"admin:deal_view:{d['id']}")])
    rows.append([_btn("◀️ К фильтру", "admin:deals")])
    rows.append([BACK_ADMIN_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_admins_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("➕ Выдать права админа",   "admin:grant_admin")],
        [_btn("➖ Отозвать права админа", "admin:revoke_admin")],
        [_btn("📋 Список админов",        "admin:admins_list")],
        [BACK_ADMIN_BTN],
    )

def admin_back_kb() -> InlineKeyboardMarkup:
    return _kb([BACK_ADMIN_BTN])

def cancel_admin_kb() -> InlineKeyboardMarkup:
    return _kb([BACK_ADMIN_BTN])


# ══════════════════════════════════════════════════════════════ #
#  5. MIDDLEWARE                                                   #
# ══════════════════════════════════════════════════════════════ #

class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        return await handler(event, data)


# ══════════════════════════════════════════════════════════════ #
#  6. ROUTERS                                                     #
# ══════════════════════════════════════════════════════════════ #

router_start   = Router(name="start")
router_wallets = Router(name="wallets")
router_deals   = Router(name="deals")
router_funds   = Router(name="funds")
router_support = Router(name="support")
router_admin   = Router(name="admin")


# ──────────────────────────────────────────────────────────────
#  START & MAIN MENU
# ──────────────────────────────────────────────────────────────

@router_start.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await db.create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        text = (
            f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
            "✅ Вы успешно зарегистрированы.\n\n"
            "Выберите нужный раздел:"
        )
    else:
        # Синхронизируем username, чтобы поиск по нему работал актуально
        await db.update_user_username(message.from_user.id, message.from_user.username or "")
        text = (
            f"👋 С возвращением, <b>{message.from_user.first_name}</b>!\n\n"
            "Выберите нужный раздел:"
        )
    await message.answer(text, reply_markup=main_menu_kb())


@router_start.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите нужный раздел:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  WALLETS
# ──────────────────────────────────────────────────────────────

@router_wallets.callback_query(F.data == "wallets:menu")
async def wallets_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👛 <b>Управление кошельками</b>\n\nВыберите действие:",
        reply_markup=wallets_menu_kb(),
    )
    await callback.answer()


@router_wallets.callback_query(F.data == "wallets:add")
async def wallet_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "➕ <b>Добавление кошелька</b>\n\nВыберите тип:",
        reply_markup=wallet_types_kb(),
    )
    await state.set_state(AddWalletSt.choosing_type)
    await callback.answer()


@router_wallets.callback_query(AddWalletSt.choosing_type, F.data.startswith("wallet_type:"))
async def wallet_type_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    wtype = callback.data.split(":")[1]
    await state.update_data(wallet_type=wtype)
    await callback.message.edit_text(
        f"<b>Тип:</b> {WALLET_TYPES[wtype]}\n\n{WALLET_PROMPTS[wtype]}",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AddWalletSt.entering_data)
    await callback.answer()


@router_wallets.message(AddWalletSt.entering_data)
async def wallet_data_entered(message: Message, state: FSMContext, db: Database) -> None:
    raw = message.text.strip()
    data = await state.get_data()
    wtype: str = data["wallet_type"]

    if wtype == "card" and (not raw.isdigit() or len(raw) != 16):
        await message.answer(
            "❌ Номер карты должен содержать ровно <b>16 цифр</b>.\nПопробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    if wtype == "ton" and not (raw.startswith("UQ") or raw.startswith("EQ")):
        await message.answer(
            "❌ Адрес TON должен начинаться с <b>UQ</b> или <b>EQ</b>.\nПопробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return

    user = await db.get_user(message.from_user.id)
    await db.add_wallet(user_id=user["id"], wtype=wtype, data=raw)
    await state.clear()
    await message.answer(
        f"✅ <b>Кошелёк добавлен!</b>\n\n"
        f"<b>Тип:</b>    {WALLET_TYPES[wtype]}\n"
        f"<b>Данные:</b> <code>{raw}</code>",
        reply_markup=back_menu_kb(),
    )


@router_wallets.callback_query(F.data == "wallets:list")
async def wallets_list(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    if not wallets:
        await callback.message.edit_text(
            "📋 <b>Мои кошельки</b>\n\nКошельков пока нет. Добавьте первый!",
            reply_markup=wallets_menu_kb(),
        )
    else:
        await callback.message.edit_text(
            f"📋 <b>Мои кошельки</b>  ({len(wallets)} шт.)\n\nНажмите для деталей:",
            reply_markup=wallet_list_kb(wallets),
        )
    await callback.answer()


@router_wallets.callback_query(F.data.startswith("wallet:view:"))
async def wallet_view(callback: CallbackQuery, db: Database) -> None:
    wallet_id = int(callback.data.split(":")[2])
    w = await db.get_wallet(wallet_id)
    if not w:
        await callback.answer("Кошелёк не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"👛 <b>Кошелёк #{w['id']}</b>\n\n"
        f"<b>Тип:</b>      {WALLET_TYPES.get(w['type'], w['type'])}\n"
        f"<b>Данные:</b>   <code>{w['data']}</code>\n"
        f"<b>Добавлен:</b> {w['created_at']}",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  DEALS
# ──────────────────────────────────────────────────────────────

@router_deals.callback_query(F.data == "deals:create")
async def create_deal_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    if not wallets:
        await callback.message.edit_text(
            "❌ <b>Нет кошельков</b>\n\n"
            "Для создания сделки нужен хотя бы один кошелёк.\n"
            "Добавьте его в разделе <b>Управление кошельками</b>.",
            reply_markup=back_menu_kb(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "🤝 <b>Создание сделки</b>\n\nВыберите вашу роль:",
        reply_markup=deal_roles_kb(),
    )
    await state.set_state(CreateDealSt.choosing_role)
    await callback.answer()


@router_deals.callback_query(CreateDealSt.choosing_role, F.data.startswith("deal_role:"))
async def deal_role_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    role = callback.data.split(":")[1]
    await state.update_data(role=role)
    await callback.message.edit_text(
        f"<b>Роль:</b> {ROLE_LABELS[role]}\n\n"
        "💵 Введите сумму сделки (например: <code>1500</code> или <code>99.99</code>):",
        reply_markup=cancel_kb(),
    )
    await state.set_state(CreateDealSt.entering_amount)
    await callback.answer()


@router_deals.message(CreateDealSt.entering_amount)
async def deal_amount_entered(message: Message, state: FSMContext, db: Database) -> None:
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Введите корректную сумму — положительное число.\n"
            "Например: <code>500</code> или <code>49.90</code>:",
            reply_markup=cancel_kb(),
        )
        return
    await state.update_data(amount=amount)
    user = await db.get_user(message.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    await message.answer(
        f"<b>Сумма:</b> {amount:.2f}\n\n👛 Выберите кошелёк для сделки:",
        reply_markup=choose_wallet_kb(wallets),
    )
    await state.set_state(CreateDealSt.choosing_wallet)


@router_deals.callback_query(CreateDealSt.choosing_wallet, F.data.startswith("deal_wallet:"))
async def deal_wallet_chosen(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    wallet_id = int(callback.data.split(":")[1])
    fsm = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    deal_id = await db.create_deal(
        creator_id=user["id"], role=fsm["role"],
        amount=fsm["amount"], wallet_id=wallet_id,
    )
    wallet = await db.get_wallet(wallet_id)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Сделка #{deal_id} создана!</b>\n\n"
        f"<b>Роль:</b>    {ROLE_LABELS[fsm['role']]}\n"
        f"<b>Сумма:</b>   {fsm['amount']:.2f}\n"
        f"<b>Кошелёк:</b> {WALLET_TYPES.get(wallet['type'], wallet['type'])}\n"
        f"<b>Статус:</b>  {DEAL_STATUSES['created']}\n\n"
        f"⏳ Ожидаем второго участника.\n"
        f"Поделитесь ID сделки: <code>{deal_id}</code>",
        reply_markup=deal_actions_kb(deal_id, "created"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:join:"))
async def deal_join(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] == user["id"]:
        await callback.answer("Нельзя присоединиться к собственной сделке.", show_alert=True)
        return
    if deal["partner_id"]:
        await callback.answer("В сделке уже есть второй участник.", show_alert=True)
        return
    if deal["status"] != "created":
        await callback.answer("Сделка недоступна для присоединения.", show_alert=True)
        return
    await db.join_deal(deal_id, user["id"])
    await callback.message.edit_text(
        f"✅ <b>Вы присоединились к сделке #{deal_id}</b>\n\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n"
        f"<b>Статус:</b> {DEAL_STATUSES['waiting_payment']}\n\n"
        "Подтвердите оплату после перевода средств.",
        reply_markup=deal_actions_kb(deal_id, "waiting_payment"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:pay:"))
async def deal_pay(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "waiting_payment":
        await callback.answer("Невозможно подтвердить оплату.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "paid")
    await callback.message.edit_text(
        f"💰 <b>Оплата по сделке #{deal_id} подтверждена!</b>\n\n"
        f"<b>Статус:</b> {DEAL_STATUSES['paid']}\n\n"
        "Получатель должен подтвердить получение средств.",
        reply_markup=deal_actions_kb(deal_id, "paid"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:complete:"))
async def deal_complete(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "paid":
        await callback.answer("Завершить можно только оплаченную сделку.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "completed")
    await callback.message.edit_text(
        f"🎉 <b>Сделка #{deal_id} успешно завершена!</b>\n\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n"
        f"<b>Статус:</b> {DEAL_STATUSES['completed']}\n\n"
        "Спасибо за использование нашего сервиса!",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:cancel:"))
async def deal_cancel(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] in ("completed", "cancelled"):
        await callback.answer("Эту сделку уже невозможно отменить.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "cancelled")
    await callback.message.edit_text(
        f"❌ <b>Сделка #{deal_id} отменена.</b>\n\n"
        "По вопросам обратитесь в поддержку.",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()

@router_deals.callback_query(F.data == "deal:enter_id")
async def enter_deal_id_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "🔎 <b>Вход в сделку</b>\n\nВведите ID сделки:",
        reply_markup=cancel_kb(),
    )
    await state.set_state(JoinDealByIdSt.entering_id)
    await callback.answer()


@router_deals.message(JoinDealByIdSt.entering_id)
async def enter_deal_id_process(message: Message, state: FSMContext, db: Database) -> None:
    try:
        deal_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите корректный числовой ID:", reply_markup=cancel_kb())
        return

    deal = await db.get_deal(deal_id)
    if not deal:
        await message.answer("❌ Сделка не найдена.", reply_markup=back_menu_kb())
        await state.clear()
        return

    user = await db.get_user(message.from_user.id)

    if deal["creator_id"] == user["id"]:
        await message.answer("❌ Нельзя зайти в свою сделку.", reply_markup=back_menu_kb())
        await state.clear()
        return

    if deal["partner_id"]:
        await message.answer("❌ В сделке уже есть второй участник.", reply_markup=back_menu_kb())
        await state.clear()
        return

    if deal["status"] != "created":
        await message.answer("❌ Сделка недоступна для входа.", reply_markup=back_menu_kb())
        await state.clear()
        return

    await db.join_deal(deal_id, user["id"])

    await message.answer(
        f"✅ <b>Вы вошли в сделку #{deal_id}</b>\n\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n"
        f"<b>Статус:</b> {DEAL_STATUSES['waiting_payment']}\n\n"
        "Подтвердите оплату после перевода средств.",
        reply_markup=deal_actions_kb(deal_id, "waiting_payment"),
    )

    await state.clear()

# ──────────────────────────────────────────────────────────────
#  FUNDS
# ──────────────────────────────────────────────────────────────

@router_funds.callback_query(F.data == "funds:view")
async def funds_view(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    deals   = await db.get_user_deals(user["id"])

    text = "💼 <b>Средства</b>\n\n"
    if wallets:
        text += f"<b>👛 Кошельков:</b> {len(wallets)}\n"
        for w in wallets:
            text += f"  • {WALLET_TYPES.get(w['type'], w['type'])}: <code>{w['data'][:25]}</code>\n"
    else:
        text += "👛 <i>Кошельков нет</i>\n"
    text += "\n"

    if not deals:
        text += "📭 <i>Сделок пока нет</i>"
    else:
        active = [d for d in deals if d["status"] not in ("completed", "cancelled")]
        closed = [d for d in deals if d["status"] in ("completed", "cancelled")]
        if active:
            text += f"<b>🔄 Активные ({len(active)}):</b>\n"
            for d in active:
                icon = "🛒" if d["role"] == "buyer" else "🏪"
                text += f"  {icon} <code>#{d['id']}</code> — {d['amount']:.2f} — {DEAL_STATUSES.get(d['status'], d['status'])}\n"
        if closed:
            text += f"\n<b>📁 Завершённые ({len(closed)}):</b>\n"
            for d in closed[:5]:
                icon = "🛒" if d["role"] == "buyer" else "🏪"
                text += f"  {icon} <code>#{d['id']}</code> — {d['amount']:.2f} — {DEAL_STATUSES.get(d['status'], d['status'])}\n"

    await callback.message.edit_text(text, reply_markup=back_menu_kb())
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  SUPPORT
# ──────────────────────────────────────────────────────────────

@router_support.callback_query(F.data == "support:start")
async def support_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите вашу проблему или вопрос.\n"
        "Администратор ответит в ближайшее время:",
        reply_markup=cancel_kb(),
    )
    await state.set_state(SupportSt.entering_message)
    await callback.answer()


@router_support.message(SupportSt.entering_message)
async def support_message_received(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    user = await db.get_user(message.from_user.id)
    await db.add_support_message(user_id=user["id"], message=message.text.strip())

    uname = f" (@{message.from_user.username})" if message.from_user.username else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆘 <b>Новое обращение</b>\n\n"
            f"<b>От:</b> {message.from_user.first_name}{uname}\n"
            f"<b>ID:</b> <code>{message.from_user.id}</code>\n\n"
            f"<b>Сообщение:</b>\n{message.text.strip()}",
        )
    except Exception:
        pass

    await state.clear()
    await message.answer(
        "✅ <b>Сообщение отправлено!</b>\n\n"
        "Ваше обращение принято. Мы ответим в ближайшее время.",
        reply_markup=back_menu_kb(),
    )


# ──────────────────────────────────────────────────────────────
#  ADMIN — HELPERS
# ──────────────────────────────────────────────────────────────

async def _notify_deal_participants(
    bot: Bot,
    db: Database,
    deal_id: int,
    text: str,
) -> None:
    """Отправить уведомление обоим участникам сделки."""
    deal = await db.get_deal_with_users(deal_id)
    if not deal:
        return
    for tg_id in {deal["creator_tg"], deal["partner_tg"]}:
        if tg_id:
            try:
                await bot.send_message(tg_id, text)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────
#  ADMIN — ENTRY & MENU
# ──────────────────────────────────────────────────────────────

@router_admin.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await message.answer(
        "🔑 <b>Панель администратора</b>\n\nВыберите раздел:",
        reply_markup=admin_menu_kb(),
    )


@router_admin.callback_query(F.data == "admin:menu")
async def admin_menu_cb(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🔑 <b>Панель администратора</b>\n\nВыберите раздел:",
        reply_markup=admin_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  ADMIN — USERS
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    users = await db.get_all_users()
    if not users:
        text = "👥 <b>Пользователи</b>\n\n<i>Пока никого нет.</i>"
    else:
        lines = []
        for u in users[:30]:
            uname = f"@{u['username']}" if u["username"] else "<i>без username</i>"
            lines.append(
                f"  • <a href='tg://user?id={u['telegram_id']}'>{u['first_name']}</a> "
                f"({uname}) — <code>{u['telegram_id']}</code>"
            )
        text = f"👥 <b>Пользователи</b>  <i>({len(users)} всего)</i>\n\n" + "\n".join(lines)
        if len(users) > 30:
            text += f"\n\n<i>...и ещё {len(users) - 30}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  ADMIN — WALLETS
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:wallets")
async def admin_wallets(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    wallets = await db.get_all_wallets()
    if not wallets:
        text = "👛 <b>Кошельки</b>\n\n<i>Кошельков пока нет.</i>"
    else:
        lines = []
        for w in wallets[:30]:
            wtype = WALLET_TYPES.get(w["type"], w["type"])
            uname = f"@{w['username']}" if w["username"] else str(w["telegram_id"])
            lines.append(f"  • {wtype}: <code>{w['data'][:25]}</code> — {uname}")
        text = f"👛 <b>Все кошельки</b>  <i>({len(wallets)} всего)</i>\n\n" + "\n".join(lines)
        if len(wallets) > 30:
            text += f"\n\n<i>...и ещё {len(wallets) - 30}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  ADMIN — DEALS (список + карточка + ручное управление)
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:deals")
async def admin_deals_menu(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 <b>Сделки</b>\n\nВыберите фильтр по статусу:",
        reply_markup=admin_deals_filter_kb(),
    )
    await callback.answer()


@router_admin.callback_query(F.data.startswith("admin:deals_list:"))
async def admin_deals_list(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    status_filter = callback.data.split(":")[2]
    if status_filter == "all":
        deals = await db.get_all_deals()
        title = "Все сделки"
    else:
        deals = await db.get_deals_by_status(status_filter)
        title = DEAL_STATUSES.get(status_filter, status_filter)

    if not deals:
        await callback.message.edit_text(
            f"🤝 <b>{title}</b>\n\n<i>По этому фильтру ничего нет.</i>",
            reply_markup=admin_back_kb(),
        )
    else:
        await callback.message.edit_text(
            f"🤝 <b>{title}</b>  <i>({len(deals)} шт.)</i>\n\n"
            "Нажмите на сделку для детального просмотра и управления:",
            reply_markup=admin_deals_list_kb(deals, status_filter),
        )
    await callback.answer()


@router_admin.callback_query(F.data.startswith("admin:deal_view:"))
async def admin_deal_view(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal_with_users(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    wallet = await db.get_wallet(deal["wallet_id"])
    wtype  = WALLET_TYPES.get(wallet["type"], wallet["type"]) if wallet else "—"
    wdata  = wallet["data"] if wallet else "—"

    partner_part = f"<code>{deal['partner_tg']}</code>" if deal["partner_tg"] else "<i>ещё не присоединился</i>"

    text = (
        f"🤝 <b>Сделка #{deal_id}</b>\n\n"
        f"<b>Статус:</b>   {DEAL_STATUSES.get(deal['status'], deal['status'])}\n"
        f"<b>Сумма:</b>    {deal['amount']:.2f}\n"
        f"<b>Роль:</b>     {ROLE_LABELS.get(deal['role'], deal['role'])}\n"
        f"<b>Кошелёк:</b> {wtype}: <code>{wdata[:30]}</code>\n\n"
        f"<b>Создатель:</b> <code>{deal['creator_tg']}</code>  (ID: {deal['creator_id']})\n"
        f"<b>Партнёр:</b>   {partner_part}\n\n"
        f"<b>Создана:</b>  {deal['created_at']}\n"
        f"<b>Обновлена:</b> {deal['updated_at']}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=admin_deal_card_kb(deal_id, deal["status"]),
    )
    await callback.answer()


@router_admin.callback_query(F.data.startswith("admin:deal_pay:"))
async def admin_deal_pay(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "waiting_payment":
        await callback.answer("Нельзя подтвердить оплату для этой сделки.", show_alert=True)
        return

    await db.update_deal_status(deal_id, "paid")

    # Уведомляем участников
    await _notify_deal_participants(
        bot, db, deal_id,
        f"💰 <b>Оплата по сделке #{deal_id} подтверждена администратором.</b>\n\n"
        f"Статус изменён на: {DEAL_STATUSES['paid']}",
    )

    await callback.answer("✅ Оплата подтверждена", show_alert=True)
    # Обновляем карточку
    deal_updated = await db.get_deal_with_users(deal_id)
    wallet = await db.get_wallet(deal_updated["wallet_id"])
    wtype  = WALLET_TYPES.get(wallet["type"], wallet["type"]) if wallet else "—"
    wdata  = wallet["data"] if wallet else "—"
    partner_part = (
        f"<code>{deal_updated['partner_tg']}</code>"
        if deal_updated["partner_tg"]
        else "<i>ещё не присоединился</i>"
    )
    await callback.message.edit_text(
        f"🤝 <b>Сделка #{deal_id}</b>\n\n"
        f"<b>Статус:</b>   {DEAL_STATUSES['paid']}\n"
        f"<b>Сумма:</b>    {deal_updated['amount']:.2f}\n"
        f"<b>Роль:</b>     {ROLE_LABELS.get(deal_updated['role'], deal_updated['role'])}\n"
        f"<b>Кошелёк:</b> {wtype}: <code>{wdata[:30]}</code>\n\n"
        f"<b>Создатель:</b> <code>{deal_updated['creator_tg']}</code>\n"
        f"<b>Партнёр:</b>   {partner_part}\n\n"
        f"✅ <i>Оплата подтверждена администратором</i>",
        reply_markup=admin_deal_card_kb(deal_id, "paid"),
    )


@router_admin.callback_query(F.data.startswith("admin:deal_complete:"))
async def admin_deal_complete(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] not in ("waiting_payment", "paid"):
        await callback.answer("Невозможно завершить эту сделку.", show_alert=True)
        return

    await db.update_deal_status(deal_id, "completed")

    await _notify_deal_participants(
        bot, db, deal_id,
        f"🎉 <b>Сделка #{deal_id} завершена администратором.</b>\n\n"
        f"Статус: {DEAL_STATUSES['completed']}",
    )

    await callback.answer("✅ Сделка завершена", show_alert=True)
    await callback.message.edit_text(
        f"🤝 <b>Сделка #{deal_id}</b>\n\n"
        f"<b>Статус:</b> {DEAL_STATUSES['completed']}\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n\n"
        f"✅ <i>Завершена администратором</i>",
        reply_markup=admin_back_kb(),
    )


@router_admin.callback_query(F.data.startswith("admin:deal_cancel:"))
async def admin_deal_cancel(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] in ("completed", "cancelled"):
        await callback.answer("Эту сделку уже нельзя отменить.", show_alert=True)
        return

    await db.update_deal_status(deal_id, "cancelled")

    await _notify_deal_participants(
        bot, db, deal_id,
        f"❌ <b>Сделка #{deal_id} отменена администратором.</b>\n\n"
        "По вопросам обратитесь в поддержку.",
    )

    await callback.answer("Сделка отменена", show_alert=True)
    await callback.message.edit_text(
        f"🤝 <b>Сделка #{deal_id}</b>\n\n"
        f"<b>Статус:</b> {DEAL_STATUSES['cancelled']}\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n\n"
        f"❌ <i>Отменена администратором</i>",
        reply_markup=admin_back_kb(),
    )


# ──────────────────────────────────────────────────────────────
#  ADMIN — SUPPORT
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:support")
async def admin_support(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    messages = await db.get_support_messages()
    if not messages:
        text = "🆘 <b>Поддержка</b>\n\n<i>Обращений пока нет.</i>"
    else:
        unread = sum(1 for m in messages if not m["is_read"])
        lines = []
        for m in messages[:20]:
            icon  = "🔴" if not m["is_read"] else "✅"
            uname = f"@{m['username']}" if m["username"] else str(m["telegram_id"])
            preview = m["message"][:60] + ("…" if len(m["message"]) > 60 else "")
            lines.append(
                f"{icon} <b>#{m['id']}</b> от {uname}\n"
                f"    <i>{preview}</i>\n"
                f"    <code>{m['created_at']}</code>"
            )
        text = (
            f"🆘 <b>Обращения</b>  "
            f"<i>(всего: {len(messages)}, непрочитанных: {unread})</i>\n\n"
            + "\n\n".join(lines)
        )
        if len(messages) > 20:
            text += f"\n\n<i>...и ещё {len(messages) - 20}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  ADMIN — BROADCAST
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Введите текст для всех пользователей.\n"
        "<i>Поддерживается HTML-форматирование.</i>",
        reply_markup=cancel_admin_kb(),
    )
    await state.set_state(BroadcastSt.entering_message)
    await callback.answer()


@router_admin.message(BroadcastSt.entering_message)
async def admin_broadcast_send(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    broadcast_text = message.text.strip()
    users = await db.get_all_users()
    sent = failed = 0
    status_msg = await message.answer(f"📤 Рассылка запущена... (0/{len(users)})")

    for user in users:
        try:
            await bot.send_message(
                user["telegram_id"],
                f"📢 <b>Сообщение от администратора</b>\n\n{broadcast_text}",
            )
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 10 == 0:
            try:
                await status_msg.edit_text(f"📤 Рассылка... ({sent + failed}/{len(users)})")
            except Exception:
                pass

    await state.clear()
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"<b>Отправлено:</b> {sent}\n"
        f"<b>Ошибок:</b>    {failed}\n"
        f"<b>Всего:</b>     {len(users)}",
        reply_markup=admin_back_kb(),
    )


# ──────────────────────────────────────────────────────────────
#  ADMIN — УПРАВЛЕНИЕ АДМИНАМИ
# ──────────────────────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:admins")
async def admin_admins_menu(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "🔑 <b>Управление администраторами</b>\n\n"
        "Выдача и отзыв прав доступа к панели администратора:",
        reply_markup=admin_admins_kb(),
    )
    await callback.answer()


@router_admin.callback_query(F.data == "admin:admins_list")
async def admin_admins_list(callback: CallbackQuery, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    admins = await db.get_all_admins()
    text = (
        f"📋 <b>Администраторы</b>\n\n"
        f"👑 Супер-админ (из .env): <code>{ADMIN_ID}</code>\n\n"
    )
    if not admins:
        text += "<i>Дополнительных администраторов нет.</i>"
    else:
        for a in admins:
            uname = f"@{a['username']}" if a["username"] else "<i>без username</i>"
            text += (
                f"  • {uname} — <code>{a['telegram_id']}</code>\n"
                f"    <i>Выдано: {a['created_at']}</i>\n"
            )
    await callback.message.edit_text(text, reply_markup=admin_admins_kb())
    await callback.answer()


# ── Выдача прав ──────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:grant_admin")
async def admin_grant_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ <b>Выдача прав администратора</b>\n\n"
        "Введите <b>@username</b> пользователя, которому хотите выдать права.\n\n"
        "<i>⚠️ Пользователь должен был хотя бы раз написать боту (/start).</i>",
        reply_markup=cancel_admin_kb(),
    )
    await state.set_state(GrantAdminSt.entering_username)
    await callback.answer()


@router_admin.message(GrantAdminSt.entering_username)
async def admin_grant_confirm(message: Message, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(message.from_user.id):
        return

    raw = message.text.strip().lstrip("@")  # принимаем и @username и username
    if not raw:
        await message.answer("❌ Введите корректный username.", reply_markup=cancel_admin_kb())
        return

    target = await db.get_user_by_username(raw)
    if not target:
        await message.answer(
            f"❌ Пользователь <b>@{raw}</b> не найден.\n\n"
            "Убедитесь, что он запускал бота командой /start.",
            reply_markup=cancel_admin_kb(),
        )
        return

    if target["telegram_id"] == ADMIN_ID:
        await message.answer(
            "ℹ️ Этот пользователь уже является супер-администратором.",
            reply_markup=cancel_admin_kb(),
        )
        return

    if await db.is_admin(target["telegram_id"]):
        await message.answer(
            f"ℹ️ @{raw} уже имеет права администратора.",
            reply_markup=admin_back_kb(),
        )
        await state.clear()
        return

    await db.add_admin(
        telegram_id=target["telegram_id"],
        username=target["username"],
        granted_by=message.from_user.id,
    )
    await state.clear()
    await message.answer(
        f"✅ <b>Права администратора выданы!</b>\n\n"
        f"Пользователь: @{raw}\n"
        f"Telegram ID: <code>{target['telegram_id']}</code>\n\n"
        "Теперь он может использовать /admin.",
        reply_markup=admin_back_kb(),
    )


# ── Отзыв прав ───────────────────────────────────────────────

@router_admin.callback_query(F.data == "admin:revoke_admin")
async def admin_revoke_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    admins = await db.get_all_admins()
    if not admins:
        await callback.message.edit_text(
            "ℹ️ Дополнительных администраторов нет.\n\n"
            "Отзывать права у супер-администратора из .env нельзя.",
            reply_markup=admin_admins_kb(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "➖ <b>Отзыв прав администратора</b>\n\n"
        "Введите <b>@username</b> администратора, у которого хотите отозвать права:\n\n"
        + "\n".join(
            f"  • @{a['username']} — <code>{a['telegram_id']}</code>"
            for a in admins
        ),
        reply_markup=cancel_admin_kb(),
    )
    await state.set_state(RevokeAdminSt.entering_username)
    await callback.answer()


@router_admin.message(RevokeAdminSt.entering_username)
async def admin_revoke_confirm(message: Message, state: FSMContext, db: Database) -> None:
    if not await db.is_admin(message.from_user.id):
        return

    raw = message.text.strip().lstrip("@")
    target = await db.get_user_by_username(raw)

    if not target:
        await message.answer(
            f"❌ Пользователь <b>@{raw}</b> не найден.",
            reply_markup=cancel_admin_kb(),
        )
        return

    if target["telegram_id"] == ADMIN_ID:
        await message.answer(
            "⛔ Нельзя отозвать права у супер-администратора из .env.",
            reply_markup=cancel_admin_kb(),
        )
        return

    if not await db.is_admin(target["telegram_id"]):
        await message.answer(
            f"ℹ️ @{raw} не является администратором.",
            reply_markup=cancel_admin_kb(),
        )
        return

    await db.remove_admin(target["telegram_id"])
    await state.clear()
    await message.answer(
        f"✅ <b>Права администратора отозваны.</b>\n\n"
        f"Пользователь: @{raw}\n"
        f"Telegram ID: <code>{target['telegram_id']}</code>",
        reply_markup=admin_back_kb(),
    )


# ══════════════════════════════════════════════════════════════ #
#  7. ENTRYPOINT                                                  #
# ══════════════════════════════════════════════════════════════ #

async def main() -> None:
    db = Database(DB_PATH)
    await db.init()
    logger.info("Database ready at %s", DB_PATH)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DatabaseMiddleware(db))

    # Порядок важен: admin первым (перехватывает /admin и FSM-состояния)
    dp.include_router(router_admin)
    dp.include_router(router_start)
    dp.include_router(router_wallets)
    dp.include_router(router_deals)
    dp.include_router(router_funds)
    dp.include_router(router_support)

    logger.info("Starting polling…")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())


# ══════════════════════════════════════════════════════════════ #
#  5. MIDDLEWARE                                                   #
# ══════════════════════════════════════════════════════════════ #

class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        return await handler(event, data)


# ══════════════════════════════════════════════════════════════ #
#  6. ROUTERS                                                     #
# ══════════════════════════════════════════════════════════════ #

router_start   = Router(name="start")
router_wallets = Router(name="wallets")
router_deals   = Router(name="deals")
router_funds   = Router(name="funds")
router_support = Router(name="support")
router_admin   = Router(name="admin")


# ──────────────────────────────────────────────────────────────
#  START & MAIN MENU
# ──────────────────────────────────────────────────────────────

@router_start.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await db.create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        text = (
            f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
            "✅ Вы успешно зарегистрированы.\n\n"
            "Выберите нужный раздел:"
        )
    else:
        text = (
            f"👋 С возвращением, <b>{message.from_user.first_name}</b>!\n\n"
            "Выберите нужный раздел:"
        )
    await message.answer(text, reply_markup=main_menu_kb())


@router_start.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите нужный раздел:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  WALLETS
# ──────────────────────────────────────────────────────────────

@router_wallets.callback_query(F.data == "wallets:menu")
async def wallets_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👛 <b>Управление кошельками</b>\n\nВыберите действие:",
        reply_markup=wallets_menu_kb(),
    )
    await callback.answer()


@router_wallets.callback_query(F.data == "wallets:add")
async def wallet_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "➕ <b>Добавление кошелька</b>\n\nВыберите тип:",
        reply_markup=wallet_types_kb(),
    )
    await state.set_state(AddWalletSt.choosing_type)
    await callback.answer()


@router_wallets.callback_query(AddWalletSt.choosing_type, F.data.startswith("wallet_type:"))
async def wallet_type_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    wtype = callback.data.split(":")[1]
    await state.update_data(wallet_type=wtype)
    await callback.message.edit_text(
        f"<b>Тип:</b> {WALLET_TYPES[wtype]}\n\n{WALLET_PROMPTS[wtype]}",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AddWalletSt.entering_data)
    await callback.answer()


@router_wallets.message(AddWalletSt.entering_data)
async def wallet_data_entered(message: Message, state: FSMContext, db: Database) -> None:
    raw = message.text.strip()
    data = await state.get_data()
    wtype: str = data["wallet_type"]

    # Basic validation
    if wtype == "card" and (not raw.isdigit() or len(raw) != 16):
        await message.answer(
            "❌ Номер карты должен содержать ровно <b>16 цифр</b>.\nПопробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return
    if wtype == "ton" and not (raw.startswith("UQ") or raw.startswith("EQ")):
        await message.answer(
            "❌ Адрес TON должен начинаться с <b>UQ</b> или <b>EQ</b>.\nПопробуйте ещё раз:",
            reply_markup=cancel_kb(),
        )
        return

    user = await db.get_user(message.from_user.id)
    await db.add_wallet(user_id=user["id"], wtype=wtype, data=raw)
    await state.clear()
    await message.answer(
        f"✅ <b>Кошелёк добавлен!</b>\n\n"
        f"<b>Тип:</b>    {WALLET_TYPES[wtype]}\n"
        f"<b>Данные:</b> <code>{raw}</code>",
        reply_markup=back_menu_kb(),
    )


@router_wallets.callback_query(F.data == "wallets:list")
async def wallets_list(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    if not wallets:
        await callback.message.edit_text(
            "📋 <b>Мои кошельки</b>\n\nКошельков пока нет. Добавьте первый!",
            reply_markup=wallets_menu_kb(),
        )
    else:
        await callback.message.edit_text(
            f"📋 <b>Мои кошельки</b>  ({len(wallets)} шт.)\n\n"
            "Нажмите на кошелёк для деталей:",
            reply_markup=wallet_list_kb(wallets),
        )
    await callback.answer()


@router_wallets.callback_query(F.data.startswith("wallet:view:"))
async def wallet_view(callback: CallbackQuery, db: Database) -> None:
    wallet_id = int(callback.data.split(":")[2])
    w = await db.get_wallet(wallet_id)
    if not w:
        await callback.answer("Кошелёк не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"👛 <b>Кошелёк #{w['id']}</b>\n\n"
        f"<b>Тип:</b>      {WALLET_TYPES.get(w['type'], w['type'])}\n"
        f"<b>Данные:</b>   <code>{w['data']}</code>\n"
        f"<b>Добавлен:</b> {w['created_at']}",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  DEALS
# ──────────────────────────────────────────────────────────────

@router_deals.callback_query(F.data == "deals:create")
async def create_deal_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    if not wallets:
        await callback.message.edit_text(
            "❌ <b>Нет кошельков</b>\n\n"
            "Для создания сделки нужен хотя бы один кошелёк.\n"
            "Добавьте кошелёк в разделе <b>Управление кошельками</b>.",
            reply_markup=back_menu_kb(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "🤝 <b>Создание сделки</b>\n\nВыберите вашу роль:",
        reply_markup=deal_roles_kb(),
    )
    await state.set_state(CreateDealSt.choosing_role)
    await callback.answer()


@router_deals.callback_query(CreateDealSt.choosing_role, F.data.startswith("deal_role:"))
async def deal_role_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    role = callback.data.split(":")[1]
    await state.update_data(role=role)
    await callback.message.edit_text(
        f"<b>Роль:</b> {ROLE_LABELS[role]}\n\n"
        "💵 Введите сумму сделки (например: <code>1500</code> или <code>99.99</code>):",
        reply_markup=cancel_kb(),
    )
    await state.set_state(CreateDealSt.entering_amount)
    await callback.answer()


@router_deals.message(CreateDealSt.entering_amount)
async def deal_amount_entered(message: Message, state: FSMContext, db: Database) -> None:
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Введите корректную сумму — положительное число.\n"
            "Например: <code>500</code> или <code>49.90</code>:",
            reply_markup=cancel_kb(),
        )
        return

    await state.update_data(amount=amount)
    user = await db.get_user(message.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    await message.answer(
        f"<b>Сумма:</b> {amount:.2f}\n\n"
        "👛 Выберите кошелёк для сделки:",
        reply_markup=choose_wallet_kb(wallets),
    )
    await state.set_state(CreateDealSt.choosing_wallet)


@router_deals.callback_query(CreateDealSt.choosing_wallet, F.data.startswith("deal_wallet:"))
async def deal_wallet_chosen(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    wallet_id = int(callback.data.split(":")[1])
    fsm = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    deal_id = await db.create_deal(
        creator_id=user["id"],
        role=fsm["role"],
        amount=fsm["amount"],
        wallet_id=wallet_id,
    )
    wallet = await db.get_wallet(wallet_id)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Сделка #{deal_id} создана!</b>\n\n"
        f"<b>Роль:</b>    {ROLE_LABELS[fsm['role']]}\n"
        f"<b>Сумма:</b>   {fsm['amount']:.2f}\n"
        f"<b>Кошелёк:</b> {WALLET_TYPES.get(wallet['type'], wallet['type'])}\n"
        f"<b>Статус:</b>  {DEAL_STATUSES['created']}\n\n"
        f"⏳ Ожидаем второго участника.\n"
        f"Поделитесь ID сделки: <code>{deal_id}</code>",
        reply_markup=deal_actions_kb(deal_id, "created"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:join:"))
async def deal_join(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] == user["id"]:
        await callback.answer("Нельзя присоединиться к собственной сделке.", show_alert=True)
        return
    if deal["partner_id"]:
        await callback.answer("В сделке уже есть второй участник.", show_alert=True)
        return
    if deal["status"] != "created":
        await callback.answer("Сделка недоступна для присоединения.", show_alert=True)
        return
    await db.join_deal(deal_id, user["id"])
    await callback.message.edit_text(
        f"✅ <b>Вы присоединились к сделке #{deal_id}</b>\n\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n"
        f"<b>Статус:</b> {DEAL_STATUSES['waiting_payment']}\n\n"
        "Подтвердите оплату после перевода средств.",
        reply_markup=deal_actions_kb(deal_id, "waiting_payment"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:pay:"))
async def deal_pay(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "waiting_payment":
        await callback.answer("Невозможно подтвердить оплату.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "paid")
    await callback.message.edit_text(
        f"💰 <b>Оплата по сделке #{deal_id} подтверждена!</b>\n\n"
        f"<b>Статус:</b> {DEAL_STATUSES['paid']}\n\n"
        "Получатель должен подтвердить получение средств.",
        reply_markup=deal_actions_kb(deal_id, "paid"),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:complete:"))
async def deal_complete(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "paid":
        await callback.answer("Завершить можно только оплаченную сделку.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "completed")
    await callback.message.edit_text(
        f"🎉 <b>Сделка #{deal_id} успешно завершена!</b>\n\n"
        f"<b>Сумма:</b>  {deal['amount']:.2f}\n"
        f"<b>Статус:</b> {DEAL_STATUSES['completed']}\n\n"
        "Спасибо за использование нашего сервиса!",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


@router_deals.callback_query(F.data.startswith("deal:cancel:"))
async def deal_cancel(callback: CallbackQuery, db: Database) -> None:
    deal_id = int(callback.data.split(":")[2])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] in ("completed", "cancelled"):
        await callback.answer("Эту сделку уже невозможно отменить.", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if deal["creator_id"] != user["id"] and deal["partner_id"] != user["id"]:
        await callback.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    await db.update_deal_status(deal_id, "cancelled")
    await callback.message.edit_text(
        f"❌ <b>Сделка #{deal_id} отменена.</b>\n\n"
        "По вопросам обратитесь в поддержку.",
        reply_markup=back_menu_kb(),
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  FUNDS
# ──────────────────────────────────────────────────────────────

@router_funds.callback_query(F.data == "funds:view")
async def funds_view(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    wallets = await db.get_user_wallets(user["id"])
    deals   = await db.get_user_deals(user["id"])

    text = "💼 <b>Средства</b>\n\n"

    # Wallets summary
    if wallets:
        text += f"<b>👛 Кошельков:</b> {len(wallets)}\n"
        for w in wallets:
            text += f"  • {WALLET_TYPES.get(w['type'], w['type'])}: <code>{w['data'][:25]}</code>\n"
    else:
        text += "👛 <i>Кошельков нет</i>\n"

    text += "\n"

    # Deals summary
    if not deals:
        text += "📭 <i>Сделок пока нет</i>"
    else:
        active = [d for d in deals if d["status"] not in ("completed", "cancelled")]
        closed = [d for d in deals if d["status"] in ("completed", "cancelled")]
        if active:
            text += f"<b>🔄 Активные ({len(active)}):</b>\n"
            for d in active:
                icon = "🛒" if d["role"] == "buyer" else "🏪"
                text += f"  {icon} <code>#{d['id']}</code> — {d['amount']:.2f} — {DEAL_STATUSES.get(d['status'], d['status'])}\n"
        if closed:
            text += f"\n<b>📁 Завершённые ({len(closed)}):</b>\n"
            for d in closed[:5]:
                icon = "🛒" if d["role"] == "buyer" else "🏪"
                text += f"  {icon} <code>#{d['id']}</code> — {d['amount']:.2f} — {DEAL_STATUSES.get(d['status'], d['status'])}\n"

    await callback.message.edit_text(text, reply_markup=back_menu_kb())
    await callback.answer()


# ──────────────────────────────────────────────────────────────
#  SUPPORT
# ──────────────────────────────────────────────────────────────

@router_support.callback_query(F.data == "support:start")
async def support_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text(
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите вашу проблему или вопрос.\n"
        "Администратор ответит в ближайшее время:",
        reply_markup=cancel_kb(),
    )
    await state.set_state(SupportSt.entering_message)
    await callback.answer()


@router_support.message(SupportSt.entering_message)
async def support_message_received(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    user = await db.get_user(message.from_user.id)
    await db.add_support_message(user_id=user["id"], message=message.text.strip())

    uname = f" (@{message.from_user.username})" if message.from_user.username else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆘 <b>Новое обращение</b>\n\n"
            f"<b>От:</b> {message.from_user.first_name}{uname}\n"
            f"<b>ID:</b> <code>{message.from_user.id}</code>\n\n"
            f"<b>Сообщение:</b>\n{message.text.strip()}",
        )
    except Exception:
        pass

    await state.clear()
    await message.answer(
        "✅ <b>Сообщение отправлено!</b>\n\n"
        "Ваше обращение принято. Мы ответим в ближайшее время.",
        reply_markup=back_menu_kb(),
    )


# ──────────────────────────────────────────────────────────────
#  ADMIN
# ──────────────────────────────────────────────────────────────

def _only_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@router_admin.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not _only_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await message.answer(
        "🔑 <b>Панель администратора</b>\n\nВыберите раздел:",
        reply_markup=admin_menu_kb(),
    )


@router_admin.callback_query(F.data == "admin:menu")
async def admin_menu_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🔑 <b>Панель администратора</b>\n\nВыберите раздел:",
        reply_markup=admin_menu_kb(),
    )
    await callback.answer()


@router_admin.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, db: Database) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    users = await db.get_all_users()
    if not users:
        text = "👥 <b>Пользователи</b>\n\n<i>Пока никого нет.</i>"
    else:
        lines = []
        for u in users[:30]:
            uname = f"@{u['username']}" if u["username"] else "<i>без username</i>"
            lines.append(
                f"  • <a href='tg://user?id={u['telegram_id']}'>{u['first_name']}</a> "
                f"({uname}) — <code>{u['telegram_id']}</code>"
            )
        text = f"👥 <b>Пользователи</b>  <i>({len(users)} всего)</i>\n\n" + "\n".join(lines)
        if len(users) > 30:
            text += f"\n\n<i>...и ещё {len(users) - 30}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


@router_admin.callback_query(F.data == "admin:wallets")
async def admin_wallets(callback: CallbackQuery, db: Database) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    wallets = await db.get_all_wallets()
    if not wallets:
        text = "👛 <b>Кошельки</b>\n\n<i>Кошельков пока нет.</i>"
    else:
        lines = []
        for w in wallets[:30]:
            wtype = WALLET_TYPES.get(w["type"], w["type"])
            uname = f"@{w['username']}" if w["username"] else str(w["telegram_id"])
            lines.append(f"  • {wtype}: <code>{w['data'][:25]}</code> — {uname}")
        text = f"👛 <b>Все кошельки</b>  <i>({len(wallets)} всего)</i>\n\n" + "\n".join(lines)
        if len(wallets) > 30:
            text += f"\n\n<i>...и ещё {len(wallets) - 30}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


@router_admin.callback_query(F.data == "admin:deals")
async def admin_deals_menu(callback: CallbackQuery) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 <b>Сделки</b>\n\nВыберите фильтр:",
        reply_markup=admin_deals_filter_kb(),
    )
    await callback.answer()


@router_admin.callback_query(F.data.startswith("admin:deals_list:"))
async def admin_deals_list(callback: CallbackQuery, db: Database) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    status_filter = callback.data.split(":")[2]
    if status_filter == "all":
        deals = await db.get_all_deals()
        title = "Все сделки"
    else:
        deals = await db.get_deals_by_status(status_filter)
        title = DEAL_STATUSES.get(status_filter, status_filter)
    if not deals:
        text = f"🤝 <b>{title}</b>\n\n<i>По этому фильтру ничего нет.</i>"
    else:
        lines = []
        for d in deals[:25]:
            sl = DEAL_STATUSES.get(d["status"], d["status"])
            partner = f"u#{d['partner_id']}" if d["partner_id"] else "ожидает"
            lines.append(
                f"  <code>#{d['id']}</code> — {d['amount']:.2f} — {sl}\n"
                f"    👤 u#{d['creator_id']} ↔ {partner}"
            )
        text = f"🤝 <b>{title}</b>  <i>({len(deals)} шт.)</i>\n\n" + "\n".join(lines)
        if len(deals) > 25:
            text += f"\n\n<i>...и ещё {len(deals) - 25}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


@router_admin.callback_query(F.data == "admin:support")
async def admin_support(callback: CallbackQuery, db: Database) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    messages = await db.get_support_messages()
    if not messages:
        text = "🆘 <b>Поддержка</b>\n\n<i>Обращений пока нет.</i>"
    else:
        unread = sum(1 for m in messages if not m["is_read"])
        lines = []
        for m in messages[:20]:
            icon  = "🔴" if not m["is_read"] else "✅"
            uname = f"@{m['username']}" if m["username"] else str(m["telegram_id"])
            preview = m["message"][:60] + ("…" if len(m["message"]) > 60 else "")
            lines.append(
                f"{icon} <b>#{m['id']}</b> от {uname}\n"
                f"    <i>{preview}</i>\n"
                f"    <code>{m['created_at']}</code>"
            )
        text = (
            f"🆘 <b>Обращения</b>  <i>(всего: {len(messages)}, непрочитанных: {unread})</i>\n\n"
            + "\n\n".join(lines)
        )
        if len(messages) > 20:
            text += f"\n\n<i>...и ещё {len(messages) - 20}</i>"
    await callback.message.edit_text(text, reply_markup=admin_back_kb())
    await callback.answer()


@router_admin.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _only_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Введите текст сообщения для всех пользователей.\n"
        "<i>Поддерживается HTML-форматирование.</i>",
        reply_markup=cancel_admin_kb(),
    )
    await state.set_state(BroadcastSt.entering_message)
    await callback.answer()


@router_admin.message(BroadcastSt.entering_message)
async def admin_broadcast_send(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    if not _only_admin(message.from_user.id):
        return
    broadcast_text = message.text.strip()
    users = await db.get_all_users()
    sent = failed = 0
    status_msg = await message.answer(f"📤 Рассылка запущена... (0/{len(users)})")

    for user in users:
        try:
            await bot.send_message(
                user["telegram_id"],
                f"📢 <b>Сообщение от администратора</b>\n\n{broadcast_text}",
            )
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 10 == 0:
            try:
                await status_msg.edit_text(f"📤 Рассылка... ({sent + failed}/{len(users)})")
            except Exception:
                pass

    await state.clear()
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"<b>Отправлено:</b> {sent}\n"
        f"<b>Ошибок:</b>    {failed}\n"
        f"<b>Всего:</b>     {len(users)}",
        reply_markup=admin_back_kb(),
    )


# ══════════════════════════════════════════════════════════════ #
#  7. ENTRYPOINT                                                  #
# ══════════════════════════════════════════════════════════════ #

async def main() -> None:
    db = Database(DB_PATH)
    await db.init()
    logger.info("Database ready at %s", DB_PATH)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Inject db into every handler via middleware
    dp.update.middleware(DatabaseMiddleware(db))

    # Register routers (admin first so /admin command takes priority)
    dp.include_router(router_admin)
    dp.include_router(router_start)
    dp.include_router(router_wallets)
    dp.include_router(router_deals)
    dp.include_router(router_funds)
    dp.include_router(router_support)

    logger.info("Starting polling…")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
