#!/usr/bin/env python3
"""Automatic source collector for 'Країна повинна знати своїх героїв'."""
from __future__ import annotations
import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'people.json'
INBOX = ROOT / 'data' / 'inbox.json'
STATE = ROOT / 'data' / 'collector_state.json'

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 '
      'KrainaHeroivCollector/1.2')
session = requests.Session()
session.headers.update({
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.7'
})

TRIGGERS = re.compile(
    r'(підозр|підозрю|обвинув|судитим|засуд|вирок|виправд|затриман|'
    r'неправомірн|незаконн|розкрад|збагачен|недекларув)', re.I
)
NAME2 = re.compile(r"\b([А-ЯІЇЄҐ][а-яіїєґ’'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’'\-]{2,30})\b")
NAME3 = re.compile(r"\b([А-ЯІЇЄҐ][а-яіїєґ’'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’'\-]{2,30})\b")

BAD_NAMES = {
    'новини події', 'новини події новини', 'верховний суд',
    'збільшити розмір тексту', 'перейти до вмісту'
}
BAD_ARTICLE_TITLES = {'збільшити розмір тексту', 'перейти до вмісту'}

def get(url: str) -> str:
    last = None
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
    raise last

def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()

def norm(s: str) -> str:
    return re.sub(r'[^а-яіїєґa-z0-9]', '', s.lower())

def status_for(text: str) -> str:
    t = text.lower()
    if 'виправд' in t:
        return 'Виправданий / справа завершена'
    if 'вирок' in t or 'засуджен' in t:
        return 'Вирок'
    if 'обвинув' in t or 'судитим' in t or 'до суду' in t:
        return 'Обвинувачення'
    if 'підозр' in t or 'затриман' in t:
        return 'Підозра'
    return 'Потребує верифікації'

def article_links(html: str, base: str, host_hint: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, 'html.parser')
    out, seen = [], set()
    base_path = urlparse(base).path.rstrip('/')
    for a in soup.find_all('a', href=True):
        href = urljoin(base, a['href']).split('#', 1)[0]
        title = clean(a.get_text(' ', strip=True))
        host = urlparse(href).netloc
        path = urlparse(href).path.rstrip('/')
        if host_hint not in host or not title or len(title) < 12:
            continue
        if title.lower() in BAD_ARTICLE_TITLES:
            continue
        # Never treat listing/pagination/navigation pages as individual cases.
        if path == base_path or '/page/' in path or path.endswith('/publications'):
            continue
        good = (
            ('/news/' in path and 'nabu.gov.ua' in host) or
            ('/publications/' in path and 'vaks.gov.ua' in host) or
            ('/news/' in path and 'vaks.gov.ua' in host)
        )
        if not good or href.rstrip('/') in seen:
            continue
        seen.add(href.rstrip('/'))
        out.append((title, href))
    return out

def telegram_links(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    out, seen = [], set()
    for msg in soup.select('.tgme_widget_message'):
        text = clean(msg.get_text(' ', strip=True))
        if not text or not TRIGGERS.search(text):
            continue
        a = msg.select_one('.tgme_widget_message_date')
        if not a or not a.get('href'):
            continue
        post_url = a['href']
        if post_url in seen:
            continue
        seen.add(post_url)
        source_url = post_url
        for link in msg.find_all('a', href=True):
            href = urljoin(post_url, link['href'])
            if 'nabu.gov.ua' in urlparse(href).netloc:
                source_url = href
                break
        title = clean(text.split('—', 1)[0]) if len(text) < 180 else text[:180]
        out.append((title, source_url, post_url, text))
    return out[:80]

def discover():
    items = []
    for page in range(1, 13):
        url = 'https://nabu.gov.ua/news/' if page == 1 else f'https://nabu.gov.ua/news/page{page}/'
        try:
            items += [('НАБУ', *x) for x in article_links(get(url), url, 'nabu.gov.ua')]
        except Exception as e:
            print('WARN NABU WEBSITE', url, e)

    try:
        tg = 'https://t.me/s/nab_ukraine'
        for title, source_url, post_url, text in telegram_links(get(tg)):
            items.append(('НАБУ Telegram', title, source_url, post_url, text))
        print('NABU TELEGRAM FALLBACK OK')
    except Exception as e:
        print('WARN NABU TELEGRAM', e)

    for base in ['https://first.vaks.gov.ua/', 'https://ap.vaks.gov.ua/publications/']:
        for page in range(1, 4):
            url = base if page == 1 else base + ('page/' if base.endswith('/publications/') else '') + str(page) + '/'
            try:
                items += [('ВАКС' if 'first.' in base else 'АП ВАКС', *x)
                          for x in article_links(get(url), url, 'vaks.gov.ua')]
            except Exception as e:
                print('WARN VAKS', url, e)

    ded = {}
    for item in items:
        ded[item[2]] = item
    return list(ded.values())

def extract_names(text: str, title: str, existing: list[dict]) -> list[str]:
    found = []
    bad_norm = {norm(x) for x in BAD_NAMES}

    for p in existing:
        if norm(p['name']) in norm(text) and p['name'] not in found:
            found.append(p['name'])

    for m in list(NAME3.finditer(text)) + list(NAME2.finditer(text)):
        name = clean(m.group(1))
        if norm(name) in bad_norm:
            continue
        if any(w in name.lower() for w in [
            'національного', 'генерального', 'антикорупційного', 'верховного',
            'україни', 'офісу', 'обласної', 'міської', 'районного',
            'державного', 'міністерства'
        ]):
            continue
        left = text[max(0, m.start()-140):m.start()]
        right = text[m.end():m.end()+140]
        if TRIGGERS.search(left + ' ' + right) and name not in found:
            found.append(name)

    return found[:10]

def article_text(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for x in soup(['script', 'style', 'noscript', 'svg']):
        x.decompose()
    main = soup.find('main') or soup.body or soup
    return clean(main.get_text(' ', strip=True))

def main():
    people = json.loads(DATA.read_text(encoding='utf-8'))
    inbox = json.loads(INBOX.read_text(encoding='utf-8')) if INBOX.exists() else []

    # Remove the three navigation false positives created by collector v1.1.
    bad_norm = {norm(x) for x in BAD_NAMES}
    people = [p for p in people if norm(p.get('name', '')) not in bad_norm]
    inbox = [x for x in inbox if norm(x.get('name', '')) not in bad_norm]

    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'seen_urls': []}
    seen = set(state.get('seen_urls', []))
    now = datetime.now(timezone.utc).date().isoformat()

    links = discover()
    print('DISCOVERED', len(links))
    changed = 0

    for item in links:
        src, title, url = item[:3]
        telegram_text = item[4] if len(item) > 4 else ''

        if url in seen:
            continue

        try:
            text = telegram_text if src == 'НАБУ Telegram' else article_text(get(url))
        except Exception as e:
            print('WARN ARTICLE', url, e)
            seen.add(url)
            continue

        if not TRIGGERS.search(title + ' ' + text):
            seen.add(url)
            continue

        names = extract_names(text, title, people)
        status = status_for(title + ' ' + text)

        for name in names:
            existing = next((p for p in people if norm(p['name']) == norm(name)), None)

            if existing:
                if url not in existing.get('sources', []):
                    existing.setdefault('sources', []).append(url)
                    changed += 1

                rank = {
                    'Потребує верифікації': 0,
                    'Підозра': 1,
                    'Обвинувачення': 2,
                    'Вирок': 3,
                    'Виправданий / справа завершена': 3
                }
                if rank.get(status, 0) > rank.get(existing.get('status', 'Потребує верифікації'), 0):
                    existing['status'] = status
                    changed += 1

                if not existing.get('summary'):
                    existing['summary'] = (
                        f'Автоматично знайдено в офіційному матеріалі {src}: {title}. '
                        'Перевірка деталей потребує редактора.'
                    )
                if title not in existing.get('cases', []):
                    existing.setdefault('cases', []).append(f'{title} — {now}.')
                existing['last_verified'] = now

            else:
                cand = next((x for x in inbox if norm(x.get('name', '')) == norm(name)), None)
                if not cand:
                    inbox.append({
                        'name': name,
                        'status': status,
                        'source_type': src,
                        'first_seen': now,
                        'sources': [url],
                        'articles': [title],
                        'needs_review': True
                    })
                    changed += 1
                else:
                    if url not in cand.get('sources', []):
                        cand['sources'].append(url)
                        cand.setdefault('articles', []).append(title)
                        changed += 1

        seen.add(url)
        time.sleep(0.15)

    # IMPORTANT: new people are NEVER published automatically.
    # They stay in inbox until editorial verification.
    DATA.write_text(json.dumps(people, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    STATE.write_text(
        json.dumps({'seen_urls': sorted(seen)[-5000:], 'last_run': now},
                   ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8'
    )
    print('PEOPLE', len(people), 'INBOX', len(inbox), 'CHANGED', changed)

if __name__ == '__main__':
    main()
