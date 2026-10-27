#!/usr/bin/env python3
"""Telegram-бот: щодня постить зміни значень предметів з Vaulted Values X.

Запуск:
    python bot.py            # працює постійно, постить щодня о POST_TIME
    python bot.py --now      # одразу один пост (для перевірки)
    python bot.py --dry-run  # показати текст у консолі, нічого не слати
"""
import argparse
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"


def load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")  # @my_channel або -100xxxxxxxxxx
POST_TIME = os.getenv("POST_TIME", "12:00")  # HH:MM
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
VALUE_LIST_URL = os.getenv("VALUE_LIST_URL", "https://cameraman-td.vaultedvaluesx.com/value-list")
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "0") == "1"  # 1 = завантажити всі 47 предметів
POST_IF_NO_CHANGES = os.getenv("POST_IF_NO_CHANGES", "0") == "1"
TITLE = os.getenv("POST_TITLE", "📊 Изменения цен за сутки")
CHAT_OVERRIDE = os.getenv("CHAT_OVERRIDE", "").strip()  # если задан — шлём только список цен в этот чат

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
log = logging.getLogger("valuebot")


# ---------- отримання даних ----------
def fetch_html() -> str:
    if USE_PLAYWRIGHT:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=UA)
            page.goto(VALUE_LIST_URL, wait_until="networkidle")
            for _ in range(30):
                btn = page.get_by_text("Load more items")
                if btn.count() == 0:
                    break
                btn.first.click()
                page.wait_for_timeout(800)
            content = page.content()
            browser.close()
            return content
    r = requests.get(VALUE_LIST_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def _item_links(node):
    return {a["href"].split("?")[0] for a in node.select('a[href*="/items/item-"]')}


def parse_items(page_html: str) -> dict:
    """Повертає {item_id: {"name": str, "value": float}}."""
    soup = BeautifulSoup(page_html, "html.parser")
    items = {}
    for a in soup.select('a[href*="/items/item-"]'):
        href = a["href"].split("?")[0]
        item_id = href.rsplit("/", 1)[-1]
        if item_id in items:
            continue
        # піднімаємось до контейнера картки (де лише один предмет)
        card = a
        while card.parent is not None and len(_item_links(card.parent)) == 1:
            card = card.parent
        name = next(
            (x.get_text(" ", strip=True) for x in card.select(f'a[href*="{item_id}"]') if x.get_text(strip=True)),
            None,
        )
        m = re.search(r"Value\s*([\d][\d,]*(?:\.\d+)?)", card.get_text(" ", strip=True))
        if not name or not m:
            continue
        items[item_id] = {"name": name, "value": float(m.group(1).replace(",", ""))}
    return items


# ---------- стан і порівняння ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def build_message(old: dict, new: dict):
    up, down, added = [], [], []
    for iid, item in new.items():
        name = html.escape(item["name"])
        if iid in old:
            a, b = old[iid]["value"], item["value"]
            if a != b:
                line = f"{name}: {fmt(a)} → {fmt(b)}"
                (up if b > a else down).append((abs(b - a), line))
        elif USE_PLAYWRIGHT and old:  # "нові" лише якщо бачимо повний список
            added.append(f"{name}: {fmt(item['value'])}")

    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    parts = [f"<b>{html.escape(TITLE)}</b>"]
    if down:
        body = "\n".join(l for _, l in sorted(down, reverse=True))
        parts.append(f"\n🔴 <b>Упали</b>\n<blockquote>{body}</blockquote>")
    if up:
        body = "\n".join(l for _, l in sorted(up, reverse=True))
        parts.append(f"\n🟢 <b>Выросли</b>\n<blockquote>{body}</blockquote>")
    if added:
        parts.append("\n🆕 <b>Новые</b>\n<blockquote>" + "\n".join(added) + "</blockquote>")
    if not (up or down or added):
        parts.append("\nИзменений за сутки нет.")

    ranked = sorted(new.values(), key=lambda i: i["value"], reverse=True)
    full = "\n".join(f"{html.escape(i['name'])}: {fmt(i['value'])}" for i in ranked)
    parts.append(f"\n💰 <b>Все цены на момент публикации ({now})</b>\n<blockquote expandable>{full}</blockquote>")
    return "\n".join(parts)


def build_prices_only(new: dict) -> str:
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    ranked = sorted(new.values(), key=lambda i: i["value"], reverse=True)
    full = "\n".join(f"{html.escape(i['name'])}: {fmt(i['value'])}" for i in ranked)
    return f"<b>💰 Актуальные цены ({now})</b>\n<blockquote expandable>{full}</blockquote>"


# ---------- Telegram ----------
def send_telegram(text: str, chat_id: str = ""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for chunk in chunks:
        r = requests.post(
            url,
            json={"chat_id": chat_id or CHANNEL_ID, "text": chunk.strip(), "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


# ---------- основна робота ----------
def run_job(dry_run=False):
    new = parse_items(fetch_html())
    if not new:
        log.error("Не вдалося розпарсити предмети — структура сайту могла змінитись.")
        return
    if CHAT_OVERRIDE:  # ручной запрос из личных сообщений: без сравнения и без сохранения
        send_telegram(build_prices_only(new), chat_id=CHAT_OVERRIDE)
        log.info("Список цен отправлен в личный чат.")
        return
    state = load_state()
    old = state.get("items", {})
    msg = build_message(old, new)

    if msg is None and POST_IF_NO_CHANGES:
        msg = f"<b>{html.escape(TITLE)}</b>\n\nЗмін немає."
    if msg is None:
        log.info("Змін немає (%d предметів), пост пропущено.", len(new))
    elif dry_run:
        print(msg)
    else:
        send_telegram(msg)
        log.info("Пост відправлено.")

    if not dry_run:
        merged = {**old, **new}  # зберігаємо й ті, що не потрапили у видачу
        save_state({"updated": datetime.now().isoformat(), "items": merged})


def seconds_until(post_time: str, tz: ZoneInfo) -> float:
    h, m = map(int, post_time.split(":"))
    now = datetime.now(tz)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="виконати один раз і вийти")
    ap.add_argument("--dry-run", action="store_true", help="не слати в Telegram, лише показати")
    args = ap.parse_args()

    if not args.dry_run and (not BOT_TOKEN or not CHANNEL_ID):
        raise SystemExit("Заповни BOT_TOKEN і CHANNEL_ID у .env")

    if args.now or args.dry_run:
        run_job(dry_run=args.dry_run)
        return

    tz = ZoneInfo(TIMEZONE)
    if not STATE_FILE.exists():
        log.info("Перший запуск: зберігаю початкові значення.")
        run_job()
    while True:
        wait = seconds_until(POST_TIME, tz)
        log.info("Наступний пост о %s (%s), через %.0f хв.", POST_TIME, TIMEZONE, wait / 60)
        time.sleep(wait)
        try:
            run_job()
        except Exception:
            log.exception("Помилка під час публікації")
        time.sleep(5)


if __name__ == "__main__":
    main()
