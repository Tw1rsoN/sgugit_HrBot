import html
import uuid
import sqlite3
import logging
import threading
from threading import Thread
import requests
import math
import time
import json
import os
import re
import queue
import subprocess
import traceback
from datetime import datetime
from fake_useragent import UserAgent
from telegram import ForceReply
from flask import Flask, request, flash, redirect, url_for
from flask import make_response
from flask import render_template
from telegram import ParseMode
from urllib.parse import quote_plus
from telegram import User, Chat, Message, CallbackQuery
from telegram.error import BadRequest
from admin_ui import register_admin_ui
import asyncio
from playwright.async_api import async_playwright
from telegram import (
    Bot, Update,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
)
from telegram.ext import (
    Updater, CommandHandler,
    MessageHandler, Filters,
    CallbackContext, CallbackQueryHandler
)
from telegram.ext.dispatcher import DispatcherHandlerStop
from hh_playwright import (
    hh_login_and_get_storage_state,
    normalize_phone,
    is_email,
    run_hh_login_from_bot,
    run_hh_apply_from_bot,
    SessionExpiredError,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.DEBUG
)

# Configuration
TG_TOKEN = "8418652568:AAFjY9V6rHu3xWQBTmNQ9YX6xM0pmpzY1xA"
CLIENT_ID = "O6U5VGEIEHBRRT5M4FFU2B2SVBKO4ANSGB0S7IOSA4VEN2R4LQVTVOLPF8MONP56"
CLIENT_SECRET = "PE0MR3BJE9DUBDS94ACAEVCH5KJB2HOMS5TVV9HLOU32S1E4PQQABP68K0RN8IKL"
AUTH_BASE = "https://hh.ru"
API_BASE = "https://api.hh.ru"
BASE_URL = "https://containing-issue-excerpt-residence.trycloudflare.com"
REDIRECT_URI = f"{BASE_URL}/hh_callback"
VIDEO_DIR = "Video"
SRC_VIDEO = os.path.join(VIDEO_DIR, "instruction.mp4")
PROCESSED_VIDEO = os.path.join(VIDEO_DIR, "instruction_small.mp4")
_CLEAN_RE = re.compile(r'[^0-9A-Za-zА-Яа-яЁё.,!?:;«»"\'\-\(\)\[\]\s\n]')
ua = UserAgent()
_HH_INDUSTRY_MAP: dict[str, str] | None = None
_HH_SPEC_MAP: dict[str, str] | None = None

# Flask for OAuth Callback
app = Flask(__name__)
app.secret_key = '4f3d2e1b6a7c8d9e0f1234567890abcd'
state_map = {}
HH_BIND_LOCK = threading.Lock()
HH_BIND = {}
active_autoclicks = set()
active_autoclicks_lock = threading.Lock()

conn = sqlite3.connect("hh_bot.db", check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
cur = conn.cursor()

save_queue: "queue.Queue[tuple[int,str,Any]]" = queue.Queue()
HH_SMS_WAITERS = {}
HH_SMS_LOCK = threading.Lock()
register_admin_ui(app, db_path="hh_bot.db")

STOP_EVENTS = {}
STOP_EVENTS_LOCK = threading.Lock()

class StopWorker(Exception):
    pass

def _stop_event(tg_id: int) -> threading.Event:
    with STOP_EVENTS_LOCK:
        ev = STOP_EVENTS.get(tg_id)
        if ev is None:
            ev = threading.Event()
            STOP_EVENTS[tg_id] = ev
        return ev

def _get_sms_queue(tg_id: int) -> "queue.Queue[str]":
    with HH_SMS_LOCK:
        q = HH_SMS_WAITERS.get(tg_id)
        if q is None:
            q = queue.Queue(maxsize=1)
            HH_SMS_WAITERS[tg_id] = q
        return q


def _push_sms_code(tg_id: int, code: str) -> None:
    q = _get_sms_queue(tg_id)
    try:
        while True:
            q.get_nowait()
    except Exception:
        pass
    q.put_nowait(code)


def _save_hh_session_to_db(tg_id: int, account_key: str, storage_state: dict):
    user = get_user(tg_id) or {}
    try:
        sessions = json.loads(user.get("hh_sessions_json") or "{}")
        if not isinstance(sessions, dict):
            sessions = {}
    except Exception:
        sessions = {}

    sessions[account_key] = storage_state

    save_field(tg_id, "hh_sessions_json", json.dumps(sessions, ensure_ascii=False))
    save_field(tg_id, "hh_active_account", account_key)
    save_field(tg_id, "hh_token", "pw")

    save_queue.join()


def _hh_bind_worker(tg_id: int, bot_obj: Bot):
    def request_login() -> str:
        with HH_BIND_LOCK:
            st = HH_BIND.get(tg_id)
            if st:
                st["stage"] = "WAIT_LOGIN"
        return HH_BIND[tg_id]["q_login"].get()

    def request_sms() -> str:
        with HH_BIND_LOCK:
            st = HH_BIND.get(tg_id)
            if st:
                st["stage"] = "WAIT_SMS"
        bot_obj.send_message(chat_id=tg_id, text="Теперь пришли SMS-код (только цифры).")
        return HH_BIND[tg_id]["q_sms"].get()

    try:
        bot_obj.send_message(chat_id=tg_id, text="запускаю Playwright.")

        storage_state, mode, account_key = run_hh_login_from_bot(
            request_login=request_login,
            request_sms=request_sms,
            config_path="config.json",
            slow_mo=120,
        )

        _save_hh_session_to_db(tg_id, account_key, storage_state)

        bot_obj.send_message(chat_id=tg_id, text="Готово. Сессия сохранена в БД.", reply_markup=build_main_kb(tg_id))

    except Exception as e:
        bot_obj.send_message(
            chat_id=tg_id,
            text="Ошибка запуска Playwright/логина:\n" + str(e) + "\n\n" + traceback.format_exc()
        )
        bot_obj.send_message(chat_id=tg_id, text="Нажми «🔑 Привязать hh.ru аккаунт» и попробуй снова.",
                             reply_markup=build_main_kb(tg_id))

    finally:
        with HH_BIND_LOCK:
            HH_BIND.pop(tg_id, None)


def hh_bind_router(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    with HH_BIND_LOCK:
        st = HH_BIND.get(tg_id)

    if not st:
        return

    stage = st["stage"]

    if stage == "WAIT_LOGIN":
        try:
            st["q_login"].put_nowait(txt)
        except Exception:
            pass
        update.message.reply_text("Логин получил. Жду SMS...")
        raise DispatcherHandlerStop()

    if stage == "WAIT_SMS":
        code = re.sub(r"\D+", "", txt)
        if len(code) < 4:
            update.message.reply_text("Код слишком короткий. Пришли только цифры.")
            raise DispatcherHandlerStop()
        try:
            st["q_sms"].put_nowait(code)
        except Exception:
            pass
        update.message.reply_text("Код получил. Дологиниваюсь…")
        raise DispatcherHandlerStop()


def hh_bind_text_router(update: Update, ctx: CallbackContext):
    msg = update.message
    if not msg or not msg.text:
        return

    tg_id = update.effective_chat.id
    txt = msg.text.strip()

    user = get_user(tg_id)
    action = (user.get("pending_action") or "").strip()

    if action == "hh_bind_login":
        try:
            if is_email(txt):
                mode = "email"
                account_key = txt.lower()
            else:
                mode = "phone"
                account_key = normalize_phone(txt)
        except Exception as e:
            msg.reply_text(f"Некорректный ввод: {e}\nВведите телефон (+7...) или почту ещё раз.")
            raise DispatcherHandlerStop()

        save_field(tg_id, "pending_action", f"hh_bind_in_progress:{mode}:{account_key}")
        save_queue.join()

        msg.reply_text("Открываю окно входа hh.ru. Жди SMS и отправь код сюда цифрами.")

        Thread(
            target=hh_login_worker,
            args=(tg_id, mode, account_key, ctx.bot),
            daemon=True
        ).start()

        raise DispatcherHandlerStop()

    if action.startswith("hh_bind_sms:"):
        code = re.sub(r"\D+", "", txt)
        if len(code) < 4:
            msg.reply_text("Код слишком короткий. Отправь только цифры из SMS.")
            raise DispatcherHandlerStop()

        _push_sms_code(tg_id, code)
        msg.reply_text("Код получил. Продолжаю вход.")
        raise DispatcherHandlerStop()

    return


def save_worker():
    conn_w = sqlite3.connect("hh_bot.db", check_same_thread=False, timeout=30)
    conn_w.execute("PRAGMA journal_mode=WAL;")
    conn_w.execute("PRAGMA synchronous=NORMAL;")
    cur_w = conn_w.cursor()

    while True:
        tg_id, field, value = save_queue.get()
        cur_w.execute(
            f"UPDATE users SET {field} = ? WHERE telegram_id = ?",
            (value, tg_id)
        )
        if cur_w.rowcount == 0:
            cur_w.execute(
                f"INSERT INTO users (telegram_id, {field}) VALUES (?, ?)",
                (tg_id, value)
            )
        conn_w.commit()
        save_queue.task_done()


threading.Thread(target=save_worker, daemon=True).start()

desired_columns = {
    "telegram_id": "INTEGER PRIMARY KEY",
    "hh_token": "TEXT",
    "resume_id": "TEXT",
    "experience": "TEXT",
    "city": "TEXT",
    "area_id": "TEXT",
    "salary_from": "TEXT",
    "industry": "TEXT",
    "specialization": "TEXT",
    "cover_letter": "TEXT",
    "remaining_clicks": "INTEGER",
    "user_agreed": "INTEGER DEFAULT 0",
    "pending_action": "TEXT DEFAULT ''",
    "hh_sessions_json": "TEXT DEFAULT '{}'",
    "hh_active_account": "TEXT DEFAULT ''",

    "is_allowed": "INTEGER NOT NULL DEFAULT 0",

    "student_first_name": "TEXT",
    "student_last_name": "TEXT",
    "student_group": "TEXT",
    "study_specialization": "TEXT",
}

cols_defs = ",\n    ".join(f"{c} {t}" for c, t in desired_columns.items())
cur.execute(f"CREATE TABLE IF NOT EXISTS users (\n    {cols_defs}\n)")


cur.execute("PRAGMA table_info(users)")
existing = {row[1]: row for row in cur.fetchall()}

for col, col_type in desired_columns.items():
    if col not in existing:
        cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")

conn.commit()


def ensure_vuz_vacancies_schema():
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            experience TEXT NOT NULL DEFAULT '',
            specialization TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
        """
    )

    cur.execute("PRAGMA table_info(vacancies)")
    cols = {r[1] for r in cur.fetchall()}

    if "description" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "experience" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN experience TEXT NOT NULL DEFAULT ''")
    if "specialization" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN specialization TEXT NOT NULL DEFAULT ''")
    if "tags_json" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    if "is_active" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancy_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            applied_at INTEGER NOT NULL,
            UNIQUE(vacancy_id, telegram_id)
        )
        """
    )

    conn.commit()


def vuz_apply_first_from_bot(tg_id: int, limit: int, on_progress=None) -> int:
    ensure_vuz_vacancies_schema()

    cur = conn.cursor()
    cur.execute("SELECT study_specialization FROM users WHERE telegram_id = ?", (tg_id,))
    row = cur.fetchone()
    user_spec = (row[0] if row else "") or ""

    cur.execute(
        """
        SELECT id, title, description, experience, specialization, tags_json
        FROM vacancies
        WHERE is_active = 1
        ORDER BY created_at DESC
        """
    )
    rows = cur.fetchall()

    applied = 0
    for r in rows:
        if applied >= int(limit or 0):
            break

        vac_id, title, description, experience, vac_spec, tags_json = r
        try:
            tags = json.loads(tags_json or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []

        if not _vacancy_matches_user(user_spec, vac_spec, tags):
            continue

        cur.execute(
            """
            INSERT OR IGNORE INTO vacancy_responses (vacancy_id, telegram_id, applied_at)
            VALUES (?, ?, ?)
            """,
            (int(vac_id), int(tg_id), int(time.time()))
        )
        conn.commit()

        if cur.rowcount == 1:
            applied += 1
            if on_progress:
                on_progress(applied, int(limit or 0), f"✅ Отклик ВУЗа: {title}")

    return applied


def _norm_txt(s: str) -> str:
    import re
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _vacancy_matches_user(user_spec: str, vac_spec: str, tags: list) -> bool:
    u = _norm_txt(user_spec)
    if not u:
        return True

    v = _norm_txt(vac_spec)
    if v and (u == v or u in v or v in u):
        return True

    for t in (tags or []):
        tt = _norm_txt(str(t))
        if not tt:
            continue
        if u == tt or u in tt or tt in u:
            return True

    return False


def get_matching_vuz_vacancies_for_user(tg_id: int) -> list:
    """
    Возвращает список вакансий ВУЗа, подходящих пользователю по study_specialization.

    """
    ensure_vuz_vacancies_schema()

    cur = conn.cursor()
    cur.execute("SELECT study_specialization FROM users WHERE telegram_id = ?", (tg_id,))
    row = cur.fetchone()
    user_spec = (row[0] if row else "") or ""

    cur.execute("SELECT id, title, specialization, tags_json FROM vacancies ORDER BY created_at DESC")
    rows = cur.fetchall()

    out = []
    import json
    for r in rows:
        vac_id, title, vac_spec, tags_json = r[0], r[1], r[2], r[3], r[4]
        try:
            tags = json.loads(tags_json or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []

        if _vacancy_matches_user(user_spec, vac_spec, tags):
            out.append({"id": int(vac_id), "title": title or ""})

    return out


def _save_vuz_response(vacancy_id: int, tg_id: int):
    ensure_vuz_vacancies_schema()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO vacancy_responses (vacancy_id, telegram_id, applied_at)
        VALUES (?, ?, ?)
        """,
        (int(vacancy_id), int(tg_id), int(time.time()))
    )
    conn.commit()


def run_hh_apply_university_vacancies_from_bot(
        *,
        tg_id: int,
        storage_state: dict,
        vacancies: list,
        limit: int,
        cover_letter: str = None,
        headless: bool = False,
        slow_mo: int = 80,
        on_progress=None,
):



    async def _run():
        applied = 0
        skipped_cover_required = 0
        skipped_questions = 0
        errors = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo)
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()

            for idx, vac in enumerate(vacancies):
                if applied >= limit:
                    break

                vac_id = int(vac["id"])
                title = (vac.get("title") or "").strip()

                try:
                    if on_progress:
                        on_progress(applied, limit, f"ВУЗ-вакансия: открываю «{title or url}»")

                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(600)

                    cur_url = (page.url or "")
                    if "login" in cur_url or "account/login" in cur_url:
                        raise SessionExpiredError("hh session expired")

                    if "/applicant/vacancy_response" in cur_url:
                        skipped_questions += 1
                        if on_progress:
                            on_progress(applied, limit, f"Пропуск (вопросы работодателя): «{title or url}»")
                        continue

                    clicked = False
                    selectors = [
                        "button[data-qa='vacancy-response-button-top']",
                        "button[data-qa='vacancy-response-button']",
                        "button:has-text('Откликнуться')",
                        "a:has-text('Откликнуться')",
                    ]
                    for sel in selectors:
                        loc = page.locator(sel)
                        if await loc.count():
                            try:
                                await loc.first.click(timeout=8000)
                                clicked = True
                                break
                            except Exception:
                                pass

                    if not clicked:
                        if on_progress:
                            on_progress(applied, limit, f"Пропуск (нет кнопки отклика): «{title or url}»")
                        continue

                    await page.wait_for_timeout(800)
                    cur_url = (page.url or "")

                    if "/applicant/vacancy_response" in cur_url:
                        skipped_questions += 1
                        if on_progress:
                            on_progress(applied, limit, f"Пропуск (вопросы работодателя): «{title or url}»")
                        continue

                    textarea = page.locator("textarea")
                    if await textarea.count():
                        if not cover_letter:
                            skipped_cover_required += 1
                            if on_progress:
                                on_progress(applied, limit, f"Пропуск (нужно сопроводительное): «{title or url}»")
                            continue
                        try:
                            await textarea.first.fill(cover_letter[:4000])
                        except Exception:
                            pass

                    submitted = False
                    submit_selectors = [
                        "button:has-text('Отправить')",
                        "button:has-text('Откликнуться')",
                        "button:has-text('Продолжить')",
                    ]
                    for sel in submit_selectors:
                        b = page.locator(sel)
                        if await b.count():
                            try:
                                await b.first.click(timeout=8000)
                                submitted = True
                                break
                            except Exception:
                                pass

                    if not submitted:
                        errors += 1
                        if on_progress:
                            on_progress(applied, limit, f"Ошибка (не нашёл кнопку отправки): «{title or url}»")
                        continue

                    await page.wait_for_timeout(1200)

                    ok = False
                    for tsel in [
                        "text=Отклик отправлен",
                        "text=Вы откликнулись",
                        "text=Отклик успешно отправлен",
                    ]:
                        try:
                            if await page.locator(tsel).count():
                                ok = True
                                break
                        except Exception:
                            pass

                    if ok:
                        applied += 1
                        _save_vuz_response(vac_id, tg_id)
                        if on_progress:
                            on_progress(applied, limit, f"✅ ВУЗ-отклик отправлен: «{title or url}»")
                    else:
                        errors += 1
                        if on_progress:
                            on_progress(applied, limit, f"⚠️ Не уверен, что отклик ушёл: «{title or url}»")

                except SessionExpiredError:
                    raise
                except Exception:
                    errors += 1
                    if on_progress:
                        on_progress(applied, limit, f"Ошибка при отклике: «{title or url}»")

            new_state = await context.storage_state()
            await context.close()
            await browser.close()

        return {
            "applied": applied,
            "skipped_cover_required": skipped_cover_required,
            "skipped_questions": skipped_questions,
            "errors": errors,
            "new_storage_state": new_state,
        }

    return asyncio.run(_run())


def run_hh_apply_vuz_first_from_bot(
        *,
        tg_id: int,
        storage_state: dict,
        limit: int,
        cover_letter: str = None,
        config_path: str = "config.json",
        headless: bool = False,
        slow_mo: int = 80,
        on_progress=None,
):
    total_limit = int(limit or 0)
    total_applied = 0
    total_skipped_cover = 0
    total_skipped_questions = 0
    total_errors = 0

    def vuz_progress(done, lim, msg):
        if on_progress:
            on_progress(done, total_limit, msg)

    vuz_done = vuz_apply_first_from_bot(
        tg_id=tg_id,
        limit=total_limit,
        on_progress=vuz_progress
    )
    total_applied += int(vuz_done)

    remaining = max(0, total_limit - total_applied)

    def hh_progress(applied_local, _limit_local, msg):
        if on_progress:
            on_progress(total_applied + int(applied_local), total_limit, msg)

    state = storage_state
    if remaining > 0:
        res_hh = run_hh_apply_from_bot(
            storage_state=state,
            limit=remaining,
            cover_letter=cover_letter,
            config_path=config_path,
            headless=headless,
            slow_mo=slow_mo,
            on_progress=hh_progress,
        )

        total_applied += int(res_hh.get("applied") or 0)
        total_skipped_cover += int(res_hh.get("skipped_cover_required") or 0)
        total_skipped_questions += int(res_hh.get("skipped_questions") or 0)
        total_errors += int(res_hh.get("errors") or 0)
        new_state = res_hh.get("new_storage_state")
        if isinstance(new_state, dict):
            state = new_state

    return {
        "applied": total_applied,
        "skipped_cover_required": total_skipped_cover,
        "skipped_questions": total_skipped_questions,
        "errors": total_errors,
        "new_storage_state": state,
    }


bot = Bot(TG_TOKEN)
updater = Updater(bot=bot, use_context=True)

KNOWN_BUTTON_TO_KEY = {
    "▶️ Старт": "start_cmd",
    "▶️ Запустить поиск вакансий": "start_cmd",
    "⏹️ Стоп": "stop_cmd",
    "⚙️ Настройки": "settings_menu",
    "◀️ Назад в главное меню": "back_main",
    "◀️ Настройки": "settings_menu",

    # "🎯 Привязать резюме": "choose_resume",
    "🔑 Привязать hh.ru аккаунт": "auth_cmd",
    "❌ Отвязать hh.ru аккаунт": "unbind_hh",
    # "❌ Отвязать резюме": "unbind_resume",

    "⚙️ Фильтры вакансий": "filters_menu",
    # "◀️ Фильтры": "filters_menu",
    "Город": "filters_city",
    "Зарплата от": "filters_salary",
    "Отрасль": "filters_industry",
    "Специальность": "filters_spec",
    "Опыт": "filters_experience",
    "❌ Сбросить фильтры": "filters_reset",

    "✉️ Сопроводительное письмо": "cover_letter",
    "✏️ Редактировать": "cover_letter_edit",
    "🗑️ Удалить": "cover_letter_delete",

    "✅ Да": "confirm_yes",
    "❌ Нет": "confirm_no",

    "✅ Сбросить": "filters_reset_confirm",
    "❌ Отмена": "filters_reset_cancel",
    "Без опыта": "filters_experience_choice",
    "1–3 года": "filters_experience_choice",
    "3–5 лет": "filters_experience_choice",
    ">5 лет": "filters_experience_choice",
}


def _normalize_button_text(txt: str) -> str:
    if not txt:
        return ""
    t = txt.replace("\xa0", " ").strip()
    return t


def coalesce_text_middleware(update: Update, ctx: CallbackContext):
    msg = getattr(update, "message", None)
    if not msg or not msg.text:
        return

    btn_text = _normalize_button_text(msg.text)
    key = KNOWN_BUTTON_TO_KEY.get(btn_text)
    if not key:
        return
    remember_user_trigger(update, key=key, bot_obj=ctx.bot)


def init_coalescing(dispatcher):
    from telegram.ext import MessageHandler, Filters
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, coalesce_text_middleware), group=-100)


dp = updater.dispatcher
init_coalescing(dp)

coalesce_map = {}  # {(tg_id, key): message_id}
coalesce_lock = threading.Lock()

user_trigger_map = {}  # {(tg_id, key): message_id}
user_trigger_lock = threading.Lock()


def send_coalesced(
        tg_id: int,
        key: str,
        *,
        text: str,
        reply_markup=None,
        parse_mode=None,
        disable_web_page_preview: bool = True,
        bot_obj: Bot | None = None
):
    b = bot_obj or bot
    try:
        with coalesce_lock:
            prev_id = coalesce_map.get((tg_id, key))
        if prev_id:
            try:
                b.delete_message(chat_id=tg_id, message_id=prev_id)
            except Exception:
                pass

        m = b.send_message(
            chat_id=tg_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        with coalesce_lock:
            coalesce_map[(tg_id, key)] = m.message_id
        return m
    except Exception as e:
        logging.warning(f"send_coalesced failed for key={key}: {e}")
        return (bot_obj or bot).send_message(
            chat_id=tg_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )


def remember_user_trigger(update: Update, key: str, *, bot_obj: Bot | None = None) -> None:
    if not update or not update.message:
        return
    b = bot_obj or bot
    tg_id = update.effective_chat.id
    mid = update.message.message_id
    try:
        with user_trigger_lock:
            prev = user_trigger_map.get((tg_id, key))
            if prev and prev != mid:
                try:
                    b.delete_message(chat_id=tg_id, message_id=prev)
                except Exception:
                    pass
            user_trigger_map[(tg_id, key)] = mid
    except Exception:
        pass


to_drop = [col for col in existing if col not in desired_columns]
if to_drop:
    logging.info(f"В таблице users есть дополнительные колонки (оставляю как есть): {to_drop}")

desired_autoclick_columns = {
    "telegram_id": "INTEGER PRIMARY KEY",
    "next_run_ts": "REAL NOT NULL",
    "last_run_ts": "REAL",
    "is_enabled": "INTEGER NOT NULL DEFAULT 1",
    "interval_sec": "INTEGER NOT NULL DEFAULT 86400",
    "created_ts": "REAL NOT NULL",
}

# Создаём таблицу, если её ещё нет 
cols_defs_tasks = ",\n    ".join(f"{c} {t}" for c, t in desired_autoclick_columns.items())
cur.execute(f"CREATE TABLE IF NOT EXISTS autoclick_tasks (\n    {cols_defs_tasks}\n)")

# Текущее состояние таблицы
cur.execute("PRAGMA table_info(autoclick_tasks)")
existing_tasks = {row[1]: row for row in cur.fetchall()}

# Добавляем недостающие колонки
for col, col_type in desired_autoclick_columns.items():
    if col not in existing_tasks:
        cur.execute(f"ALTER TABLE autoclick_tasks ADD COLUMN {col} {col_type}")

to_drop_tasks = [col for col in existing_tasks if col not in desired_autoclick_columns]
if to_drop_tasks:
    logging.info(f"В таблице autoclick_tasks есть дополнительные колонки (оставляю как есть): {to_drop_tasks}")

cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_autoclick_due
    ON autoclick_tasks (is_enabled, next_run_ts)
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS running_workers (
        telegram_id INTEGER PRIMARY KEY,
        start_ts REAL NOT NULL
    )
""")

conn.commit()

_NO_CLEAN_FIELDS = {
    "hh_sessions_json",
    "hh_active_account",
    "hh_token",
}


def save_field(tg_id: int, field: str, value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)

    if isinstance(value, str) and field not in _NO_CLEAN_FIELDS and field != "experience":
        value = _CLEAN_RE.sub('', value).strip()

    save_queue.put((tg_id, field, value))


def get_user(tg_id: int) -> dict:
    cur_read = conn.cursor()
    cur_read.execute(
        """
        SELECT hh_token,
               resume_id,
               experience,
               city,
               area_id,
               salary_from,
               industry,
               specialization,
               cover_letter,
               is_allowed,
               pending_action,
               user_agreed,
               hh_sessions_json,
               hh_active_account
        FROM users
        WHERE telegram_id = ?
        """,
        (tg_id,),
    )
    row = cur_read.fetchone()
    cur_read.close()

    if not row:
        cur_ins = conn.cursor()
        cur_ins.execute(
            """
            INSERT OR IGNORE INTO users (
                telegram_id, hh_token, resume_id, experience, city, area_id,
                salary_from, industry, specialization, cover_letter,
                is_allowed, pending_action, user_agreed, hh_sessions_json, hh_active_account
            )
            VALUES (?, '', '', '', '', '', '', '', '', '', 0, '', 0, '{}', '')
            """,
            (tg_id,),
        )
        conn.commit()
        cur_ins.close()

        return {
            "telegram_id": tg_id,
            "hh_token": "",
            "resume_id": "",
            "experience": "",
            "city": "",
            "area_id": "",
            "salary_from": "",
            "industry": "",
            "specialization": "",
            "cover_letter": "",
            "is_allowed": 0,
            "pending_action": "",
            "user_agreed": 0,
            "hh_sessions_json": "{}",
            "hh_active_account": "",
        }

    return {
        "telegram_id": tg_id,
        "hh_token": row[0] or "",
        "resume_id": row[1] or "",
        "experience": row[2] or "",
        "city": row[3] or "",
        "area_id": row[4] or "",
        "salary_from": row[5] or "",
        "industry": row[6] or "",
        "specialization": row[7] or "",
        "cover_letter": row[8] or "",
        "is_allowed": int(row[9] or 0),
        "pending_action": row[10] or "",
        "user_agreed": int(row[11] or 0),
        "hh_sessions_json": row[12] or "{}",
        "hh_active_account": row[13] or "",
    }


def run_flask():
    app.run(host="0.0.0.0", port=5000)


class HHClient:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "HH-User-Agent": "HH_bot/1.0"}

    def list_resumes(self):
        return requests.get(f"{API_BASE}/resumes/mine", headers=self.headers)

    def recommendations(self, resume_id: str, area: str = None):
        params = {"area": area} if area else {}
        return requests.get(
            f"{API_BASE}/resumes/{resume_id}/recommendations",
            headers=self.headers,
            params=params
        )

    def search_vacancies(self, title: str, per_page: int):
        return requests.get(
            f"{API_BASE}/vacancies",
            headers=self.headers,
            params={"text": title, "per_page": per_page}
        )


def is_user_allowed(tg_id: int) -> bool:
    user = get_user(tg_id) or {}
    return int(user.get("is_allowed") or 0) == 1


def _parse_admin_ids() -> list:
    raw = (os.getenv("ADMIN_IDS") or "").strip()
    if not raw:
        return []
    ids = []
    for part in re.split(r"[ ,;]+", raw):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except Exception:
            pass
    return ids


def send_access_denied(update: Update, ctx: CallbackContext):
    update.message.reply_text(
        "⛔ Доступ к откликам пока не выдан.\n\n"
        "Нажмите «🔐 Запросить доступ» и попросите администратора вашего ВУЗа выдать разрешение.",
        reply_markup=build_main_kb(update.effective_chat.id),
    )


def request_access(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    update.message.reply_text(
        "✅ Запрос на доступ отправлен.\n"
        "Администратор вашего ВУЗа должен выдать разрешение на использование бота.",
        reply_markup=build_main_kb(tg_id),
    )

    admins = _parse_admin_ids()
    if not admins:
        return

    user = update.effective_user
    name = " ".join([p for p in [user.first_name, user.last_name] if p]) if user else str(tg_id)
    username = f"@{user.username}" if user and user.username else ""
    text = (
        "🔐 Запрос доступа к боту\n"
        f"• Пользователь: {name} {username}\n"
        f"• telegram_id: {tg_id}\n\n"
        "Чтобы выдать доступ, установите в БД users.is_allowed = 1 для этого telegram_id."
    )
    for admin_id in admins:
        try:
            ctx.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass


def request_access_start(update, context):
    tg_id = update.effective_user.id

    user = get_user(tg_id) or {}
    if int(user.get("is_allowed") or 0) == 1:
        update.message.reply_text(
            "✅ Доступ уже выдан. Нажмите «🔑 Привязать hh.ru аккаунт».",
            reply_markup=build_main_kb(tg_id),
        )
        raise DispatcherHandlerStop()

    if (user.get("pending_action") or "").strip() == "access_requested":
        update.message.reply_text(
            "✅ Заявка уже отправлена. Ожидайте подтверждения администратора.",
            reply_markup=build_main_kb(tg_id),
        )
        raise DispatcherHandlerStop()

    context.user_data.clear()
    context.user_data["request_state"] = "first_name"
    update.message.reply_text("Введите ваше имя:")
    raise DispatcherHandlerStop()


def request_access_process(update, context):
    msg = update.message
    if not msg or not msg.text:
        return

    tg_id = update.effective_user.id
    text = msg.text.strip()
    state = context.user_data.get("request_state")

    if not state and text != "📨 Отправить заявку":
        return

    if state == "first_name":
        context.user_data["student_first_name"] = text
        context.user_data["request_state"] = "last_name"
        msg.reply_text("Введите вашу фамилию:")
        raise DispatcherHandlerStop()

    if state == "last_name":
        context.user_data["student_last_name"] = text
        context.user_data["request_state"] = "group"
        msg.reply_text("Введите вашу группу:")
        raise DispatcherHandlerStop()

    if state == "group":
        context.user_data["student_group"] = text
        context.user_data["request_state"] = "specialization"
        msg.reply_text("Введите вашу специализацию обучения:")
        raise DispatcherHandlerStop()

    if state == "specialization":
        context.user_data["study_specialization"] = text
        context.user_data["request_state"] = "confirm"
        msg.reply_text(
            "Проверьте введённые данные:\n\n"
            f"Имя: {context.user_data.get('student_first_name', '')}\n"
            f"Фамилия: {context.user_data.get('student_last_name', '')}\n"
            f"Группа: {context.user_data.get('student_group', '')}\n"
            f"Специализация: {context.user_data.get('study_specialization', '')}\n\n"
            "Если всё верно — нажмите «📨 Отправить заявку».",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📨 Отправить заявку")]], resize_keyboard=True),
        )
        raise DispatcherHandlerStop()

    if text == "📨 Отправить заявку":
        first_name = context.user_data.get("student_first_name") or ""
        last_name = context.user_data.get("student_last_name") or ""
        group_name = context.user_data.get("student_group") or ""
        spec = context.user_data.get("study_specialization") or ""

        db = sqlite3.connect("hh_bot.db", timeout=30)
        db.execute("PRAGMA journal_mode=WAL;")
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO users (telegram_id, student_first_name, student_last_name, student_group, study_specialization, pending_action)
            VALUES (?, ?, ?, ?, ?, 'access_requested')
            ON CONFLICT(telegram_id) DO UPDATE SET
                student_first_name=excluded.student_first_name,
                student_last_name=excluded.student_last_name,
                student_group=excluded.student_group,
                study_specialization=excluded.study_specialization,
                pending_action='access_requested'
            """,
            (tg_id, first_name, last_name, group_name, spec),
        )
        db.commit()
        db.close()

        msg.reply_text(
            "✅ Ваша заявка отправлена. Ожидайте подтверждения администратора.",
            reply_markup=build_main_kb(tg_id),
        )
        context.user_data.clear()
        raise DispatcherHandlerStop()

    # если попали сюда в состоянии confirm, но нажали/написали что-то другое
    if state == "confirm":
        msg.reply_text(
            "Нажмите «📨 Отправить заявку», чтобы отправить заявку.",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📨 Отправить заявку")]], resize_keyboard=True),
        )
        raise DispatcherHandlerStop()

    return


def access_status_watcher_daemon(poll_sec: float = 1.0):
    last_state: dict[int, int] = {}

    conn_w = sqlite3.connect("hh_bot.db", check_same_thread=False, timeout=30)
    conn_w.execute("PRAGMA journal_mode=WAL;")
    conn_w.execute("PRAGMA synchronous=NORMAL;")
    cur = conn_w.cursor()

    while True:
        try:
            cur.execute(
                "SELECT telegram_id, COALESCE(is_allowed,0), COALESCE(pending_action,'') FROM users"
            )
            rows = cur.fetchall()

            alive_ids = set()

            for tg_id, is_allowed, pending_action in rows:
                if tg_id is None:
                    continue
                tg_id = int(tg_id)
                is_allowed = int(is_allowed or 0)
                pending_action = (pending_action or "").strip()

                alive_ids.add(tg_id)

                prev = last_state.get(tg_id)

                if is_allowed == 1 and pending_action == "access_requested":
                    send_coalesced(
                        tg_id,
                        key="access_state",
                        text="✅ Доступ выдан администратором.\nНажмите «🔑 Привязать hh.ru аккаунт».",
                        reply_markup=build_main_kb(tg_id),
                    )
                    try:
                        cur.execute("UPDATE users SET pending_action='' WHERE telegram_id=?", (tg_id,))
                        conn_w.commit()
                    except Exception:
                        pass
                    last_state[tg_id] = 1
                    continue


                if prev is None:
                    last_state[tg_id] = is_allowed
                    continue

                if prev != is_allowed:
                    last_state[tg_id] = is_allowed

                    if is_allowed == 1:
                        send_coalesced(
                            tg_id,
                            key="access_state",
                            text="✅ Доступ выдан администратором.\nНажмите «🔑 Привязать hh.ru аккаунт».",
                            reply_markup=build_main_kb(tg_id),
                        )
                        try:
                            cur.execute("UPDATE users SET pending_action='' WHERE telegram_id=?", (tg_id,))
                            conn_w.commit()
                        except Exception:
                            pass
                    else:
                        try:
                            disable_autoclick_task(tg_id)
                        except Exception:
                            pass

                        send_coalesced(
                            tg_id,
                            key="access_state",
                            text="⛔ Доступ отозван администратором.\nДоступна кнопка «🔐 Запросить доступ».",
                            reply_markup=build_main_kb(tg_id),
                        )

            for known_id in list(last_state.keys()):
                if known_id not in alive_ids:
                    last_state.pop(known_id, None)

        except Exception as e:
            logging.warning(f"access_status_watcher_daemon error: {e}")

        time.sleep(poll_sec)


def build_main_kb(tg_id: int):
    user = get_user(tg_id) or {}

    allowed = int(user.get("is_allowed") or 0) == 1
    hh_bound = bool(user.get("hh_token"))
    resume_bound = bool(user.get("resume_id"))

    if not allowed:
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🔐 Запросить доступ")]],
            resize_keyboard=True
        )

    if not hh_bound:
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🔑 Привязать hh.ru аккаунт")]],
            resize_keyboard=True
        )

    # if not resume_bound:
    #     return ReplyKeyboardMarkup(
    #         [[KeyboardButton("🎯 Привязать резюме")]],
    #         resize_keyboard=True
    #     )

    try:
        running = is_autoclick_enabled(tg_id)
    except Exception:
        running = False

    start_stop = "⏹️ Стоп" if running else "▶️ Старт"

    return ReplyKeyboardMarkup(
        [[KeyboardButton(start_stop), KeyboardButton("⚙️ Настройки")]],
        resize_keyboard=True
    )


@app.errorhandler(400)
def bad_request(e):
    return render_template("error_400.html"), 400


@app.errorhandler(500)
def server_error(e):
    return render_template("error_500.html"), 500



@app.route("/hh_callback")
def hh_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    tg_id = state_map.get(state)

    if not code or not state or tg_id is None:
        return "", 400

    resp = requests.post(
        f"{API_BASE}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = (resp.json() or {}).get("access_token")
    if not token:
        bot.send_message(
            chat_id=tg_id,
            text=(
                "❌ Не удалось получить токен hh.ru.\n"
                "Пожалуйста, нажмите кнопку «🔑 Войти на hh.ru», чтобы повторить авторизацию."
            ),
        )
        return render_template("error_400.html"), 400

    save_field(tg_id, "hh_token", token)
    save_queue.join()

    bot.send_message(
        chat_id=tg_id,
        text="✅ hh.ru - аккаунт успешно привязан!\n\nА теперь: 🎯 Привяжите резюме.",
        reply_markup=build_main_kb(tg_id),
    )

    return render_template("success.html")


def get_hh_headers(user_id: int) -> dict:
    token = get_user(user_id)["hh_token"]
    return {
        "Authorization": f"Bearer {token}",
        "HH-User-Agent": ua.random
    }


def _ensure_spec_map() -> dict[str, str]:
    global _HH_SPEC_MAP
    if _HH_SPEC_MAP is not None:
        return _HH_SPEC_MAP

    data = None
    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/professional_roles", timeout=10)
            resp.raise_for_status()
            data = resp.json().get("categories", [])
            break
        except Exception as e:
            logging.warning(f"HH /professional_roles attempt {attempt + 1}/3 failed: {e}")
            time.sleep(1)

    if data is None:
        logging.error("HH /professional_roles failed after 3 attempts")
        _HH_SPEC_MAP = {}
        return _HH_SPEC_MAP

    mp: dict[str, str] = {}
    for cat in data:
        mp[str(cat["id"])] = cat["name"]
        for role in cat.get("roles", []):
            mp[str(role["id"])] = role["name"]

    _HH_SPEC_MAP = mp
    return mp


def _ensure_industry_map() -> dict[str, str]:
    global _HH_INDUSTRY_MAP
    if _HH_INDUSTRY_MAP is not None:
        return _HH_INDUSTRY_MAP

    data = None
    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/industries", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            logging.warning(f"HH /industries attempt {attempt + 1}/3 failed: {e}")
            time.sleep(1)

    if data is None:
        logging.error("HH /industries failed after 3 attempts")
        _HH_INDUSTRY_MAP = {}
        return _HH_INDUSTRY_MAP

    mp: dict[str, str] = {}
    for cat in data:
        mp[str(cat["id"])] = cat["name"]
        for sub in cat.get("industries", []):
            mp[str(sub["id"])] = sub["name"]

    _HH_INDUSTRY_MAP = mp
    return mp


def _csv_ids_to_names(csv_ids: str | None, mp: dict[str, str]) -> list[str]:
    if not csv_ids:
        return []
    parts = [p.strip() for p in str(csv_ids).split(",") if p.strip()]
    return [mp.get(p, p) for p in parts]


def _humanize_names(names: list[str], limit: int = 6) -> str:
    if not names:
        return "—"
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return f"{shown} …(+{len(names) - limit})"


@app.route("/industries", methods=["GET", "POST"])
def industries():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return "Missing user_id", 400

    if request.method == "GET":
        headers = get_hh_headers(user_id)
        data = None
        for attempt in range(3):
            try:
                resp = requests.get(f"{API_BASE}/industries", headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                logging.warning(f"GET /industries попытка {attempt + 1}/3 не удалась: {e}")
                time.sleep(1)
        if data is None:
            try:
                send_coalesced(
                    user_id, key="industries_unavailable",
                    text="⚠️ Справочник hh.ru недоступен в данное время. Пожалуйста, попробуйте позже.",
                    bot_obj=bot
                )
            except Exception:
                pass
            return "", 503

        categories = [
            (cat["name"], [(sub["id"], sub["name"]) for sub in cat.get("industries", [])])
            for cat in data
        ]
        return render_template("industries.html", categories=categories, user_id=user_id)

    selected = request.form.getlist("industry_ids")
    save_field(user_id, "industry", ",".join(selected))

    mp = _ensure_industry_map()
    saved_names = [mp.get(str(i), str(i)) for i in selected]
    names_str = ", ".join(saved_names) if saved_names else "—"

    start_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Запустить поиск вакансий", callback_data="start_autoclick")]])
    send_coalesced(
        user_id, key="industries_saved",
        text=f"✅ {'Отрасль' if len(saved_names) == 1 else 'Отрасли'} сохранены: {names_str}\n\n"
             "<b>Для запуска поиска вакансий нажмите на кнопку ниже:</b>",
        reply_markup=start_btn, parse_mode=ParseMode.HTML, bot_obj=bot
    )
    return '', 204


@app.route("/specializations", methods=["GET", "POST"])
def specializations():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return "Missing user_id", 400

    if request.method == "GET":
        headers = get_hh_headers(user_id)
        resp = requests.get(f"{API_BASE}/professional_roles", headers=headers, timeout=10)
        resp.raise_for_status()
        categories = [
            (cat["name"], [(role["id"], role["name"]) for role in cat.get("roles", [])])
            for cat in resp.json().get("categories", [])
        ]
        return render_template("specializations.html", categories=categories, user_id=user_id)

    selected = request.form.getlist("specialization_ids")
    save_field(user_id, "specialization", ",".join(selected))

    mp = _ensure_spec_map()
    saved_names = [mp.get(str(i), str(i)) for i in selected]
    names_str = ", ".join(saved_names) if saved_names else "—"

    start_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Запустить поиск вакансий", callback_data="start_autoclick")]])
    send_coalesced(
        user_id, key="spec_saved",
        text=f"✅ Специальности сохранены: {names_str}\n\n"
             "<b>Для запуска поиска вакансий нажмите на кнопку ниже:</b>",
        reply_markup=start_btn, parse_mode=ParseMode.HTML, bot_obj=bot
    )
    return '', 204


def ensure_user_agreed(update, ctx):
    # tg_id = update.effective_chat.id
    # user = get_user(tg_id)
    # if not user.get("user_agreed"):
    #     # определяем внутреннее имя действия
    #     if update.callback_query:
    #         action = update.callback_query.data
    #     else:
    #         text = update.message.text or ""
    #         mapping = {
    #             "▶️ Старт": "autoclick",
    #             "🔑 Привязать HeadHunter-аккаунт": "auth_cmd",
    #             "🎯 Привязать резюме": "choose_resume",
    #             "⚙️ Настройки": "settings_menu",
    #         }
    #         action = mapping.get(text, "")
    #
    #     # сохраняем только внутреннее имя
    #     save_field(tg_id, "pending_action", action)
    #
    #     # генерируем корректную ссылку (BASE_URL уже содержит https://)
    #     url = f"{BASE_URL}/agreement?chat_id={tg_id}"
    #
    #     kb = InlineKeyboardMarkup([[
    #         InlineKeyboardButton("📄 Принять соглашение", url=url)
    #     ]])
    #     msg = "🔔 Для продолжения работы сначала примите пользовательское соглашение."
    #
    #     if update.callback_query:
    #         update.callback_query.answer()
    #         update.callback_query.message.reply_text(msg, reply_markup=kb)
    #     else:
    #         update.message.reply_text(msg, reply_markup=kb)
    #     return False

    return True


def settings_menu(update: Update, ctx: CallbackContext):
    remember_user_trigger(update, key="settings_menu", bot_obj=ctx.bot)

    if not ensure_user_agreed(update, ctx):
        return
    tg_id = update.effective_chat.id
    send_coalesced(
        tg_id,
        key="settings_menu",
        text=(
            "🔧 Меню настроек:\n\n"
            "• ❌ Отвязать hh.ru аккаунт\n"
            # "• ❌ Отвязать резюме\n"
            # "• ⚙️ Фильтры вакансий\n"
            "• ✉️ Сопроводительное письмо"
        ),
        reply_markup=ReplyKeyboardMarkup(
            [
                ["❌ Отвязать hh.ru аккаунт",
                 # "❌ Отвязать резюме"
                 ],
                [
                    # "⚙️ Фильтры вакансий",
                    "✉️ Сопроводительное письмо"],
                ["◀️ Назад в главное меню"]
            ],
            resize_keyboard=True
        ),
        bot_obj=ctx.bot,
    )


def ask_cover_letter(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    user = get_user(tg_id)
    cover = (user.get("cover_letter") or "").strip()

    kb_back = ReplyKeyboardMarkup([["◀️ Настройки"]], resize_keyboard=True)

    if cover:
        menu_text = (
            "✉️ Сопроводительное письмо сохранено и будет добавляться к каждому поиску вакансий.\n\n"
            f"<b>Ваше текущее письмо:</b>\n{cover}\n\n"
            "✏️ Нажмите «Редактировать», чтобы заменить текст письма.\n"
            "🗑️ Нажмите «Удалить», чтобы полностью убрать письмо."
        )
        menu_kb = ReplyKeyboardMarkup(
            [["✏️ Редактировать", "🗑️ Удалить"], ["◀️ Настройки"]],
            resize_keyboard=True
        )
        send_coalesced(
            tg_id, key="cover_menu",
            text=menu_text, reply_markup=menu_kb,
            parse_mode=ParseMode.HTML, bot_obj=ctx.bot
        )
    else:
        ctx.user_data["awaiting_cover"] = True
        prompt = (
            "✉️ У вас пока нет сопроводительного письма.\n"
            "Вводите текст — он будет автоматически прикрепляться к вашим поискам вакансий."
        )
        send_coalesced(
            tg_id, key="cover_prompt",
            text=prompt, reply_markup=kb_back,
            bot_obj=ctx.bot
        )

    start_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Запустить поиск вакансий", callback_data="start_autoclick")]])
    send_coalesced(
        tg_id, key="cover_cta",
        text="<b>Для запуска поиска вакансий нажмите на кнопку ниже:</b>",
        reply_markup=start_btn, parse_mode=ParseMode.HTML, bot_obj=ctx.bot
    )


def save_cover_letter(update: Update, ctx: CallbackContext):
    if not ctx.user_data.pop("awaiting_cover", False):
        return
    tg_id = update.effective_chat.id
    cover = (update.message.text or "").strip()
    save_field(tg_id, "cover_letter", cover)

    send_coalesced(
        tg_id, key="cover_saved",
        text="✅ Сопроводительное письмо сохранено!",
        bot_obj=ctx.bot
    )
    ask_cover_letter(update, ctx)


def cover_menu_handler(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    text = _normalize_button_text(update.message.text or "")

    if text == "✏️ Редактировать":
        ctx.user_data["awaiting_cover"] = True
        send_coalesced(
            tg_id, key="cover_edit_prompt",
            text="✏️ Введите новый текст сопроводительного письма:",
            reply_markup=ReplyKeyboardMarkup([["◀️ Настройки"]], resize_keyboard=True),
            bot_obj=ctx.bot
        )
        return

    if text == "🗑️ Удалить":
        ctx.user_data["await_delete_cover"] = True
        send_coalesced(
            tg_id, key="cover_delete_confirm",
            text="❓ Вы действительно хотите удалить письмо?",
            reply_markup=ReplyKeyboardMarkup([["✅ Да", "❌ Нет"]], resize_keyboard=True),
            bot_obj=ctx.bot
        )
        return

    if text in ("✅ Да", "❌ Нет") and ctx.user_data.pop("await_delete_cover", False):
        if text == "✅ Да":
            save_field(tg_id, "cover_letter", "")
            send_coalesced(tg_id, key="cover_delete_result", text="✅ Сопроводительное письмо удалено!", bot_obj=ctx.bot)
        else:
            send_coalesced(tg_id, key="cover_delete_result", text="❌ Удаление отменено.", bot_obj=ctx.bot)
        ask_cover_letter(update, ctx)
        return

    if text == "◀️ Настройки":
        settings_menu(update, ctx)


def start(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    get_user(tg_id)

    user = get_user(tg_id) or {}
    allowed = int(user.get("is_allowed") or 0) == 1

    msg = (
        "Привет! Я помогу автоматически откликаться на вакансии на hh.ru.\n\n"
        "1) Нажмите «🔑 Привязать hh.ru аккаунт» и пройдите авторизацию.\n"
        "2) Привяжите резюме.\n"
        "3) Настройте фильтры и сопроводительное письмо.\n"
        "4) Нажмите «▶️ Старт».\n"
    )

    if not allowed:
        msg += (
            "\n⛔ Сейчас у вас нет доступа к откликам.\n"
            "Нажмите «🔐 Запросить доступ» и попросите администратора вашего ВУЗа выдать разрешение."
        )

    update.message.reply_text(msg, reply_markup=build_main_kb(tg_id))


def ensure_processed_video():
    if not os.path.exists(PROCESSED_VIDEO):
        subprocess.run([
            "ffmpeg", "-y",
            "-i", SRC_VIDEO,

            "-filter:v", "scale=min(720\\,iw):-2:flags=lanczos,setsar=1,setpts=PTS/1.5",

            "-filter:a", "atempo=1.5",

            "-c:v", "libx264", "-crf", "23", "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",

            "-movflags", "+faststart",

            PROCESSED_VIDEO
        ], check=True)


def show_video_callback(update: Update, ctx: CallbackContext):
    query = update.callback_query
    query.answer()

    wait_msg = query.message.reply_text(
        "🔄 Пожалуйста, подождите, видео-инструкция загружается…"
    )

    ensure_processed_video()

    with open(PROCESSED_VIDEO, "rb") as video_file:
        query.message.reply_video(
            video=video_file,
            caption="▶️ Видео-инструкция по работе с ботом",
            supports_streaming=True
        )

    query.bot.delete_message(
        chat_id=wait_msg.chat.id,
        message_id=wait_msg.message_id
    )


def auth_cmd(update: Update, ctx: CallbackContext):
    if not ensure_user_agreed(update, ctx):
        return

    tg_id = update.effective_chat.id

    with HH_BIND_LOCK:
        if tg_id in HH_BIND:
            ctx.bot.send_message(chat_id=tg_id, text="Процесс входа уже запущен. Пришли логин/смс.")
            return

        HH_BIND[tg_id] = {
            "stage": "WAIT_LOGIN",
            "q_login": queue.Queue(maxsize=1),
            "q_sms": queue.Queue(maxsize=1),
        }

    ctx.bot.send_message(
        chat_id=tg_id,
        text="Ок, запускаю браузер hh.ru. Пришли телефон (+7...) или почту."
    )

    t = threading.Thread(target=_hh_bind_worker, args=(tg_id, ctx.bot), daemon=True)
    t.start()


def choose_resume(update: Update, ctx: CallbackContext):
    if not ensure_user_agreed(update, ctx):
        return

    tg_id = update.effective_chat.id
    bot = ctx.bot

    user = get_user(tg_id)
    if not user["hh_token"]:
        return send_coalesced(
            tg_id, key="choose_resume_result",
            text="❗ Сначала привяжите hh.ru-аккаунт",
            reply_markup=build_main_kb(tg_id),
            bot_obj=bot
        )

    hh = HHClient(user["hh_token"])
    resp = hh.list_resumes()
    if resp.status_code != 200:
        return send_coalesced(
            tg_id, key="choose_resume_result",
            text="❌ Ошибка получения списка резюме",
            reply_markup=build_main_kb(tg_id),
            bot_obj=bot
        )

    items = resp.json().get("items", [])
    if not items:
        return send_coalesced(
            tg_id, key="choose_resume_result",
            text="❗ У вас нет резюме на hh.ru",
            reply_markup=build_main_kb(tg_id),
            bot_obj=bot
        )

    intro = (
        "📄 Ниже — список ваших резюме на hh.ru:\n"
        "Нажмите на нужное резюме, чтобы привязать его к боту."
    )
    buttons = [
        [InlineKeyboardButton(
            r.get("title") or r.get("name") or r["id"],
            callback_data=f"selres_{r['id']}"
        )]
        for r in items
    ]
    kb = InlineKeyboardMarkup(buttons)

    send_coalesced(
        tg_id, key="choose_resume_list",
        text=intro,
        reply_markup=kb,
        bot_obj=bot
    )


def resume_selected(update: Update, ctx: CallbackContext):
    q = update.callback_query
    q.answer()
    rid = q.data.split("|", 1)[1]
    save_field(q.from_user.id, "resume_id", rid)

    q.message.reply_text(
        "✅ Резюме привязано.\n\n"
        "Теперь вы можете настроить сопроводительное письмо и нажать «▶️ Старт».",
        reply_markup=build_main_kb(q.from_user.id),
    )


def unbind_hh(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id

    save_field(tg_id, "hh_token", None)
    save_field(tg_id, "hh_sessions_json", "{}")
    save_field(tg_id, "hh_active_account", "")
    save_field(tg_id, "pending_action", "")
    save_queue.join()

    send_coalesced(
        tg_id, key="unbind_hh_result",
        text="hh.ru аккаунт отвязан. Сессии удалены из базы данных.",
        reply_markup=build_main_kb(tg_id),
        bot_obj=ctx.bot
    )


def unbind_resume(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    save_field(tg_id, "resume_id", None)
    save_queue.join()
    send_coalesced(
        tg_id, key="unbind_resume_result",
        text="✅ Резюме отвязано.",
        reply_markup=build_main_kb(tg_id),
        bot_obj=ctx.bot
    )


# def set_filters(update: Update, ctx: CallbackContext):
#     remember_user_trigger(update, key="filters_cmd", bot_obj=ctx.bot)
#
#     chat = update.effective_chat
#     tg_id = chat.id
#     user = get_user(tg_id) or {}
#
#     raw_city = user.get("city")
#     raw_salary = user.get("salary_from")
#     raw_exp = user.get("experience")
#     raw_ind_csv = user.get("industry")
#     raw_spec_csv = user.get("specialization")
#
#     exp_map = {"0": "Без опыта", "1-3": "1–3 года", "3-5": "3–5 лет", "5+": ">5 лет"}
#     exp_txt = exp_map.get(str(raw_exp)) if raw_exp not in (None, "", "None") else None
#
#     ind_names = _csv_ids_to_names(raw_ind_csv, _ensure_industry_map())
#     spec_names = _csv_ids_to_names(raw_spec_csv, _ensure_spec_map())
#
#     has_city = bool(raw_city)
#     has_salary = raw_salary not in (None, "", "0", 0, 0.0)
#     has_exp = bool(exp_txt)
#     has_ind = bool(ind_names)
#     has_spec = bool(spec_names)
#     any_filters = has_city or has_salary or has_exp or has_ind or has_spec
#
#     if any_filters:
#         lines = [
#             "⚙️ Фильтры включены.",
#             "Чтобы изменить — выберите ниже.",
#             "Чтобы убрать все разом — «❌ Сбросить фильтры».",
#             ""
#         ]
#         if has_city:
#             lines.append(f"🏙️ Город: {raw_city}")
#         if has_salary:
#             lines.append(f"💰 Зарплата от: {raw_salary}")
#         if has_ind:
#             lines.append(f"🏭 Отрасли: {_humanize_names(ind_names)}")
#         if has_spec:
#             lines.append(f"🎯 Специальности: {_humanize_names(spec_names)}")
#         if has_exp:
#             lines.append(f"⏳ Опыт: {exp_txt}")
#         text = "\n".join(lines)
#     else:
#         text = (
#             "⚙️ Фильтры пока не настроены.\n\n"
#             "Можно фильтровать по:\n"
#             "— городу\n"
#             "— зарплате\n"
#             "— отраслям\n"
#             "— специальностям\n"
#             "— опыту\n\n"
#             "Выберите, что хотите настроить:"
#         )
#
#     kb = ReplyKeyboardMarkup([
#         ["Город", "Зарплата от"],
#         ["Отрасль", "Специальность"],
#         ["Опыт", "❌ Сбросить фильтры"],
#         ["◀️ Настройки"]
#     ], resize_keyboard=True)
#
#     send_coalesced(
#         tg_id, key="filters_info", text=text, reply_markup=kb, bot_obj=ctx.bot,
#     )
#
#     start_btn = InlineKeyboardMarkup([
#         [InlineKeyboardButton("▶️ Запустить поиск вакансий", callback_data="start_autoclick")]
#     ])
#     send_coalesced(
#         tg_id, key="filters_cta",
#         text="<b>Для запуска поиска вакансий нажмите на кнопку ниже:</b>",
#         reply_markup=start_btn, parse_mode=ParseMode.HTML, bot_obj=ctx.bot,
#     )


def back_to_main(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    send_coalesced(
        tg_id, key="back_main",
        text="Вы вернулись в главное меню.",
        reply_markup=build_main_kb(tg_id),
        bot_obj=ctx.bot
    )


def reset_filters(update: Update, ctx: CallbackContext):
    ctx.user_data["awaiting_reset"] = True
    tg_id = update.effective_chat.id
    kb = ReplyKeyboardMarkup([["✅ Сбросить", "❌ Отмена"]], resize_keyboard=True)
    send_coalesced(
        tg_id, key="filters_reset_confirm",
        text="⚠️ Вы действительно хотите сбросить все фильтры?",
        reply_markup=kb, bot_obj=ctx.bot
    )


def reset_filters_confirmation(update: Update, ctx: CallbackContext):
    if not ctx.user_data.pop("awaiting_reset", False):
        return
    tg_id = update.effective_chat.id
    text = _normalize_button_text(update.message.text or "")

    if text == "✅ Сбросить":
        for f in ("city", "salary_from", "industry", "specialization", "experience", "area_id"):
            save_field(tg_id, f, None)
        send_coalesced(
            tg_id, key="filters_reset_result",
            text="✅ Все фильтры сброшены.",
            reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True),
            bot_obj=ctx.bot
        )
        set_filters(update, ctx)
    else:
        # ❌ Отмена
        send_coalesced(
            tg_id, key="filters_reset_result",
            text="❌ Сброс отменён.",
            reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True),
            bot_obj=ctx.bot
        )
        set_filters(update, ctx)


def ask_filter_value(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    text = _normalize_button_text(update.message.text or "")

    mapping = {
        "Город": "city",
        "Зарплата от": "salary_from",
        "Отрасль": "industry",
        "Специальность": "specialization",
        "Опыт": "experience",
    }
    field = mapping.get(text)
    if not field:
        return

    ctx.user_data["awaiting"] = field

    if field == "salary_from":
        prompt = "💰 Введите минимальную зарплату (только цифры, без пробелов), например: 50000"
    elif field == "city":
        prompt = "🌆 Введите город (например: Москва):"
    elif field == "industry":
        prompt = (
            "🏭 Введите отрасль (точно, как в списке),\n"
            f'или откройте и сохраните из справочника: {BASE_URL}/industries?user_id={tg_id}'
        )
    elif field == "specialization":
        prompt = (
            "🔧 Введите специальность (точно, как в списке),\n"
            f'или откройте и сохраните из справочника: {BASE_URL}/specializations?user_id={tg_id}'
        )
    else:
        return set_filters_experience(update, ctx)

    kb = ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True)
    send_coalesced(
        tg_id, key="filters_prompt",
        text=prompt, reply_markup=kb,
        bot_obj=ctx.bot
    )


# def set_filters_experience(update: Update, ctx: CallbackContext):
#     tg_id = update.effective_chat.id
#     kb = ReplyKeyboardMarkup(
#         [["Без опыта", "1–3 года"], ["3–5 лет", ">5 лет"], ["◀️ Фильтры"]],
#         resize_keyboard=True
#     )
#     send_coalesced(
#         tg_id, key="filters_experience_menu",
#         text="Выберите опыт:",
#         reply_markup=kb, bot_obj=ctx.bot
#     )
#


def save_experience(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    mp = {"Без опыта": "0", "1–3 года": "1-3", "3–5 лет": "3-5", ">5 лет": "5+"}
    val = mp.get(_normalize_button_text(update.message.text or ""))
    if not val:
        return
    save_field(tg_id, "experience", val)
    send_coalesced(tg_id, key="filters_saved_notice", text="✅ Опыт сохранён!", bot_obj=ctx.bot)
    set_filters(update, ctx)


def autoclick(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id

    if not is_user_allowed(tg_id):
        return send_access_denied(update, ctx)

    user = get_user(tg_id) or {}
    if not (user.get("hh_token") or "").strip():
        return update.message.reply_text(
            "⚠️ Сначала привяжите hh.ru аккаунт.",
            reply_markup=build_main_kb(tg_id),
        )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Начать отклики", callback_data="start_autoclick")]])
    update.message.reply_text(
        "Готово. Нажмите кнопку ниже, чтобы начать отклики.",
        reply_markup=kb,
    )


def is_autoclick_enabled(user_id: int) -> bool:
    try:
        cur2 = conn.cursor()
        cur2.execute("SELECT is_enabled FROM autoclick_tasks WHERE telegram_id = ?", (user_id,))
        row = cur2.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def enable_autoclick_task(user_id: int, *, first_run_now: bool = True):
    import time
    now = time.time()
    next_run = now if first_run_now else now + 86400
    cur2 = conn.cursor()
    cur2.execute("""
        INSERT INTO autoclick_tasks (telegram_id, next_run_ts, last_run_ts, is_enabled, interval_sec, created_ts)
        VALUES (?, ?, NULL, 1, 86400, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            is_enabled=1,
            next_run_ts=excluded.next_run_ts
    """, (user_id, next_run, now))
    conn.commit()


def disable_autoclick_task(user_id: int):
    cur2 = conn.cursor()
    cur2.execute("UPDATE autoclick_tasks SET is_enabled=0 WHERE telegram_id = ?", (user_id,))
    conn.commit()


def refresh_main_menu(tg_id: int, text: str = "Меню обновлено."):
    send_coalesced(
        tg_id,
        key="main_menu",
        text=text,
        reply_markup=build_main_kb(tg_id),
        parse_mode=None,
    )


def autoclick_scheduler_daemon():
    global active_autoclicks, active_autoclicks_lock
    if "active_autoclicks" not in globals():
        active_autoclicks = set()
    if "active_autoclicks_lock" not in globals():
        active_autoclicks_lock = threading.Lock()

    while True:
        try:
            now = time.time()
            cur2 = conn.cursor()
            cur2.execute(
                """
                SELECT telegram_id, next_run_ts, interval_sec
                FROM autoclick_tasks
                WHERE is_enabled = 1 AND next_run_ts <= ?
                """,
                (now,),
            )
            tasks = cur2.fetchall()
            cur2.close()

            for uid, next_ts, interval_sec in tasks:
                uid = int(uid)

                if not is_user_allowed(uid):
                    disable_autoclick_task(uid)
                    try:
                        bot.send_message(
                            chat_id=uid,
                            text="⛔ Автоотклик остановлен: нет доступа. Нажмите «🔐 Запросить доступ».",
                            reply_markup=build_main_kb(uid),
                        )
                    except Exception:
                        pass
                    continue

                with active_autoclicks_lock:
                    if uid in active_autoclicks:
                        continue
                    active_autoclicks.add(uid)

                try:
                    cur_u = conn.cursor()
                    cur_u.execute(
                        "UPDATE autoclick_tasks SET next_run_ts = ? WHERE telegram_id = ?",
                        (time.time() + float(interval_sec or 86400), uid),
                    )
                    conn.commit()
                    cur_u.close()
                except Exception:
                    pass

                def _run(uid_inner: int):
                    try:
                        worker(uid_inner)
                    finally:
                        with active_autoclicks_lock:
                            active_autoclicks.discard(uid_inner)

                Thread(target=_run, args=(uid,), daemon=True).start()

        except Exception:
            pass

        time.sleep(5)


def start_autoclick(update: Update, ctx: CallbackContext):
    q = update.callback_query
    q.answer()
    tg_id = q.from_user.id

    if not is_user_allowed(tg_id):
        try:
            q.message.reply_text(
                "⛔ Доступ к откликам пока не выдан.\n"
                "Нажмите «🔐 Запросить доступ».",
                reply_markup=build_main_kb(tg_id),
            )
        except Exception:
            pass
        return

    enable_autoclick_task(tg_id)
    Thread(target=worker, args=(tg_id,), daemon=True).start()

    try:
        q.message.reply_text("✅ Отклики запущены.", reply_markup=build_main_kb(tg_id))
    except Exception:
        pass



def stop_autoclick_prompt(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    if not is_autoclick_enabled(tg_id):
        return update.message.reply_text(
            "ℹ️ Автоотклик уже отключён.",
            reply_markup=build_main_kb(tg_id)
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data="stop_confirm_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="stop_confirm_no"),
        ]
    ])
    update.message.reply_text(
        "🛑 Остановить автоматический отклик вакансий?",
        reply_markup=kb
    )


def stop_autoclick_confirm_yes(update: Update, ctx: CallbackContext):
    q = update.callback_query
    tg_id = q.message.chat_id
    _stop_event(tg_id).set()
    try:
        q.answer("Остановлено")
    except Exception:
        pass

    disable_autoclick_task(tg_id)

    try:
        ctx.bot.delete_message(chat_id=tg_id, message_id=q.message.message_id)
    except Exception:
        try:
            q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    ctx.bot.send_message(
        chat_id=tg_id,
        text="⏹️ Автоотклик остановлен. Можете запустить снова кнопкой «▶️ Старт».",
        reply_markup=build_main_kb(tg_id)
    )


def stop_autoclick_confirm_no(update: Update, ctx: CallbackContext):
    q = update.callback_query
    tg_id = q.message.chat_id
    try:
        q.answer("Ок, не останавливаем")
    except Exception:
        pass

    try:
        q.edit_message_text("👍 Автоотклик продолжает работать ежедневно.")
    except Exception:
        pass

    try:
        refresh_main_menu(tg_id, text="Оставляю автоотклик включённым.")
    except Exception:
        ctx.bot.send_message(
            chat_id=tg_id,
            text="Оставляю автоотклик включённым.",
            reply_markup=build_main_kb(tg_id)
        )

def ensure_running_workers_schema():
    cur0 = conn.cursor()
    try:
        cur0.execute("""
            CREATE TABLE IF NOT EXISTS running_workers (
                telegram_id INTEGER PRIMARY KEY,
                start_ts REAL NOT NULL
            )
        """)
        conn.commit()
    finally:
        try:
            cur0.close()
        except Exception:
            pass


def worker(tg_id: int):
    ensure_running_workers_schema()

    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO running_workers (telegram_id, start_ts) VALUES (?, ?)",
            (tg_id, time.time()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        bot.send_message(
            chat_id=tg_id,
            text="⚠️ У вас уже запущены отклики. Дождитесь завершения.",
            reply_markup=build_main_kb(tg_id),
        )
        cur.close()
        return

    progress_msg = None
    last_edit_ts = 0.0
    last_text = ""

    def bind_hh_kb():
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🔑 Привязать hh.ru аккаунт")]],
            resize_keyboard=True,
        )

    def make_progress_bar(done: int, total: int, size: int = 10) -> str:
        if total <= 0:
            return "🟩" * size + " 100%"
        pct = int(done / total * 100)
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        filled = int(size * pct / 100)
        return "🟩" * filled + "⬜" * (size - filled) + f" {pct}%"

    def safe_edit(text: str):
        nonlocal last_edit_ts, last_text, progress_msg

        if not progress_msg:
            return

        now = time.time()
        if text == last_text:
            return
        if now - last_edit_ts < 0.6:
            return

        try:
            bot.edit_message_text(
                chat_id=tg_id,
                message_id=progress_msg.message_id,
                text=text,
            )
            last_edit_ts = now
            last_text = text
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        except Exception:
            pass

    class StopNow(Exception):
        pass

    skipped_cover = 0
    skipped_questions = 0
    errors = 0

    def on_progress(done: int, total: int, msg: str):
        nonlocal skipped_cover, skipped_questions, errors

        if not is_autoclick_enabled(tg_id):
            raise StopNow()

        safe_edit(
            "🚀 Идут отклики...\n"
            # f"{msg}\n"
            f"{make_progress_bar(int(done or 0), int(total or 0))}\n"
            f"✅ Откликов: {int(done or 0)}/{int(total or 0)}\n"
            f"⏭️ Пропущено (нужно сопроводительное): {skipped_cover}\n"
            f"⏭️ Пропущено (тест/вопросы): {skipped_questions}\n"
            f"⚠️ Ошибок: {errors}"
        )

    try:
        if not is_user_allowed(tg_id):
            disable_autoclick_task(tg_id)
            bot.send_message(
                chat_id=tg_id,
                text="⛔ Доступ к откликам пока не выдан. Нажмите «🔐 Запросить доступ».",
                reply_markup=build_main_kb(tg_id),
            )
            return

        user = get_user(tg_id) or {}
        cover = (user.get("cover_letter") or "").strip()

        try:
            sessions = json.loads(user.get("hh_sessions_json") or "{}")
            if not isinstance(sessions, dict):
                sessions = {}
        except Exception:
            sessions = {}

        active_key = (user.get("hh_active_account") or "").strip()
        account_key = active_key if active_key in sessions else next(iter(sessions.keys()), "")
        storage_state = sessions.get(account_key)

        if not storage_state:
            disable_autoclick_task(tg_id)
            bot.send_message(
                chat_id=tg_id,
                text="⚠️ hh.ru не привязан. Нажмите «🔑 Привязать hh.ru аккаунт».",
                reply_markup=bind_hh_kb(),
            )
            return

        run_limit = 200
        progress_msg = bot.send_message(
            chat_id=tg_id,
            text=(
                "🚀 Начинаю отклики...\n"
                f"{make_progress_bar(0, run_limit)}\n"
                f"✅ Откликов: 0/{run_limit}\n"
                f"⏭️ Пропущено (нужно сопроводительное): 0\n"
                f"⏭️ Пропущено (тест/вопросы): 0\n"
                f"⚠️ Ошибок: 0"
            ),
        )

        try:
            result = run_hh_apply_vuz_first_from_bot(
                tg_id=tg_id,
                storage_state=storage_state,
                limit=run_limit,
                cover_letter=cover if cover else None,
                config_path="config.json",
                headless=False,
                slow_mo=80,
                on_progress=on_progress,
            )
        except StopNow:
            safe_edit("⏹️ Остановлено пользователем.")
            return

        try:
            new_state = result.get("new_storage_state")
            if isinstance(new_state, dict) and account_key:
                _save_hh_session_to_db(tg_id, account_key, new_state)
        except Exception:
            pass

        applied = int(result.get("applied") or 0)
        skipped_cover = int(result.get("skipped_cover_required") or 0)
        skipped_questions = int(result.get("skipped_questions") or 0)
        errors = int(result.get("errors") or 0)

        safe_edit(
            "✅ Готово!\n"
            f"{make_progress_bar(applied, run_limit)}\n"
            f"✅ Откликов: {applied}/{run_limit}\n"
            f"⏭️ Пропущено (нужно сопроводительное): {skipped_cover}\n"
            f"⏭️ Пропущено (тест/вопросы): {skipped_questions}\n"
            f"⚠️ Ошибок: {errors}"
        )

        bot.send_message(
            chat_id=tg_id,
            text="📄 Вы можете посмотреть свои отклики:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📄 Мои отклики", url=f"{AUTH_BASE}/applicant/negotiations")]]
            ),
        )

    except Exception as e:
        try:
            safe_edit(f"❌ Ошибка в worker(): {e}")
        except Exception:
            pass
        try:
            bot.send_message(chat_id=tg_id, text=f"❌ Ошибка: {e}", reply_markup=build_main_kb(tg_id))
        except Exception:
            pass

    finally:
        try:
            cur.execute("DELETE FROM running_workers WHERE telegram_id = ?", (tg_id,))
            conn.commit()
        except Exception:
            pass
        try:
            cur.close()
        except Exception:
            pass



def force_refresh_main_menu(bot: Bot, chat_id: int):
    from telegram import ReplyKeyboardRemove
    import time

    try:
        bot.send_message(chat_id=chat_id, text="\u200b", reply_markup=ReplyKeyboardRemove(), disable_notification=True)
    except Exception:
        pass

    time.sleep(0.4)

    try:
        bot.send_message(chat_id=chat_id, text="\u200b", reply_markup=build_main_kb(chat_id), disable_notification=True)
    except Exception:
        pass


def _format_time_left(delta_seconds: float) -> str:
    if delta_seconds <= 0:
        return "0 мин."
    if delta_seconds >= 86400:
        days = int(delta_seconds // 86400)
        return f"{days} дн."
    if delta_seconds >= 3600:
        hours = int(delta_seconds // 3600)
        minutes = int((delta_seconds % 3600) // 60)
        return f"{hours} ч. {minutes} мин."
    minutes = int(delta_seconds // 60)
    return f"{minutes} мин."


def validate_city(value: str, hh_headers):
    try:
        resp = requests.get(
            f"{API_BASE}/suggests/areas",
            params={"text": value},
            headers=hh_headers,
            timeout=5
        )
        resp.raise_for_status()
    except Exception:
        return None, "⚠️ Сервис временно недоступен. Попробуйте позже."
    for item in resp.json().get("items", []):
        name = item["text"].split(" (")[0]
        if name.lower() == value.lower():
            return item["id"], None
    return None, f"🏙️ Город «{value}» не найден. Попробуйте ввести ещё раз."


def validate_salary(value: str):
    if not value.isdigit():
        return None, "💰 Введите, пожалуйста, только цифры, например: 50000"
    num = int(value)
    if num <= 0:
        return None, "💰 Значение должно быть больше нуля."
    return num, None


def _make_hh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "HH-User-Agent": "MyPelicanBot/1.0 (me@example.com)"
    }


def validate_industry(value: str, token: str) -> (str | None, str | None):
    url = f"{API_BASE}/industries"
    headers = _make_hh_headers(token)
    try:
        logging.debug("Запрос отраслей: %s", url)
        resp = requests.get(url, headers=headers, timeout=5)
        logging.debug("Ответ отраслей: %s %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()
        cats = resp.json()
    except Exception as e:
        logging.exception("Не удалось получить /industries")
        return None, "⚠️ Справочник отраслей временно недоступен. Попробуйте позже."

    for cat in cats:
        if cat.get("name", "").lower() == value.lower():
            return cat["id"], None
        for sub in cat.get("industries", []):
            if sub.get("name", "").lower() == value.lower():
                return sub["id"], None

    return None, f"🏭 Отрасль «{value}» не найдена. Введите точное название из списка."


def validate_specialization(value: str, token: str) -> (str | None, str | None):
    url = f"{API_BASE}/professional_roles"
    headers = _make_hh_headers(token)
    try:
        logging.debug("Запрос специализаций: %s", url)
        resp = requests.get(url, headers=headers, timeout=5)
        logging.debug("Ответ специализаций: %s %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()
        cats = resp.json().get("categories", [])  # список { id, name, roles: [...] }
    except Exception as e:
        logging.exception("Не удалось получить /professional_roles")
        return None, "⚠️ Справочник специализаций временно недоступен. Попробуйте позже."

    for cat in cats:
        if cat.get("name", "").lower() == value.lower():
            return cat["id"], None
        for role in cat.get("roles", []):
            if role.get("name", "").lower() == value.lower():
                return role["id"], None

    return None, f"🎯 Специальность «{value}» не найдена. Введите точное название из списка."


def handle_text(update: Update, ctx: CallbackContext):
    tg_id = update.effective_chat.id
    text = update.message.text.strip()

    if ctx.user_data.pop("awaiting_cover", False):
        save_field(tg_id, "cover_letter", text)
        update.message.reply_text(
            "✅ Сопроводительное письмо сохранено!"
        )
        ask_cover_letter(update, ctx)
        return

    # # 2) Фильтры
    # field = ctx.user_data.pop("awaiting", None)
    # if field:
    #     hh = HHClient(get_user(tg_id)["hh_token"])
    #
    #     # 2.1) Город
    #     if field == "city":
    #         area_id, err = validate_city(text, hh.headers)
    #         if err:
    #             ctx.user_data["awaiting"] = "city"
    #             return update.message.reply_text(
    #                 err + "\n\nВведите корректное название города:",
    #                 reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True)
    #             )
    #         save_field(tg_id, "city", text)
    #         save_field(tg_id, "area_id", area_id)
    #         update.message.reply_text(
    #             f"✅ Город сохранён: {text}"
    #         )
    #
    #     # 2.2) Зарплата от
    #     elif field == "salary_from":
    #         num, err = validate_salary(text)
    #         if err:
    #             ctx.user_data["awaiting"] = "salary_from"
    #             return update.message.reply_text(
    #                 err + "\n\nВведите, пожалуйста, только цифры, например: 50000",
    #                 reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True)
    #             )
    #         save_field(tg_id, "salary_from", num)
    #         update.message.reply_text(
    #             f"✅ Зарплата от сохранена: {num}"
    #         )
    #
    #     # 2.3) Отрасль
    #     elif field == "industry":
    #         ind_id, err = validate_industry(text, hh.headers)
    #         if err:
    #             ctx.user_data["awaiting"] = "industry"
    #             return update.message.reply_text(
    #                 err + "\n\nВведите точное название отрасли:",
    #                 reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True)
    #             )
    #         save_field(tg_id, "industry", ind_id)
    #         update.message.reply_text(
    #             f"✅ Отрасль сохранена: {text}"
    #         )
    #
    #     # 2.4) Специальность
    #     elif field == "specialization":
    #         spec_id, err = validate_specialization(text, hh.headers)
    #         if err:
    #             ctx.user_data["awaiting"] = "specialization"
    #             return update.message.reply_text(
    #                 err + "\n\nВведите точное название специальности:",
    #                 reply_markup=ReplyKeyboardMarkup([["◀️ Фильтры"]], resize_keyboard=True)
    #             )
    #         save_field(tg_id, "specialization", spec_id)
    #         update.message.reply_text(
    #             f"✅ Специальность сохранена: {text}"
    #         )
    #
    #     # 2.5) Опыт
    #     elif field == "experience":
    #         save_field(tg_id, "experience", text)
    #         update.message.reply_text(
    #             f"✅ Опыт сохранён: {text}"
    #         )
    #
    #     # 2.6) После любого фильтра возвращаем меню фильтров
    #     set_filters(update, ctx)
    #     return



dp.add_handler(CommandHandler("start", start))
dp.add_handler(CallbackQueryHandler(show_video_callback, pattern=r"^show_video$"))

dp.add_handler(MessageHandler(Filters.regex(r"^🔑 Привязать hh.ru аккаунт$"), auth_cmd))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, hh_bind_router), group=-90)

# dp.add_handler(MessageHandler(Filters.regex(r"^🎯 Привязать резюме$"), choose_resume))
dp.add_handler(CallbackQueryHandler(resume_selected, pattern=r"^selres_"))
dp.add_handler(MessageHandler(Filters.regex(r"^❌ Отвязать hh.ru аккаунт$"), unbind_hh))
# dp.add_handler(MessageHandler(Filters.regex(r"^❌ Отвязать резюме$"), unbind_resume))

dp.add_handler(MessageHandler(Filters.regex(r"^◀️ Назад в главное меню$"), back_to_main))

dp.add_handler(MessageHandler(Filters.regex(r"^⚙️ Настройки$"), settings_menu))
dp.add_handler(MessageHandler(Filters.regex(r"^◀️ Настройки$"), settings_menu))



# dp.add_handler(MessageHandler(
#     Filters.regex(r"^\+7\d{10}$|^[^@\s]+@[^@\s]+\.[^@\s]+$"),
# ))

dp.add_handler(MessageHandler(Filters.regex("^🔐 Запросить доступ$"), request_access_start), group=-81)
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, request_access_process), group=-80)
dp.add_handler(MessageHandler(Filters.regex(r"^▶️ Старт$"), autoclick))
dp.add_handler(MessageHandler(Filters.regex(r"^⏹️ Стоп$"), stop_autoclick_prompt))
dp.add_handler(CallbackQueryHandler(stop_autoclick_confirm_yes, pattern=r"^stop_confirm_yes$"))
dp.add_handler(CallbackQueryHandler(stop_autoclick_confirm_no, pattern=r"^stop_confirm_no$"))
dp.add_handler(CallbackQueryHandler(start_autoclick, pattern=r"^start_autoclick$"))

dp.add_handler(MessageHandler(Filters.regex(r"^❌ Сбросить фильтры$"), reset_filters))
dp.add_handler(MessageHandler(Filters.regex(r"^(✅ Сбросить|❌ Отмена)$"), reset_filters_confirmation))

# dp.add_handler(MessageHandler(Filters.regex(r"^⚙️ Фильтры вакансий$"), set_filters))
# dp.add_handler(MessageHandler(Filters.regex(r"^◀️ Фильтры$"), set_filters))
# dp.add_handler(MessageHandler(Filters.regex(r"^Опыт$"), set_filters_experience))
# dp.add_handler(MessageHandler(Filters.regex(r"^(Без опыта|1[–-]3 года|3[–-]5 лет|>5 лет)$"), save_experience))
# dp.add_handler(MessageHandler(Filters.regex(r"^Город$"), ask_filter_value))
# dp.add_handler(MessageHandler(Filters.regex(r"^Зарплата от$"), ask_filter_value))
# dp.add_handler(MessageHandler(Filters.regex(r"^Отрасль$"), ask_filter_value))
# dp.add_handler(MessageHandler(Filters.regex(r"^Специальность$"), ask_filter_value))

dp.add_handler(MessageHandler(Filters.regex(r"^✉️ Сопроводительное письмо$"), ask_cover_letter))
dp.add_handler(MessageHandler(
    Filters.regex(r"^(✏️ Редактировать|🗑️ Удалить|✅ Да|❌ Нет|◀️ Настройки)$"),
    cover_menu_handler
))

dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# === Start polling ===
if __name__ == "__main__":
    bot.delete_webhook(drop_pending_updates=True)
    Thread(target=run_flask, daemon=True).start()
    Thread(target=autoclick_scheduler_daemon, daemon=True).start()
    Thread(target=access_status_watcher_daemon, daemon=True).start()
    updater.start_polling()
    updater.idle()
