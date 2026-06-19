import re
import json
import asyncio
from pathlib import Path
from typing import Callable, Tuple, Dict, Any, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


# -----------------------------
# CONFIG
# -----------------------------
def load_urls(config_path: str = "config.json") -> Dict[str, str]:
    p = Path(config_path)
    if not p.exists() and not p.is_absolute():
        p = Path(__file__).with_name(config_path)

    cfg = json.loads(p.read_text(encoding="utf-8"))
    urls = cfg.get("urls") or {}
    login = urls.get("login")
    base = urls.get("base")

    if not isinstance(login, str) or not login.strip():
        raise RuntimeError("В config.json нет urls.login")
    if not isinstance(base, str) or not base.strip():
        raise RuntimeError("В config.json нет urls.base")

    return {"login": login.strip(), "base": base.strip()}


# -----------------------------
# HELPERS
# -----------------------------
def is_email(value: str) -> bool:
    v = (value or "").strip()
    return "@" in v and "." in v.split("@")[-1]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")

    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]

    if len(digits) > 10:
        digits = digits[-10:]

    if len(digits) != 10:
        raise ValueError(f"Телефон должен содержать 10 цифр без +7. Получилось: {digits}")

    return digits


async def click_if_visible(page, role: str, name: str, timeout: int = 2000) -> bool:
    loc = page.get_by_role(role, name=name)
    try:
        await loc.first.click(timeout=timeout)
        return True
    except Exception:
        return False


async def ensure_open_login_if_needed(page):
    await click_if_visible(page, "button", "Я ищу работу")
    await click_if_visible(page, "link", "Я ищу работу")

    clicked = await click_if_visible(page, "button", "Войти")
    if not clicked:
        await click_if_visible(page, "link", "Войти")


async def select_auth_mode(page, mode: str):
    if mode == "phone":
        await click_if_visible(page, "tab", "Телефон")
        await click_if_visible(page, "button", "Телефон")
        await click_if_visible(page, "link", "Телефон")
    else:
        await click_if_visible(page, "tab", "Почта")
        await click_if_visible(page, "button", "Почта")
        await click_if_visible(page, "link", "Почта")


async def fill_main_input(page, value: str, mode: str):
    if mode == "phone":
        inp = page.locator('input[data-qa="magritte-phone-input-national-number-input"]').first
        await inp.wait_for(state="visible", timeout=20000)

        await inp.click(force=True)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")

        await inp.type(value, delay=60)

    else:
        inp = page.locator(
            'input:visible:not([type="radio"]):not([type="checkbox"]):not([type="tel"])'
        ).first
        await inp.wait_for(state="visible", timeout=20000)
        await inp.click(force=True)
        await inp.fill(value)


async def fill_sms_code(page, code: str):
    code = (code or "").strip()
    code_input = page.get_by_placeholder(re.compile(r"Введите\s+код", re.I))
    try:
        await code_input.first.click(timeout=3000)
        await code_input.first.fill(code)
        return
    except Exception:
        pass

    inp = page.locator('input:visible:not([type="radio"]):not([type="checkbox"])').first
    await inp.wait_for(state="visible", timeout=20000)
    await inp.click(force=True)
    await inp.fill(code)


class SessionExpiredError(RuntimeError):
    pass


async def _assert_logged_in_or_raise(page, login_url: str):
    u = (page.url or "").lower()
    if login_url.lower() in u or "login" in u:
        raise SessionExpiredError("Сессия невалидна: редирект на логин.")


# -----------------------------
# LOGIN FLOW (для бота)
# -----------------------------
async def hh_login_from_bot(
    request_login: Callable[[], str],
    request_sms: Callable[[], str],
    *,
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 120,
) -> Tuple[Dict[str, Any], str, str]:
    """
    1) Открываем login_url
    2) Ждём логин из TG (request_login)
    3) Вводим
    4) Ждём SMS из TG (request_sms)
    5) Возвращаем (storage_state, mode, account_key)
    """
    urls = load_urls(config_path)
    login_url = urls["login"]
    base_url = urls["base"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(viewport={"width": 430, "height": 900})
        page = await context.new_page()

        page.on("console", lambda m: print(f"[PW console] {m.type}: {m.text}"))
        page.on("pageerror", lambda e: print(f"[PW pageerror] {e}"))

        print(f"[PW] goto login: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded")

        await ensure_open_login_if_needed(page)

        login_value_raw = (await asyncio.to_thread(request_login)).strip()
        mode = "email" if is_email(login_value_raw) else "phone"

        if mode == "phone":
            login_value_to_fill = normalize_phone(login_value_raw)
            account_key = login_value_to_fill  # 10 цифр
        else:
            login_value_to_fill = login_value_raw
            account_key = login_value_raw.lower()

        await select_auth_mode(page, mode)
        await fill_main_input(page, login_value_to_fill, mode)

        clicked = await click_if_visible(page, "button", "Дальше", timeout=8000)
        if not clicked:
            try:
                await page.get_by_role("button", name=re.compile(r"Дальше", re.I)).first.click(timeout=8000)
            except Exception:
                await page.keyboard.press("Enter")

        try:
            await page.wait_for_selector('input:visible:not([type="radio"]):not([type="checkbox"])', timeout=20000)
        except PWTimeoutError:
            raise RuntimeError("Не дождался поля кода. Проверь окно браузера (капча/доп. шаг).")

        sms = (await asyncio.to_thread(request_sms)).strip()
        await fill_sms_code(page, sms)

        await click_if_visible(page, "button", "Дальше", timeout=3000)
        await click_if_visible(page, "button", "Войти", timeout=3000)

        await page.wait_for_timeout(1500)
        try:
            await page.goto(base_url, wait_until="domcontentloaded")
        except Exception:
            pass

        state = await context.storage_state()
        await browser.close()

        print("[PW] login ok, storage_state ready")
        return state, mode, account_key


def run_hh_login_from_bot(
    request_login: Callable[[], str],
    request_sms: Callable[[], str],
    *,
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 120,
):
    return asyncio.run(
        hh_login_from_bot(
            request_login=request_login,
            request_sms=request_sms,
            config_path=config_path,
            headless=headless,
            slow_mo=slow_mo,
        )
    )


async def _login_with_known_credential_async(
    *,
    credential: str,
    mode: str,
    request_sms_code: Callable[[], str],
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 120,
) -> Dict[str, Any]:
    urls = load_urls(config_path)
    login_url = urls["login"]
    base_url = urls["base"]

    if mode not in ("phone", "email"):
        raise ValueError("mode должен быть 'phone' или 'email'")

    if mode == "phone":
        credential_to_fill = normalize_phone(credential)
    else:
        credential_to_fill = (credential or "").strip()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(viewport={"width": 430, "height": 900})
        page = await context.new_page()

        page.on("console", lambda m: print(f"[PW console] {m.type}: {m.text}"))
        page.on("pageerror", lambda e: print(f"[PW pageerror] {e}"))

        print(f"[PW] goto login: {login_url}")
        await page.goto(login_url, wait_until="domcontentloaded")

        await ensure_open_login_if_needed(page)

        await select_auth_mode(page, mode)
        await fill_main_input(page, credential_to_fill, mode)

        clicked = await click_if_visible(page, "button", "Дальше", timeout=8000)
        if not clicked:
            try:
                await page.get_by_role("button", name=re.compile(r"Дальше", re.I)).first.click(timeout=8000)
            except Exception:
                await page.keyboard.press("Enter")

        try:
            await page.wait_for_selector('input:visible:not([type="radio"]):not([type="checkbox"])', timeout=20000)
        except PWTimeoutError:
            raise RuntimeError("Не дождался поля ввода кода (капча/доп. шаг).")

        sms = (await asyncio.to_thread(request_sms_code)).strip()
        await fill_sms_code(page, sms)

        await click_if_visible(page, "button", "Дальше", timeout=3000)
        await click_if_visible(page, "button", "Войти", timeout=3000)

        await page.wait_for_timeout(1500)
        try:
            await page.goto(base_url, wait_until="domcontentloaded")
        except Exception:
            pass

        state = await context.storage_state()
        await browser.close()
        print("[PW] login ok, storage_state ready")
        return state


def hh_login_and_get_storage_state(
    *,
    login_value_raw: Optional[str] = None,
    credential: Optional[str] = None,
    mode: Optional[str] = None,
    request_sms_code: Callable[[], str],
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 120,
) -> Dict[str, Any]:
    if login_value_raw is not None:
        raw = (login_value_raw or "").strip()
        m = "email" if is_email(raw) else "phone"
        cred = raw
        return asyncio.run(
            _login_with_known_credential_async(
                credential=cred,
                mode=m,
                request_sms_code=request_sms_code,
                config_path=config_path,
                headless=headless,
                slow_mo=slow_mo,
            )
        )

    if credential is None:
        raise ValueError("Нужно передать либо login_value_raw=..., либо credential=...")

    cred2 = (credential or "").strip()
    m2 = mode or ("email" if is_email(cred2) else "phone")

    return asyncio.run(
        _login_with_known_credential_async(
            credential=cred2,
            mode=m2,
            request_sms_code=request_sms_code,
            config_path=config_path,
            headless=headless,
            slow_mo=slow_mo,
        )
    )

# -----------------------------
# APPLY FLOW (200 откликов)
# -----------------------------
def _extract_vacancy_key_from_href(href: str) -> str:
    href = href or ""
    m = re.search(r"vacancyId=(\d+)", href)
    if m:
        return f"vacancyId:{m.group(1)}"
    m = re.search(r"/vacancy/(\d+)", href)
    if m:
        return f"vacancy:{m.group(1)}"
    return href.strip()[:200] or "unknown"

async def _click_by_exact_text(scope, text: str, *, timeout: int = 8000) -> bool:
    patterns = [
        ("button", text),
        ("link", text),
    ]

    for role, name in patterns:
        loc = scope.get_by_role(role, name=name)
        if await loc.count():
            try:
                await loc.first.click(timeout=timeout, no_wait_after=True)
                return True
            except Exception:
                try:
                    await loc.first.click(timeout=timeout, force=True, no_wait_after=True)
                    return True
                except Exception:
                    pass

    loc = scope.get_by_text(text, exact=True)
    if await loc.count():
        try:
            await loc.first.click(timeout=timeout, no_wait_after=True)
            return True
        except Exception:
            try:
                await loc.first.click(timeout=timeout, force=True, no_wait_after=True)
                return True
            except Exception:
                pass

    return False


async def _get_show_more_button(page):
    loc = page.get_by_text(re.compile(r"^Посмотреть\s+\d+\s+вакан", re.I))
    if await loc.count():
        btn = page.get_by_role("button", name=re.compile(r"^Посмотреть\s+\d+\s+вакан", re.I))
        if await btn.count():
            return btn.first
        lnk = page.get_by_role("link", name=re.compile(r"^Посмотреть\s+\d+\s+вакан", re.I))
        if await lnk.count():
            return lnk.first
        return loc.first
    return page.locator("xpath=//*[0]")


def _card_from_apply_btn(btn):
    return btn.locator('xpath=ancestor-or-self::*[@data-qa="vacancy-serp__vacancy"]').first


async def _vacancy_key_from_button(btn) -> str:
    href = await btn.get_attribute("href")
    key = _extract_vacancy_key_from_href(href) if href else ""
    if key:
        return key

    card = _card_from_apply_btn(btn)
    if await card.count():
        title_link = card.locator('a[href*="/vacancy/"]').first
        href2 = await title_link.get_attribute("href")
        if href2:
            m = re.search(r"/vacancy/(\d+)", href2)
            if m:
                return f"vacancyId:{m.group(1)}"

    try:
        if await card.count():
            title = await card.inner_text()
            title = (title or "").strip()
            if title:
                return f"card:{hash(title)}"
    except Exception:
        pass

    return "unknown"


async def _wait_applied_marker(card, *, timeout: int = 9000) -> bool:
    try:
        marker = card.get_by_text("Вы откликнулись", exact=False)
        await marker.first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def _handle_foreign_country_warning_if_present(page) -> None:
    try:
        btn_clicked = await _click_by_exact_text(page, "Все равно откликнуться", timeout=3000)
        if btn_clicked:
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def _get_apply_modal(page):
    overlay = page.locator('[data-qa="modal-overlay"]:visible').first
    if await overlay.count():
        if await overlay.get_by_text("Отклик на вакансию", exact=False).count():
            return overlay
        if await overlay.get_by_text("Добавить сопроводительное", exact=False).count():
            return overlay
        if await overlay.get_by_text("Откликнуться", exact=False).count():
            return overlay
    dlg = page.locator('[role="dialog"]:visible').first
    return dlg

async def _wait_for_hh_overlays_to_clear(page, timeout_ms: int = 5000) -> None:
    overlays = page.locator(
        '[data-qa="modal-overlay"]:visible, '
        '[class*="magritte-modal-overlay___"]:visible, '
        '[class*="magritte-overlay___"]:visible'
    )
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        if await overlays.count() == 0:
            return
        await page.wait_for_timeout(100)


async def _top_modal_scope(page):
    modal = page.locator(
        '[data-qa="modal-overlay"]:visible, '
        '[class*="magritte-modal-overlay___"]:visible, '
        '[role="dialog"]:visible, '
        '.bloko-modal:visible'
    ).last
    if await modal.count() > 0:
        return modal
    return None


async def _click_modal_button_by_text(page, modal, label: str, *, exact: bool = True, timeout_ms: int = 6000) -> bool:
    name = re.compile(rf'^{re.escape(label)}$') if exact else label
    loc = modal.get_by_role('button', name=name)
    if await loc.count() == 0:
        return False

    btn = loc.first

    aria_disabled = (await btn.get_attribute('aria-disabled')) or ''
    if aria_disabled.lower() == 'true':
        return False
    if await btn.get_attribute('disabled') is not None:
        return False

    await btn.scroll_into_view_if_needed()
    await _wait_for_hh_overlays_to_clear(page, timeout_ms=1500)

    try:
        await btn.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            await btn.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            return False


async def _modal_cover_letter_required(modal) -> bool:
    txt = modal.locator('text=/Сопроводительное\\s+письмо\\s+обязатель/i')
    if await txt.count() > 0:
        return True

    hint = modal.locator('text=/обязател/i')
    if await hint.count() > 0 and await modal.locator('textarea').count() > 0:
        return True

    return False


async def _fill_cover_letter(modal, cover_letter: str) -> None:
    try:
        if await modal.locator("textarea").count() == 0 and await modal.locator('div[contenteditable="true"]').count() == 0:
            await _click_by_exact_text(modal, "Добавить сопроводительное", timeout=2000)
            await modal.wait_for_timeout(300)
    except Exception:
        pass

    textarea = modal.locator("textarea")
    if await textarea.count():
        await textarea.first.fill(cover_letter)
        return

    rich = modal.locator('div[contenteditable="true"]')
    if await rich.count():
        await rich.first.fill(cover_letter)


async def _drain_hh_modals(page, cover_letter: str | None) -> dict:
    out = {'did_something': False, 'applied_clicked': False, 'skipped_cover': False}

    modal = await _top_modal_scope(page)
    if modal is None:
        return out

    # 1) другая страна
    if await _click_modal_button_by_text(page, modal, 'Все равно откликнуться', exact=True, timeout_ms=8000):
        out['did_something'] = True
        await _wait_for_hh_overlays_to_clear(page, timeout_ms=8000)
        return out

    # 2) сопроводительное обязательно
    if await _modal_cover_letter_required(modal):
        if not cover_letter:
            # закрываем модалку и помечаем как skipped_cover
            if await _click_modal_button_by_text(page, modal, 'Отменить', exact=True, timeout_ms=2000):
                pass
            else:
                try:
                    await page.keyboard.press('Escape')
                except Exception:
                    pass
            out['did_something'] = True
            out['skipped_cover'] = True
            await _wait_for_hh_overlays_to_clear(page, timeout_ms=5000)
            return out

        await _fill_cover_letter_in_modal(page, modal, cover_letter)

    # 3) подтверждаем отклик в модалке
    if await _click_modal_button_by_text(page, modal, 'Откликнуться', exact=True, timeout_ms=12000):
        out['did_something'] = True
        out['applied_clicked'] = True
        await _wait_for_hh_overlays_to_clear(page, timeout_ms=12000)
        return out

    return out


async def _find_next_apply_button(page, processed: set[str]):
    show_more_btn = await _get_show_more_button(page)
    show_more_y = None
    try:
        if await show_more_btn.count():
            box = await show_more_btn.bounding_box()
            if box:
                show_more_y = box.get("y")
    except Exception:
        show_more_y = None

    cards = page.locator('[data-qa="vacancy-serp__vacancy"]')
    total = await cards.count()

    for i in range(total):
        card = cards.nth(i)

        if await card.get_by_text("Вы откликнулись", exact=False).count():
            continue

        btn = card.get_by_role("button", name="Откликнуться")
        if not await btn.count():
            btn = card.get_by_role("link", name="Откликнуться")
        if not await btn.count():
            btn = card.get_by_text("Откликнуться", exact=True)

        if not await btn.count():
            continue

        candidate = btn.first

        if show_more_y is not None:
            try:
                b = await candidate.bounding_box()
                if b and b.get("y") is not None and b["y"] > (show_more_y - 5):
                    continue
            except Exception:
                pass

        key = await _vacancy_key_from_button(candidate)
        if key in processed:
            continue

        return candidate, key, card

    btn = page.get_by_role("button", name="Откликнуться")
    if await btn.count():
        cand = btn.first
        key = await _vacancy_key_from_button(cand)
        if key not in processed:
            return cand, key, _card_from_apply_btn(cand)

    lnk = page.get_by_role("link", name="Откликнуться")
    if await lnk.count():
        cand = lnk.first
        key = await _vacancy_key_from_button(cand)
        if key not in processed:
            return cand, key, _card_from_apply_btn(cand)

    return None, None, None


async def _close_modal_if_present(page):
    modal = page.locator(
        '[role="dialog"]:visible, [data-qa*="modal"]:visible, [data-qa*="popup"]:visible, .bloko-modal:visible'
    ).first
    if await modal.count() == 0:
        return

    for sel in [
        'button[aria-label="Закрыть"]',
        'button[aria-label="Close"]',
        'button:has-text("×")',
        'button:has-text("✕")',
        '[data-qa="modal-close-button"]',
    ]:
        try:
            btn = modal.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(250)
                return
        except Exception:
            continue

    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass

async def _get_apply_modal(page):
    candidates = [
        page.locator('[role="dialog"]:visible').filter(has=page.locator('text=Отклик на вакансию')).first,
        page.locator('[data-qa*="modal"]:visible, [data-qa*="popup"]:visible, .bloko-modal:visible')
            .filter(has=page.locator('text=Отклик на вакансию')).first,
        page.locator('[role="dialog"]:visible, [data-qa*="modal"]:visible, [data-qa*="popup"]:visible, .bloko-modal:visible')
            .filter(has=page.locator('button:has-text("Откликнуться")')).first,
    ]

    for m in candidates:
        try:
            if await m.count() and await m.is_visible():
                return m
        except Exception:
            pass
    return None


async def _modal_requires_cover_letter(modal) -> bool:
    try:
        txt = (await modal.inner_text()) or ""
        low = txt.lower()
        if "сопроводительное" in low and "обязател" in low:
            return True
        if "сопроводительное" in low and ("нужно" in low or "требу" in low):
            return True
        return False
    except Exception:
        return False


async def _is_questions_page(page) -> bool:
    try:
        markers = [
            "Тестовое задание",
            "Вопросы работодателя",
            "Ответьте на вопросы",
            "Перейти к тесту",
            "Тест",
        ]
        for t in markers:
            if await page.get_by_text(t).first.is_visible():
                return True
        return False
    except Exception:
        return False


async def _confirm_foreign_country_if_present(page) -> bool:
    try:
        scope = page
        modal = page.locator('[data-qa="modal-overlay"]:visible, [role="dialog"]:visible').last
        if await modal.count():
            scope = modal

        btn = scope.get_by_role("button", name=re.compile(r"^Все\s+равно\s+откликнуться$", re.I)).first
        if await btn.count() and await btn.is_visible():
            await btn.click(timeout=8000)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass
    return False


async def hh_apply_to_vacancies(
    *,
    storage_state: Dict[str, Any],
    limit: int = 200,
    cover_letter: Optional[str] = None,
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 80,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:

    urls = load_urls(config_path)
    base_url = urls["base"]
    login_url = urls["login"]

    applied = 0
    skipped_cover_required = 0
    skipped_questions = 0
    errors = 0

    cover_letter_text = (cover_letter or "").strip()

    processed: set[str] = set()
    last_clicked_key: Optional[str] = None

    empty_rounds = 0
    MAX_EMPTY_ROUNDS = 25 

    def progress(msg: str):
        if on_progress:
            on_progress(applied, limit, msg)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(storage_state=storage_state)
        page = await context.new_page()

        async def fix_about_blank():
            u = (page.url or "").lower().strip()
            if u == "about:blank" or u.startswith("about:blank"):
                await page.goto(base_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(700)

        async def is_questions_page() -> bool:
            u = (page.url or "")
            if "/applicant/vacancy_response" in u:
                return True
            try:
                if await page.locator('text=Для отклика необходимо ответить').count():
                    return True
                if await page.locator('text=Ответьте на вопросы').count():
                    return True
                if await page.locator('text=Вопросы работодателя').count():
                    return True
                if await page.locator('text=Тестовое задание').count():
                    return True
            except Exception:
                pass
            return False

        async def limit_reached_marker() -> bool:
            try:
                rx = re.compile(r"(лимит.*отклик|исчерпал.*отклик|слишком много отклик|ограничен.*отклик)", re.I)
                return await page.get_by_text(rx).first.is_visible()
            except Exception:
                return False

        async def vacancy_key_from_button(btn) -> str:
            try:
                return await btn.evaluate(
                    """
                    (el) => {
                      const card =
                        el.closest('[data-qa="vacancy-serp__vacancy"]')
                        || el.closest('.vacancy-serp-item')
                        || el.closest('div');

                      const a =
                        (card && (card.querySelector('a[href*="/vacancy/"]')))
                        || (card && card.querySelector('a'))
                        || null;

                      const href = a ? (a.href || a.getAttribute('href') || '') : '';
                      const m = href.match(/\\/vacancy\\/(\\d+)/);

                      if (m && m[1]) return 'vacancy:' + m[1];
                      if (href) return 'href:' + href;

                      const txt = (card ? card.innerText : el.innerText) || '';
                      return 'txt:' + txt.slice(0, 180);
                    }
                    """
                )
            except Exception:
                return "unknown"

        async def click_by_text(scope, text: str, timeout: int = 12000) -> bool:
            try:
                btn = scope.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=timeout)
                    return True
            except Exception:
                pass

            try:
                link = scope.get_by_role("link", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
                if await link.count() and await link.is_visible():
                    await link.click(timeout=timeout)
                    return True
            except Exception:
                pass

            try:
                loc = scope.locator(f'button:has-text("{text}"), a:has-text("{text}")').first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                pass

            return False

        async def close_modal_if_needed():
            modal = page.locator('[data-qa="modal-overlay"]:visible, [role="dialog"]:visible').last
            if not await modal.count():
                return
            for sel in [
                'button[aria-label="Закрыть"]',
                'button[aria-label="Close"]',
                'button:has-text("×")',
                'button:has-text("✕")',
            ]:
                try:
                    b = modal.locator(sel).first
                    if await b.count() and await b.is_visible():
                        await b.click(timeout=3000)
                        await page.wait_for_timeout(300)
                        return
                except Exception:
                    pass
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        async def wait_apply_success() -> bool:
            try:
                await page.locator('text=Вы откликнулись').first.wait_for(timeout=8000)
                return True
            except Exception:
                return False

        async def resolve_modal_after_card_click() -> str:
            start = asyncio.get_event_loop().time()

            while (asyncio.get_event_loop().time() - start) < 35:
                await fix_about_blank()

                if await is_questions_page():
                    try:
                        await page.go_back(wait_until="domcontentloaded")
                    except Exception:
                        await page.goto(base_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(600)
                    return "skip_test"

                if await click_by_text(page, "Все равно откликнуться", timeout=8000):
                    await page.wait_for_timeout(600)
                    continue

                modal = page.locator('[data-qa="modal-overlay"]:visible, [role="dialog"]:visible').last
                if not await modal.count():
                    if await wait_apply_success():
                        return "applied"
                    await page.wait_for_timeout(300)
                    continue

                need_letter = (
                    await modal.locator('text=/сопроводительное\\s+письмо/iu').count()
                    and await modal.locator('text=/обязател/iu').count()
                )

                if need_letter and not cover_letter_text:
                    await close_modal_if_needed()
                    return "skip_cover"

                if cover_letter_text:
                    await click_by_text(modal, "Добавить сопроводительное", timeout=4000)
                    await page.wait_for_timeout(400)
                    ta = modal.locator('textarea:visible, [contenteditable="true"]:visible').first
                    if await ta.count():
                        try:
                            await ta.fill(cover_letter_text)
                        except Exception:
                            pass

                if await click_by_text(modal, "Откликнуться", timeout=15000):
                    await page.wait_for_timeout(600)

                    if await wait_apply_success():
                        return "applied"

                    if await click_by_text(page, "Все равно откликнуться", timeout=6000):
                        await page.wait_for_timeout(600)
                        if await wait_apply_success():
                            return "applied"

                await page.wait_for_timeout(350)

            return "noop"

        progress("Открываю вакансии…")
        await page.goto(base_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)

        progress("Стартую отклики…")

        while applied < limit:
            await fix_about_blank()

            if await is_questions_page():
                try:
                    await page.go_back(wait_until="domcontentloaded")
                except Exception:
                    await page.goto(base_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(700)

            if await limit_reached_marker():
                progress("Похоже, лимит откликов исчерпан — завершаю.")
                break

            if await page.locator('[data-qa="modal-overlay"]:visible, [role="dialog"]:visible').count():
                res = await resolve_modal_after_card_click()

                if res == "applied":
                    applied += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    empty_rounds = 0
                    progress(f"Откликнулся ({applied}/{limit})")

                elif res == "skip_test":
                    skipped_questions += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    empty_rounds = 0
                    progress("Открылся тест/вопросы — пропускаю.")

                elif res == "skip_cover":
                    skipped_cover_required += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    empty_rounds = 0
                    progress("Нужно сопроводительное — пропускаю (нет письма).")

                else:
                    errors += 1
                    progress("Модалка есть, но не смог прожать — пропуск.")
                continue

            btns = page.locator('button:visible:has-text("Откликнуться"), a:visible:has-text("Откликнуться")')
            cnt = await btns.count()
            target = None
            target_key: Optional[str] = None

            for i in range(cnt):
                el = btns.nth(i)
                try:
                    inside_modal = await el.evaluate(
                        "el => !!el.closest('[data-qa=\"modal-overlay\"], [role=\"dialog\"], .bloko-modal')"
                    )
                    if inside_modal:
                        continue
                    if not await el.is_visible():
                        continue

                    key = await vacancy_key_from_button(el)
                    if key in processed:
                        continue

                    target = el
                    target_key = key
                    break
                except Exception:
                    continue

            if not target:
                try:
                    show_more = await _get_show_more_button(page)
                    if await show_more.count() and await show_more.is_visible():
                        await show_more.click(timeout=8000)
                        await page.wait_for_timeout(900)
                        empty_rounds = 0
                        continue
                except Exception:
                    pass

                try:
                    await page.mouse.wheel(0, 1800)
                except Exception:
                    try:
                        await page.evaluate("window.scrollBy(0, 1800)")
                    except Exception:
                        pass

                await page.wait_for_timeout(800)

                empty_rounds += 1
                if empty_rounds < MAX_EMPTY_ROUNDS:
                    continue

                progress("Новых карточек для отклика не найдено — завершаю.")
                break

            empty_rounds = 0

            last_clicked_key = target_key

            try:
                await target.scroll_into_view_if_needed()

                popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=1200))

                try:
                    await target.click(timeout=15000)
                except Exception:
                    await target.click(timeout=15000, force=True)

                try:
                    pop = await popup_task
                    try:
                        await pop.close()
                    except Exception:
                        pass
                except PWTimeoutError:
                    pass
                except Exception:
                    pass

                await page.wait_for_timeout(500)

                res = await resolve_modal_after_card_click()
                if res == "applied":
                    applied += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    progress(f"Откликнулся ({applied}/{limit})")

                elif res == "skip_test":
                    skipped_questions += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    progress("Открылся тест/вопросы — пропускаю вакансию.")

                elif res == "skip_cover":
                    skipped_cover_required += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    progress("Нужно сопроводительное — пропускаю (нет письма).")

                else:
                    errors += 1
                    if last_clicked_key:
                        processed.add(last_clicked_key)
                        last_clicked_key = None
                    progress("После клика не смог нажать в модалке — пропуск.")

            except Exception as e:
                errors += 1
                progress(f"Ошибка — пропускаю ({e})")
                await page.wait_for_timeout(700)

        new_state = await context.storage_state()
        await browser.close()

        return {
            "applied": applied,
            "skipped_cover_required": skipped_cover_required,
            "skipped_questions": skipped_questions,
            "errors": errors,
            "new_storage_state": new_state,
        }


async def _click_and_absorb_popup(page, locator, *, click_timeout: int = 8000, popup_timeout: int = 1200):
    try:
        async with page.expect_popup(timeout=popup_timeout) as pop:
            await locator.click(timeout=click_timeout)
        popup = await pop.value
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return popup
    except PWTimeoutError:
        await locator.click(timeout=click_timeout)
        return None
    except Exception:
        try:
            await locator.click(timeout=click_timeout)
        except Exception:
            pass
        return None


def run_hh_apply_from_bot(
    *,
    storage_state: Dict[str, Any],
    limit: int = 200,
    cover_letter: Optional[str] = None,
    config_path: str = "config.json",
    headless: bool = False,
    slow_mo: int = 80,
    slow_mo_ms: Optional[int] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    import inspect

    effective_slow_mo = slow_mo_ms if slow_mo_ms is not None else slow_mo

    params: Dict[str, Any] = {
        "storage_state": storage_state,
        "limit": limit,
        "cover_letter": cover_letter,
        "config_path": config_path,
        "headless": headless,
        "on_progress": on_progress,
    }

    sig = inspect.signature(hh_apply_to_vacancies)
    if "slow_mo" in sig.parameters:
        params["slow_mo"] = effective_slow_mo
    elif "slow_mo_ms" in sig.parameters:
        params["slow_mo_ms"] = effective_slow_mo

    return asyncio.run(hh_apply_to_vacancies(**params))
