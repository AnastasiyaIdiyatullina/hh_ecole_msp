#!/usr/bin/env python3
"""Обновляет цены курсов в YML-фиде МШП для HH по данным со страниц курсов.

Как работает:
1. Читает текущий фид (docs/feed.xml) - он же служит источником списка курсов и ссылок.
2. Для каждого оффера скачивает страницу курса (БЕЗ utm-меток, чтобы не плодить
   фиктивные визиты) и ищет на ней пару «старая цена / цена со скидкой».
3. Пересчитывает oldprice, price, discount и ежемесячный платёж.
4. Если что-то изменилось - перезаписывает фид и обновляет дату yml_catalog.

Защита от сбоев: при ошибке парсинга у оффера остаются прежние значения;
если не получилось обработать слишком много страниц, фид не перезаписывается,
а скрипт завершается с кодом 2 (GitHub Actions пришлёт письмо о падении).
"""
import argparse
import datetime
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

MSK = datetime.timezone(datetime.timedelta(hours=3))
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 FeedPriceSync/1.0"
)
MONTHLY_PARAM = "Ежемесячный платеж"
LINK_PARAM = "Ссылка на курс"

MAX_CHANGE = 0.5        # цена не может измениться больше чем на 50% за один прогон
MIN_OK_SHARE = 0.7      # минимум успешно обработанных страниц, иначе фид не трогаем
DISCOUNT_TOL = 0.04     # допуск при сверке со скидкой из прежнего фида
REQUEST_DELAY = 1.0     # пауза между запросами, сек

NUM = r"(\d{1,3}(?:[\s  ]\d{3})+|\d{3,6})"
CUR = r"\s*(?:₽|руб\.?|р\.|р\b)"
RE_MONEY_TEXT = re.compile(NUM + CUR, re.I)
RE_MONTHLY = re.compile(NUM + CUR + r"\s*(?:/|в|за)?\s*мес", re.I)
RE_JSON_PRICE = re.compile(
    r'"(?:price|old_?price|full_?price|base_?price|discount_?price|new_?price|'
    r'price_?old|price_?new|cost)"\s*:\s*"?(\d{3,6})(?:\.\d+)?"?',
    re.I,
)
RE_META_PRICE = re.compile(
    r'(?:itemprop="price"|property="product:price:amount")[^>]*?content="(\d{3,6})(?:\.\d+)?"',
    re.I,
)


# ---------- сеть ----------

def clean_url(url: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc, s.path, "", ""))


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                r.encoding = r.encoding or "utf-8"
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * attempt)
    raise RuntimeError(f"не удалось загрузить {url}: {last}")


# ---------- парсинг ----------

def to_int(s: str) -> int:
    return int(re.sub(r"[\s  ]", "", s))


def strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = (raw.replace("&nbsp;", " ").replace("&#8381;", "₽")
              .replace("&thinsp;", " ").replace("&amp;", "&"))
    return re.sub(r"\s+", " ", raw)


def collect_candidates(raw_html: str) -> Counter:
    """Все числа, похожие на цену: из видимого текста, JSON в скриптах и meta."""
    c: Counter = Counter()
    text = strip_html(raw_html)
    for m in RE_MONEY_TEXT.finditer(text):
        c[to_int(m.group(1))] += 1
    for m in RE_JSON_PRICE.finditer(raw_html):
        c[int(m.group(1))] += 1
    for m in RE_META_PRICE.finditer(raw_html):
        c[int(m.group(1))] += 1
    return Counter({k: v for k, v in c.items() if 1000 <= k <= 1_000_000})


def find_monthly(raw_html: str, price: int):
    text = strip_html(raw_html)
    vals = [to_int(m.group(1)) for m in RE_MONTHLY.finditer(text)]
    vals = [v for v in vals if 0 < v < price]
    return Counter(vals).most_common(1)[0][0] if vals else None


def find_pair(cands: Counter, prev_old: int, prev_price: int, prev_discount: int):
    """Возвращает (oldprice, price, уверенность) или None.

    Новая цена - всегда самое частое число на странице (цена повторяется в нескольких
    блоках, посторонние числа вроде «до 150 000 ₽ в месяц» встречаются один раз).

    high      - старая цена найдена по прежней пропорции скидки;
    medium    - старая цена осталась прежней, а новая (скидка) изменилась;
    unchanged - цена на странице совпала с прежней, старую цену подтвердить не удалось,
                но менять нечего.
    """
    if not cands:
        return None
    top = max(cands.values())
    news = sorted(v for v, c in cands.items() if c == top)
    ratio = 1 - prev_discount / 100

    best = None
    for new in news:
        for old in cands:
            if old > new and abs(new / old - ratio) <= DISCOUNT_TOL:
                score = (cands[old], old)
                if best is None or score > best[0]:
                    best = (score, old, new)
    if best:
        return best[1], best[2], "high"

    for new in news:
        if prev_old in cands and 0.4 <= new / prev_old <= 0.9:
            return prev_old, new, "medium"

    if prev_price in news:
        return prev_old, prev_price, "unchanged"
    return None


# ---------- фид ----------

def offer_param(offer, name):
    for p in offer.findall("param"):
        if p.get("name") == name:
            return p
    return None


def snapshot(offer):
    m = offer_param(offer, MONTHLY_PARAM)
    return {
        "oldprice": offer.findtext("oldprice"),
        "price": offer.findtext("price"),
        "discount": offer.findtext("discount"),
        "monthly": m.text if m is not None else None,
    }


def write_feed(root, path: Path):
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n", encoding="utf-8")


def process_offer(session, offer, debug=False):
    """Возвращает (новые значения | None, сообщение-предупреждение | None)."""
    name = offer.findtext("name")
    link = offer_param(offer, LINK_PARAM)
    if link is None or not link.text:
        return None, f"{name}: нет ссылки на курс"
    url = clean_url(link.text.strip())

    try:
        raw = fetch(session, url)
    except RuntimeError as e:
        return None, f"{name}: {e}"

    cands = collect_candidates(raw)
    if debug:
        print(f"[debug] {name}\n  url: {url}\n  кандидаты: {dict(cands.most_common(15))}")

    prev_old = int(offer.findtext("oldprice"))
    prev_price = int(offer.findtext("price"))
    prev_disc = int(offer.findtext("discount") or 0)

    pair = find_pair(cands, prev_old, prev_price, prev_disc)
    if not pair:
        return None, f"{name}: на странице не найдены цены (возможно, поменялась вёрстка или цены рисуются JS)"
    old, new, conf = pair
    if debug:
        print(f"  пара: old={old} price={new} уверенность={conf}")

    change = abs(new - prev_price) / prev_price
    limit = 1.0 if conf == "high" else MAX_CHANGE
    if change > limit:
        return None, (f"{name}: цена изменилась слишком сильно ({prev_price} -> {new}), "
                      f"проверьте вручную")

    note = None
    if change > 0.3:
        note = f"большое изменение цены {prev_price} -> {new}, проверьте на сайте"
    if conf == "unchanged":
        note = "старую цену на странице подтвердить не удалось, оставлена прежняя"

    monthly = find_monthly(raw, new)
    return {
        "oldprice": old,
        "price": new,
        "discount": round((1 - new / old) * 100),
        "monthly": monthly,
        "note": note,
    }, None


def apply_values(offer, v):
    offer.find("oldprice").text = str(v["oldprice"])
    offer.find("price").text = str(v["price"])
    offer.find("discount").text = str(v["discount"])
    if v["monthly"] is not None:
        p = offer_param(offer, MONTHLY_PARAM)
        if p is not None:
            p.text = str(v["monthly"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", default="docs/feed.xml")
    ap.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    ap.add_argument("--debug", action="store_true", help="показать найденные числа по каждой странице")
    args = ap.parse_args()

    path = Path(args.feed)
    tree = ET.parse(path)
    root = tree.getroot()
    offers = root.findall("./shop/offers/offer")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})

    ok, warnings, changes = 0, [], []
    for i, offer in enumerate(offers):
        if i:
            time.sleep(REQUEST_DELAY)
        before = snapshot(offer)
        values, warn = process_offer(session, offer, args.debug)
        if warn:
            warnings.append(warn)
            continue
        ok += 1
        if values.get("note"):
            print(f"::warning::{offer.findtext('name')}: {values['note']}")
        apply_values(offer, values)
        after = snapshot(offer)
        if after != before:
            changes.append((offer.findtext("name"), before, after))

    total = len(offers)
    print(f"Обработано страниц: {ok}/{total}")
    for w in warnings:
        print(f"::warning::{w}")

    if total == 0 or ok / total < MIN_OK_SHARE:
        print("::error::Слишком мало страниц обработано, фид не перезаписан")
        return 2

    if not changes:
        print("Цены не изменились, фид не перезаписываю.")
        return 0

    for name, b, a in changes:
        print(f"* {name}: {b['oldprice']}/{b['price']} (-{b['discount']}%, {b['monthly']}/мес) "
              f"-> {a['oldprice']}/{a['price']} (-{a['discount']}%, {a['monthly']}/мес)")

    if args.dry_run:
        print("dry-run: файл не записан")
        return 0

    root.set("date", datetime.datetime.now(MSK).replace(microsecond=0).isoformat())
    write_feed(root, path)
    print(f"Фид обновлён: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
