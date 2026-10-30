import asyncio
import html
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, TelegramObject, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    FloodWait,
    Unauthorized,
    PeerIdInvalid,
    UsernameNotOccupied,
    RPCError,
)
from pyrogram.raw import functions, types

from config import settings
from database import (
    init_db, add_account, get_accounts, get_session, remove_account,
    count_accounts, save_complaint, get_history, mark_invalid,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ------------------------------------------------------------------
# Причины жалоб + шаблоны текстов
# ------------------------------------------------------------------
REASONS = {
    "spam":             ("📨 Спам", types.InputReportReasonSpam),
    "violence":         ("💪 Насилие", types.InputReportReasonViolence),
    "pornography":      ("🔞 Порнография", types.InputReportReasonPornography),
    "child_abuse":      ("🧒 Жестокое обращение с детьми", types.InputReportReasonChildAbuse),
    "illegal_drugs":    ("💊 Незаконные наркотики", types.InputReportReasonIllegalDrugs),
    "personal_details": ("👤 Личные данные", types.InputReportReasonPersonalDetails),
    "copyright":        ("©️ Авторские права", types.InputReportReasonCopyright),
    "other":            ("❓ Другое", types.InputReportReasonOther),
}

# Шаблоны текстов для каждой причины
REASON_TEMPLATES = {
    "spam": [
        "Аккаунт массово рассылает рекламные сообщения и спам.",
        "Рассылка фишинговых ссылок с целью обмана пользователей.",
        "Автоматизированная рассылка нежелательных сообщений.",
        "Навязчивая реклама запрещённых товаров и услуг.",
    ],
    "violence": [
        "Пропаганда насилия и призывы к жестокости.",
        "Угрозы физической расправы в адрес пользователей.",
        "Публикация материалов, оправдывающих терроризм.",
        "Разжигание ненависти и призывы к насилию.",
    ],
    "pornography": [
        "Публикация порнографического контента без предупреждения.",
        "Распространение запрещённого порнографического материала.",
        "Незаконный эротический контент в открытом доступе.",
        "Порнографический контент, нарушающий правила платформы.",
    ],
    "child_abuse": [
        "Подозрительный контент с участием несовершеннолетних.",
        "Материалы, эксплуатирующие детей.",
        "Попытки вовлечения несовершеннолетних в опасную деятельность.",
        "Контент сексуального характера с участием детей.",
    ],
    "illegal_drugs": [
        "Пропаганда и продажа запрещённых наркотических веществ.",
        "Инструкции по изготовлению наркотиков.",
        "Реклама наркотических средств и психотропных веществ.",
        "Координация распространения запрещённых препаратов.",
    ],
    "personal_details": [
        "Публикация персональных данных без согласия человека.",
        "Распространение чужих телефонов, адресов и документов.",
        "Доксинг — публикация личной информации с целью травли.",
        "Утечка конфиденциальных данных третьих лиц.",
    ],
    "copyright": [
        "Нарушение авторских прав, распространение пиратского контента.",
        "Незаконное использование чужих материалов без разрешения.",
        "Публикация защищённого авторским правом контента.",
        "Плагиат и выдача чужих работ за свои.",
    ],
    "other": [
        "Сообщение нарушает правила Telegram и пользовательское соглашение.",
        "Данный контент противоречит условиям использования платформы.",
        "Мошенничество и введение пользователей в заблуждение.",
        "Координация незаконной деятельности.",
    ],
}

STATUS_TEXT = {
    "success": "✅ Отправлено",
    "failed":  "⚠️ Telegram вернул false",
    "error":   "❌ Ошибка",
    "flood":   "🕒 FloodWait (лимит Telegram)",
    "invalid": "🔴 Сессия недействительна",
}


# ------------------------------------------------------------------
# Состояния
# ------------------------------------------------------------------
class Auth(StatesGroup):
    phone = State()
    code = State()
    password = State()


class Report(StatesGroup):
    mode = State()
    single_account = State()
    multi_account = State()
    link = State()
    reason = State()
    template_choice = State()   # НОВОЕ: выбор шаблона
    custom_comment = State()    # НОВОЕ: ввод своего текста
    confirm = State()


# ------------------------------------------------------------------
# Очереди входа
# ------------------------------------------------------------------
@dataclass
class PendingLogin:
    client: Client
    phone: str
    phone_code_hash: str
    created_at: float = field(default_factory=time.time)
    hint: str = ""


pending: dict[tuple[int, str], PendingLogin] = {}

bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher(storage=MemoryStorage())


# ------------------------------------------------------------------
# Доступ (опциональный вайтлист)
# ------------------------------------------------------------------
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        allowed = settings.allowed_ids
        user = getattr(event, "from_user", None)
        if allowed and (user is None or user.id not in allowed):
            if isinstance(event, Message):
                await event.answer("⛔ Бот закрыт для посторонних.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Бот закрыт для посторонних.", show_alert=True)
            return None
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


# ------------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------------
def esc(text: str) -> str:
    return html.escape(text or "")


def parse_link(link: str) -> tuple[str, int] | None:
    link = link.strip()
    m = re.match(r"^https?://t\.me/", link)
    if m:
        link = link[m.end():]
    link = link.lstrip("/")

    m = re.match(r"^c/(\d+)/(\d+)", link)
    if m:
        return f"-100{m.group(1)}", int(m.group(2))

    m = re.match(r"^([A-Za-z][A-Za-z0-9_]{4,31})/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))

    return None


def make_reason(key: str, comment: str):
    cls = REASONS[key][1]
    if key == "other":
        try:
            return cls(text=comment or "Нарушение правил Telegram")
        except TypeError:
            return cls()
    return cls()


async def drop_pending(owner_id: int, phone: str):
    item = pending.pop((owner_id, phone), None)
    if item:
        try:
            await item.client.disconnect()
        except Exception:
            pass


async def clean_pending_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for key, item in list(pending.items()):
            if now - item.created_at > 600:
                await drop_pending(*key)


# ------------------------------------------------------------------
# Веб-сервер для Render (Health Check)
# ------------------------------------------------------------------
async def health_check(request):
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Веб-сервер запущен на порту {port}")
    return runner


# ------------------------------------------------------------------
# Старт / справка
# ------------------------------------------------------------------
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "🛡 <b>Бот жалоб на сообщения</b>\n\n"
        "1. /add_account — подключить свой Telegram-аккаунт\n"
        "2. /complain — подать жалобу\n\n"
        "<b>Команды</b>\n"
        "/add_account — добавить аккаунт\n"
        "/list_accounts — мои аккаунты\n"
        "/remove_account — удалить аккаунт\n"
        "/complain — подать жалобу\n"
        "/history — последние жалобы\n\n"
        "⚠️ Жалобы уходят от имени подключённого аккаунта.\n"
        "Не злоупотребляйте: Telegram банит за спам жалобами."
    )


# ------------------------------------------------------------------
# Добавление аккаунта
# ------------------------------------------------------------------
@dp.message(Command("add_account"))
async def cmd_add_account(message: Message, state: FSMContext):
    if await count_accounts(message.from_user.id) >= settings.MAX_ACCOUNTS:
        await message.answer(f"❌ Лимит: {settings.MAX_ACCOUNTS} аккаунтов.")
        return

    await message.answer(
        "📱 <b>Подключение аккаунта</b>\n\n"
        "Отправь номер этого аккаунта в международном формате:\n"
        "<code>+79991234567</code>\n\n"
        "❗ Код придёт <b>в приложение Telegram</b> на этот номер."
    )
    await state.set_state(Auth.phone)


@dp.message(Auth.phone)
async def auth_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not re.match(r"^\+[1-9]\d{7,14}$", phone):
        await message.answer("❌ Неверный формат. Пример: +79991234567")
        return

    owner = message.from_user.id
    await drop_pending(owner, phone)

    client = Client(
        name=f"login_{owner}_{phone}",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        in_memory=True,
        no_updates=True,
    )

    try:
        await client.connect()
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        await client.disconnect()
        await message.answer("❌ Telegram говорит, что номер не существует.")
        await state.clear()
        return
    except FloodWait as e:
        await client.disconnect()
        await message.answer(f"🕒 Слишком много запросов. Подожди {e.value} сек.")
        await state.clear()
        return
    except Exception as e:
        await client.disconnect()
        log.exception("send_code")
        await message.answer(f"❌ Не удалось отправить код: <code>{esc(str(e))}</code>")
        await state.clear()
        return

    pending[(owner, phone)] = PendingLogin(client, phone, sent.phone_code_hash)
    await state.set_state(Auth.code)
    await state.update_data(phone=phone)
    await message.answer("📩 Код отправлен. Пришли его цифрами (например: <code>48291</code>).")


@dp.message(Auth.code)
async def auth_code(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    owner = message.from_user.id
    item = pending.get((owner, phone))

    if not item:
        await message.answer("❌ Сессия входа истекла. Начни заново: /add_account")
        await state.clear()
        return

    if time.time() - item.created_at > 600:
        await drop_pending(owner, phone)
        await message.answer("❌ Прошло больше 10 минут. Начни заново: /add_account")
        await state.clear()
        return

    code = message.text.strip()
    if not re.match(r"^\d{4,6}$", code):
        await message.answer("❌ Код — это 4–6 цифр.")
        return

    try:
        await item.client.sign_in(
            phone_number=phone,
            phone_code_hash=item.phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        try:
            pw = await item.client.invoke(functions.account.GetPassword())
            item.hint = getattr(pw, "hint", "") or ""
        except Exception:
            pass
        await state.set_state(Auth.password)
        await message.answer(
            "🔐 Включено 2FA (облачный пароль).\n"
            f"Подсказка: {esc(item.hint) or 'нет'}\n"
            "Пришли <b>облачный пароль</b>, не код."
        )
        return
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await drop_pending(owner, phone)
        await state.clear()
        await message.answer("❌ Неверный или устаревший код. Начни заново: /add_account")
        return
    except Exception as e:
        await drop_pending(owner, phone)
        await state.clear()
        await message.answer(f"❌ Ошибка входа: <code>{esc(str(e))}</code>")
        return

    await finish_login(message, state, owner, phone, item.client)


@dp.message(Auth.password)
async def auth_password(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    owner = message.from_user.id
    item = pending.get((owner, phone))

    if not item:
        await message.answer("❌ Сессия входа истекла. /add_account")
        await state.clear()
        return

    try:
        await item.client.check_password(message.text)
    except Exception as e:
        await message.answer(f"❌ Неверный пароль ({type(e).__name__}). Попробуй ещё раз.")
        return

    await finish_login(message, state, owner, phone, item.client)


async def finish_login(message: Message, state: FSMContext, owner: int, phone: str, client: Client):
    try:
        me = await client.get_me()
        session = await client.export_session_string()
        ok = await add_account(
            owner, me.id, phone, me.first_name or "", session
        )
        if not ok:
            await message.answer("❌ Не удалось сохранить аккаунт в базу.")
        else:
            await message.answer(
                f"✅ Аккаунт подключён!\n"
                f"Имя: {esc(me.first_name or '')}\n"
                f"Номер: {phone}\n\n"
                "Теперь можно: /complain"
            )
    except Exception as e:
        log.exception("finish_login")
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
    finally:
        await drop_pending(owner, phone)
        await state.clear()


# ------------------------------------------------------------------
# Список / удаление аккаунтов
# ------------------------------------------------------------------
@dp.message(Command("list_accounts"))
async def cmd_list(message: Message):
    accounts = await get_accounts(message.from_user.id)
    if not accounts:
        await message.answer("📭 Аккаунтов нет. Добавь: /add_account")
        return
    text = "📱 <b>Твои аккаунты:</b>\n\n"
    for i, (phone, name, _) in enumerate(accounts, 1):
        text += f"{i}. {phone} — {esc(name)}\n"
    await message.answer(text)


@dp.message(Command("remove_account"))
async def cmd_remove(message: Message):
    accounts = await get_accounts(message.from_user.id)
    if not accounts:
        await message.answer("📭 Аккаунтов нет.")
        return
    kb = InlineKeyboardBuilder()
    for phone, _, _ in accounts:
        kb.button(text=f"🗑 {phone}", callback_data=f"del:{phone}")
    kb.adjust(1)
    await message.answer("Выбери аккаунт для удаления:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    phone = callback.data.split(":", 1)[1]
    ok = await remove_account(callback.from_user.id, phone)
    await callback.message.edit_text(
        f"✅ Аккаунт {phone} удалён." if ok else "❌ Не найдено."
    )
    await callback.answer()


# ------------------------------------------------------------------
# Жалоба: выбор режима
# ------------------------------------------------------------------
@dp.message(Command("complain"))
async def cmd_complain(message: Message, state: FSMContext):
    accounts = await get_accounts(message.from_user.id)
    if not accounts:
        await message.answer("📭 Сначала подключи аккаунт: /add_account")
        return

    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 С одного аккаунта", callback_data="mode:single")
    kb.button(text="🚀 СРАЗУ СО ВСЕХ", callback_data="mode:all")
    kb.button(text="☑️ Выбрать несколько", callback_data="mode:select")
    kb.adjust(1)

    await state.set_state(Report.mode)
    await message.answer(
        f"🛡 Аккаунтов: <b>{len(accounts)}</b>\nВыбери режим подачи:",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(Report.mode, F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    accounts = await get_accounts(callback.from_user.id)
    await state.update_data(all_phones=[a[0] for a in accounts])

    if mode == "all":
        await state.update_data(selected=[a[0] for a in accounts])
        await ask_link(callback, state)

    elif mode == "single":
        kb = InlineKeyboardBuilder()
        for phone, name, _ in accounts:
            kb.button(text=f"📱 {phone} ({name})", callback_data=f"one:{phone}")
        kb.adjust(1)
        await state.set_state(Report.single_account)
        await callback.message.edit_text("Выбери один аккаунт:", reply_markup=kb.as_markup())

    else:
        await state.update_data(selected=[])
        await state.set_state(Report.multi_account)
        await callback.message.edit_text(
            "Отметь нужные аккаунты и нажми «Готово»:",
            reply_markup=multi_kb(accounts, []),
        )

    await callback.answer()


def multi_kb(accounts, selected):
    kb = InlineKeyboardBuilder()
    for phone, _, _ in accounts:
        mark = "☑️" if phone in selected else "⬜"
        kb.button(text=f"{mark} {phone}", callback_data=f"pick:{phone}")
    kb.button(text="✅ ГОТОВО", callback_data="pick:done")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(Report.single_account, F.data.startswith("one:"))
async def cb_one(callback: CallbackQuery, state: FSMContext):
    phone = callback.data.split(":", 1)[1]
    await state.update_data(selected=[phone])
    await callback.message.edit_text(f"Выбран аккаунт: {phone}")
    await ask_link(callback, state)
    await callback.answer()


@dp.callback_query(Report.multi_account, F.data.startswith("pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("selected", []))

    if value != "done":
        if value in selected:
            selected.remove(value)
        else:
            selected.append(value)
        await state.update_data(selected=selected)
        accounts = await get_accounts(callback.from_user.id)
        await callback.message.edit_reply_markup(reply_markup=multi_kb(accounts, selected))
        await callback.answer()
        return

    if not selected:
        await callback.answer("Отметь хотя бы один аккаунт!", show_alert=True)
        return

    await callback.message.edit_text(f"Выбрано аккаунтов: {len(selected)}")
    await ask_link(callback, state)
    await callback.answer()


# ------------------------------------------------------------------
# Жалоба: ссылка
# ------------------------------------------------------------------
async def ask_link(event, state: FSMContext):
    await state.set_state(Report.link)
    text = (
        "🔗 Отправь ссылку на сообщение:\n"
        "<code>https://t.me/channel/123</code>\n\n"
        "Также работает <code>t.me/c/123456/123</code> — "
        "но аккаунт должен состоять в этом приватном канале."
    )
    if isinstance(event, CallbackQuery):
        await event.message.answer(text)
    else:
        await event.answer(text)


@dp.message(Report.link)
async def report_link(message: Message, state: FSMContext):
    parsed = parse_link(message.text)
    if not parsed:
        await message.answer("❌ Не понял ссылку. Пример: https://t.me/channel/123")
        return

    await state.update_data(target=message.text.strip(), parsed=parsed)

    kb = InlineKeyboardBuilder()
    for key, (label, _) in REASONS.items():
        kb.button(text=label, callback_data=f"reason:{key}")
    kb.adjust(1)

    await state.set_state(Report.reason)
    await message.answer("Выбери причину:", reply_markup=kb.as_markup())


# ------------------------------------------------------------------
# Жалоба: причина + шаблоны
# ------------------------------------------------------------------
@dp.callback_query(Report.reason, F.data.startswith("reason:"))
async def report_reason(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in REASONS:
        await callback.answer()
        return
    
    await state.update_data(reason_key=key)
    
    # Показываем шаблоны
    templates = REASON_TEMPLATES.get(key, [])
    kb = InlineKeyboardBuilder()
    
    for i, template in enumerate(templates):
        # Обрезаем длинные шаблоны для кнопки
        short_text = template[:50] + "..." if len(template) > 50 else template
        kb.button(text=short_text, callback_data=f"tpl:{i}")
    
    kb.button(text="✏️ Написать свой текст", callback_data="tpl:custom")
    kb.button(text="⏭️ Без комментария", callback_data="tpl:none")
    kb.adjust(1)
    
    await state.set_state(Report.template_choice)
    
    templates_text = "\n".join([f"{i+1}. {t}" for i, t in enumerate(templates)])
    
    await callback.message.answer(
        f"<b>Причина:</b> {REASONS[key][0]}\n\n"
        f"<b>Выбери шаблон жалобы:</b>\n{templates_text}",
        reply_markup=kb.as_markup()
    )
    await callback.answer()


@dp.callback_query(Report.template_choice, F.data.startswith("tpl:"))
async def report_template(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    reason_key = data.get("reason_key")
    templates = REASON_TEMPLATES.get(reason_key, [])
    
    if action == "custom":
        await state.set_state(Report.custom_comment)
        await callback.message.answer(
            "✏️ Напиши свой текст жалобы (до 512 символов):"
        )
        await callback.answer()
        return
    
    if action == "none":
        await state.update_data(comment="")
        await show_confirm(callback.message, state)
        await callback.answer()
        return
    
    # Выбран шаблон
    try:
        index = int(action)
        if 0 <= index < len(templates):
            comment = templates[index]
            await state.update_data(comment=comment)
            await show_confirm(callback.message, state)
    except (ValueError, IndexError):
        pass
    
    await callback.answer()


@dp.message(Report.custom_comment)
async def report_custom_comment(message: Message, state: FSMContext):
    text = message.text.strip()
    comment = "" if text == "0" else text[:512]
    await state.update_data(comment=comment)
    await show_confirm(message, state)


async def show_confirm(message: Message, state: FSMContext):
    d = await state.get_data()
    reason = REASONS[d["reason_key"]][0]
    selected = d.get("selected", [])

    text = (
        "📋 <b>Проверь и подтверди</b>\n\n"
        f"Ссылка: <code>{esc(d['target'])}</code>\n"
        f"Причина: {reason}\n"
        f"Комментарий: {esc(d.get('comment') or '—')}\n\n"
        f"Аккаунтов: <b>{len(selected)}</b>\n"
        + "\n".join(f"• {p}" for p in selected)
        + "\n\n⚠️ Это действие отправит реальные жалобы в Telegram."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ ОТПРАВИТЬ", callback_data="send:yes")
    kb.button(text="❌ Отмена", callback_data="send:no")
    kb.adjust(1)

    await state.set_state(Report.confirm)
    await message.answer(text, reply_markup=kb.as_markup())


# ------------------------------------------------------------------
# Жалоба: отправка
# ------------------------------------------------------------------
@dp.callback_query(Report.confirm, F.data.startswith("send:"))
async def send_reports(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data.split(":")[1] == "no":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        return

    d = await state.get_data()
    await state.clear()

    selected = d.get("selected", [])
    target = d["target"]
    identifier, msg_id = d["parsed"]
    reason_key = d["reason_key"]
    reason_label = REASONS[reason_key][0]
    comment = d.get("comment", "")
    owner = callback.from_user.id

    msg = await callback.message.answer(
        f"⏳ Отправляю с {len(selected)} аккаунтов…\n"
        "Между аккаунтами пауза, чтобы не поймать лимит."
    )

    lines = []

    for phone in selected:
        session = await get_session(owner, phone)
        if not session:
            lines.append(f"{phone}: 🔴 сессия не найдена")
            continue

        client = Client(
            name=f"report_{owner}_{phone}",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )

        status, detail = "failed", ""

        try:
            await client.start()

            peer = (
                await client.resolve_peer(int(identifier))
                if identifier.startswith("-")
                else await client.resolve_peer(identifier)
            )

            try:
                result = await client.invoke(
                    functions.messages.Report(
                        peer=peer,
                        id=[msg_id],
                        reason=make_reason(reason_key, comment),
                        message=comment,
                    )
                )
                status = "success" if result else "failed"
            except FloodWait as e:
                if e.value <= 90:
                    await asyncio.sleep(e.value + 2)
                    result = await client.invoke(
                        functions.messages.Report(
                            peer=peer,
                            id=[msg_id],
                            reason=make_reason(reason_key, comment),
                            message=comment,
                        )
                    )
                    status = "success" if result else "failed"
                else:
                    status, detail = "flood", f"ждать {e.value}с"

        except (Unauthorized, TypeError, ValueError) as e:
            status = "invalid"
            detail = type(e).__name__
            await mark_invalid(owner, phone)
        except (PeerIdInvalid, UsernameNotOccupied):
            status, detail = "error", "нет доступа к чату/сообщению"
        except FloodWait as e:
            status, detail = "flood", f"ждать {e.value}с"
        except RPCError as e:
            status, detail = "error", f"{e.MESSAGE}"
        except Exception as e:
            log.exception("report")
            status, detail = "error", type(e).__name__
        finally:
            try:
                await client.stop()
            except Exception:
                pass

        await save_complaint(owner, phone, target, reason_label, comment, status)
        lines.append(f"{phone}: {STATUS_TEXT.get(status, status)}" + (f" ({detail})" if detail else ""))

        await msg.edit_text(f"📊 Прогресс:\n" + "\n".join(lines))

        if phone != selected[-1]:
            await asyncio.sleep(random.uniform(4.0, 9.0))

    await msg.edit_text("🏁 <b>Готово</b>\n\n" + "\n".join(lines))


# ------------------------------------------------------------------
# История
# ------------------------------------------------------------------
@dp.message(Command("history"))
async def cmd_history(message: Message):
    rows = await get_history(message.from_user.id)
    if not rows:
        await message.answer("📭 Историй пока нет.")
        return
    text = "📜 <b>Последние жалобы</b>\n\n"
    for phone, link, reason, status, created in rows:
        text += (
            f"{created} | {phone}\n"
            f"{esc(link)}\n"
            f"{esc(reason)} → {STATUS_TEXT.get(status, status)}\n\n"
        )
    await message.answer(text[:3900])


# ------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------
async def main():
    await init_db()
    log.info("БД готова. Запускаю polling и веб-сервер…")

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="add_account", description="Подключить аккаунт"),
        BotCommand(command="list_accounts", description="Мои аккаунты"),
        BotCommand(command="remove_account", description="Удалить аккаунт"),
        BotCommand(command="complain", description="Подать жалобу"),
        BotCommand(command="history", description="История жалоб"),
    ])

    # Запускаем веб-сервер
    web_runner = await start_web_server()
    
    # Запускаем очистку pending логинов
    task = asyncio.create_task(clean_pending_loop())
    
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        task.cancel()
        for key in list(pending):
            await drop_pending(*key)
        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
