# COLLECTOR V2
# Автоматичний збір офіційних матеріалів про справи з 01.01.2022

import json
import re
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PEOPLE_FILE = DATA / "people.json"
INBOX_FILE = DATA / "inbox.json"
STATE_FILE = DATA / "collector_state.json"

SINCE = datetime(2022, 1, 1)

UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36"

SOURCES = {
    "nabu": "https://nabu.gov.ua",
    "dbr": "https://dbr.gov.ua",
    "sapo": "https://t.me/s/sap_gov_ua",
    "nabu_tg": "https://t.me/s/nab_ukraine",
}

TRIGGERS = [
    "підозр", "обвинувачен", "обвинувальн",
    "вирок", "засуджен", "виправдан",
    "затриман", "арешт", "застава",
    "корупц", "хабар", "неправомірн",
    "незаконн", "розтра", "привласнен",
    "зловживан", "службов", "деклар",
    "незаконне збагачення", "відмиван",
    "легалізаці", "конфіскац",
    "збитк", "заволодін",
]

BAD_NAMES = {
    "новини події",
    "новини події новини",
    "верховний суд",
    "збільшити розмір тексту",
    "перейти до вмісту",
    "читати далі",
    "детальніше",
    "головна сторінка",
}

NAME_RE = re.compile(
    r"\b([А-ЯІЇЄҐ][а-яіїєґ'ʼ-]{2,})\s+"
    r"([А-ЯІЇЄҐ]\.[А-ЯІЇЄҐ]\.)\b"
)

NAME3_RE = re.compile(
    r"\b([А-ЯІЇЄҐ][а-яіїєґ'ʼ-]{2,})\s+"
    r"([А-ЯІЇЄҐ][а-яіїєґ'ʼ-]{2,})\s+"
    r"([А-ЯІЇЄҐ][а-яіїєґ'ʼ-]{2,})\b"
)

session = requests.Session()
session.headers.update({"User-Agent": UA})


def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


people = load_json(PEOPLE_FILE, [])
inbox = load_json(INBOX_FILE, [])
state = load_json(STATE_FILE, {})

if isinstance(people, dict):
    people = people.get("people", [])

if isinstance(inbox, dict):
    inbox = inbox.get("people", [])


def get(url):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(1 + attempt)
    return ""


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def article_date(soup, text):
    candidates = []

    for tag in soup.find_all("time"):
        if tag.get("datetime"):
            candidates.append(tag["datetime"])
        candidates.append(tag.get_text(" ", strip=True))

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if "date" in key or "published" in key:
            if meta.get("content"):
                candidates.append(meta["content"])

    candidates += re.findall(
        r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](202\d)\b",
        text
    )

    for value in candidates:
        m = re.search(r"(202[2-9])[-./](\d{1,2})[-./](\d{1,2})", value)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass

        m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](202[2-9])", value)
        if m:
            try:
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except Exception:
                pass

    return None


def relevant(text):
    low = text.lower()
    return any(x in low for x in TRIGGERS)


def extract_names(text):
    found = set()

    for m in NAME_RE.finditer(text):
        name = clean(" ".join(m.groups()))
        if name.lower() not in BAD_NAMES:
            found.add(name)

    for m in NAME3_RE.finditer(text):
        name = clean(" ".join(m.groups()))
        low = name.lower()

        if low in BAD_NAMES:
            continue

        # Не беремо очевидні заголовки/навігацію
        if any(x in low for x in [
            "новини", "розмір тексту", "перейти",
            "верховний суд", "детальніше"
        ]):
            continue

        found.add(name)

    return sorted(found)


def source_record(url, title, date, source, text):
    return {
        "url": url,
        "title": title[:500],
        "date": date.strftime("%Y-%m-%d") if date else None,
        "source": source,
        "snippet": clean(text)[:1200],
    }


articles = []
seen_urls = set()

# Зберігаємо вже відомі URL
for p in people + inbox:
    for s in p.get("sources", []):
        if isinstance(s, dict) and s.get("url"):
            seen_urls.add(s["url"])


def process_page(url, source):
    if url in seen_urls:
        return 0

    html = get(url)
    if not html:
        return 0

    soup = BeautifulSoup(html, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    date = article_date(soup, text)

    # Старі матеріали не беремо
    if date and date < SINCE:
        return 0

    if not relevant(text):
        return 0

    title = clean(
        soup.title.get_text(" ", strip=True)
        if soup.title else ""
    )

    names = extract_names(text)

    if not names:
        return 0

    rec = source_record(url, title, date, source, text)
    rec["names"] = names
    articles.append(rec)
    seen_urls.add(url)

    return len(names)


def archive_links(base_url, pages, source):
    total = 0

    for page in range(1, pages + 1):
        if page == 1:
            url = base_url
        else:
            url = f"{base_url}?page={page}"

        html = get(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        links = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            text = clean(a.get_text(" ", strip=True))

            if not href.startswith("http"):
                continue

            low = text.lower()

            if any(x in low for x in [
                "підоз", "обвин", "вирок", "затрим",
                "коруп", "хабар", "розтра", "зловжив",
                "незакон", "деклар"
            ]):
                links.add(href)

        for link in links:
            total += process_page(link, source)

        print(f"{source}: page {page}/{pages}, names={total}")

    return total


print("=== KRainaHeroiv Collector V2 ===")
print("Historical backfill since:", SINCE.date())

total = 0

# NABU — основний архів
total += archive_links(
    "https://nabu.gov.ua/news/",
    12,
    "NABU"
)

# NABU Telegram fallback
total += archive_links(
    SOURCES["nabu_tg"],
    80,
    "NABU Telegram"
)

# SAPO official Telegram
total += archive_links(
    SOURCES["sapo"],
    80,
    "SAPO Telegram"
)

# DBR — великий архів
total += archive_links(
    "https://dbr.gov.ua/news",
    180,
    "DBR"
)

print("DISCOVERED NAMES:", total)


def status_from_text(text):
    low = text.lower()

    if "виправдан" in low:
        return "Виправданий / справа завершена"

    if "вирок" in low or "засуджен" in low:
        return "Вирок"

    if "обвинувальн" in low or "обвинувачен" in low:
        return "Обвинувачення"

    if "підозр" in low:
        return "Підозра"

    return "Потребує верифікації"


# Індекс уже наявних людей
index = {
    clean(p.get("name", "")).lower(): p
    for p in people
    if p.get("name")
}

inbox_index = {
    clean(p.get("name", "")).lower(): p
    for p in inbox
    if p.get("name")
}


for article in articles:
    status = status_from_text(article["snippet"])

    for name in article["names"]:
        key = name.lower()

        if key in index:
            p = index[key]

            sources = p.setdefault("sources", [])

            if not any(
                isinstance(s, dict) and s.get("url") == article["url"]
                for s in sources
            ):
                sources.append({
                    "title": article["title"],
                    "url": article["url"],
                    "source": article["source"],
                    "date": article["date"],
                })

            # Не знижуємо вже сильніший статус
            rank = {
                "Потребує верифікації": 0,
                "Підозра": 1,
                "Обвинувачення": 2,
                "Вирок": 3,
                "Виправданий / справа завершена": 3,
            }

            old = p.get("status", "Потребує верифікації")

            if rank.get(status, 0) > rank.get(old, 0):
                p["status"] = status

        else:
            if key not in inbox_index:
                item = {
                    "name": name,
                    "status": status,
                    "summary": "Автоматично виявлено в офіційному матеріалі. Потребує редакційної перевірки.",
                    "role": "",
                    "cases": [],
                    "sources": [{
                        "title": article["title"],
                        "url": article["url"],
                        "source": article["source"],
                        "date": article["date"],
                    }],
                    "needs_review": True,
                }

                inbox.append(item)
                inbox_index[key] = item
            else:
                item = inbox_index[key]
                sources = item.setdefault("sources", [])

                if not any(
                    isinstance(s, dict) and s.get("url") == article["url"]
                    for s in sources
                ):
                    sources.append({
                        "title": article["title"],
                        "url": article["url"],
                        "source": article["source"],
                        "date": article["date"],
                    })


save_json(PEOPLE_FILE, people)
save_json(INBOX_FILE, inbox)

state["last_run"] = datetime.utcnow().isoformat() + "Z"
state["since"] = "2022-01-01"
state["version"] = "2.0"
state["people"] = len(people)
state["inbox"] = len(inbox)
state["articles_added"] = len(articles)

save_json(STATE_FILE, state)

print("PEOPLE:", len(people))
print("INBOX:", len(inbox))
print("ARTICLES:", len(articles))
print("DONE")
