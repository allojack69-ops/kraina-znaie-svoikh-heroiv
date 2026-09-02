#!/usr/bin/env python3
"""Automatic source collector for 'Країна повинна знати своїх героїв'.

Design goal: source-first ingestion. Official pages are collected, existing
profiles are enriched automatically, and new people are added only as
'Потребує верифікації' candidates. The collector never turns an allegation
into a conviction.
"""
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

UA = 'KrainaHeroivCollector/1.0 (+https://github.com/allojack69-ops/kraina-znaie-svoikh-heroiv)'
SOURCES = [
    ('НАБУ', 'https://nabu.gov.ua/news/'),
    ('ВАКС', 'https://first.vaks.gov.ua/'),
    ('АП ВАКС', 'https://ap.vaks.gov.ua/publications/'),
]
TRIGGERS = re.compile(r'(підозр|підозрю|обвинув|судитим|засуд|вирок|виправд|затриман|неправомірн|незаконн|розкрад|збагачен|недекларув)', re.I)
NAME2 = re.compile(r'\b([А-ЯІЇЄҐ][а-яіїєґ’\'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’\'\-]{2,30})\b')
NAME3 = re.compile(r'\b([А-ЯІЇЄҐ][а-яіїєґ’\'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’\'\-]{2,30}\s+[А-ЯІЇЄҐ][а-яіїєґ’\'\-]{2,30})\b')

session = requests.Session()
session.headers.update({'User-Agent': UA, 'Accept-Language': 'uk-UA,uk;q=0.9'})

def get(url: str) -> str:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text

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

def article_links(html: str, base: str, host_hint: str) -> list[tuple[str,str]]:
    soup = BeautifulSoup(html, 'html.parser')
    out=[]; seen=set()
    for a in soup.find_all('a', href=True):
        href=urljoin(base,a['href']); title=clean(a.get_text(' ',strip=True))
        host=urlparse(href).netloc
        if host_hint not in host: continue
        good = ('/news/' in href and 'nabu.gov.ua' in host) or ('/publications/' in href and 'vaks.gov.ua' in host) or ('/news/' in href and 'vaks.gov.ua' in host)
        if not good or not title or len(title)<12: continue
        if href.rstrip('/') in seen: continue
        seen.add(href.rstrip('/')); out.append((title,href))
    return out

def discover():
    items=[]
    # Recent NABU pages. Enough overlap to catch late edits; old years are not fetched every run.
    for page in range(1, 13):
        url='https://nabu.gov.ua/news/' if page==1 else f'https://nabu.gov.ua/news/page{page}/'
        try: items += [('НАБУ',*x) for x in article_links(get(url),url,'nabu.gov.ua')]
        except Exception as e: print('WARN',url,e)
    for base in ['https://first.vaks.gov.ua/','https://ap.vaks.gov.ua/publications/']:
        for page in range(1,4):
            url=base if page==1 else base + ('page/' if base.endswith('/publications/') else '') + str(page) + '/'
            try: items += [('ВАКС' if 'first.' in base else 'АП ВАКС',*x) for x in article_links(get(url),url,'vaks.gov.ua')]
            except Exception as e: print('WARN',url,e)
    ded={}
    for src,title,url in items: ded[url]=(src,title,url)
    return list(ded.values())

def extract_names(text: str, title: str, existing: list[dict]) -> list[str]:
    found=[]
    # First priority: exact names already known in our database.
    for p in existing:
        if norm(p['name']) in norm(text) and p['name'] not in found:
            found.append(p['name'])
    # New candidates: only names occurring close to a legally relevant trigger.
    for m in list(NAME3.finditer(text))+list(NAME2.finditer(text)):
        name=clean(m.group(1))
        # avoid institutional/country noise
        if any(w in name.lower() for w in ['національного','генерального','антикорупційного','верховного','україни','офісу','обласної','міської','районного','державного','міністерства']):
            continue
        left=text[max(0,m.start()-140):m.start()]
        right=text[m.end():m.end()+140]
        if TRIGGERS.search(left+' '+right) and name not in found:
            found.append(name)
    return found[:10]

def article_text(html: str) -> str:
    soup=BeautifulSoup(html,'html.parser')
    for x in soup(['script','style','noscript','svg']): x.decompose()
    main=soup.find('main') or soup.body or soup
    return clean(main.get_text(' ',strip=True))

def main():
    people=json.loads(DATA.read_text(encoding='utf-8'))
    inbox=json.loads(INBOX.read_text(encoding='utf-8')) if INBOX.exists() else []
    state=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'seen_urls':[]}
    seen=set(state.get('seen_urls',[])); now=datetime.now(timezone.utc).date().isoformat()
    links=discover(); print('DISCOVERED',len(links))
    changed=0
    for src,title,url in links:
        if url in seen: continue
        try: html=get(url); text=article_text(html)
        except Exception as e: print('WARN ARTICLE',url,e); continue
        if not TRIGGERS.search(title+' '+text):
            seen.add(url); continue
        names=extract_names(text,title,people)
        status=status_for(title+' '+text)
        for name in names:
            existing=next((p for p in people if norm(p['name'])==norm(name)),None)
            if existing:
                if url not in existing['sources']: existing['sources'].append(url); changed+=1
                # Move only upward in confidence; never downgrade a conviction.
                rank={'Потребує верифікації':0,'Підозра':1,'Обвинувачення':2,'Вирок':3,'Виправданий / справа завершена':3}
                if rank.get(status,0)>rank.get(existing.get('status','Потребує верифікації'),0):
                    existing['status']=status; changed+=1
                if not existing.get('summary'):
                    existing['summary']=f'Автоматично знайдено в офіційному матеріалі {src}: {title}. Перевірка деталей потребує редактора.'
                if title not in existing.get('cases',[]): existing.setdefault('cases',[]).append(f'{title} — {now}.')
                existing['last_verified']=now
            else:
                cand=next((x for x in inbox if norm(x['name'])==norm(name)),None)
                if not cand:
                    cand={'name':name,'status':status,'source_type':src,'first_seen':now,'sources':[url],'articles':[title],'needs_review':True}
                    inbox.append(cand); changed+=1
                else:
                    if url not in cand['sources']: cand['sources'].append(url); cand['articles'].append(title); changed+=1
        seen.add(url)
        time.sleep(0.15)
    # Auto-add candidates to the public JSON only as explicitly unverified profiles.
    existing_names={norm(p['name']) for p in people}
    next_id=max([p.get('id',0) for p in people] or [0])+1
    for cand in inbox:
        if not cand.get('needs_review') or norm(cand['name']) in existing_names: continue
        people.append({'id':next_id,'name':cand['name'],'status':'Потребує верифікації','role':'','summary':f"Автоматично знайдено в офіційному матеріалі {cand['source_type']}. Це не встановлює вини. Профіль очікує редакторської перевірки.",'cases':cand.get('articles',[])[:5],'photo':'','sources':cand.get('sources',[])[:10],'last_verified':now}); existing_names.add(norm(cand['name'])); next_id+=1
    DATA.write_text(json.dumps(people,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    INBOX.write_text(json.dumps(inbox,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    STATE.write_text(json.dumps({'seen_urls':sorted(seen)[-5000:],'last_run':now},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('PEOPLE',len(people),'INBOX',len(inbox),'CHANGED',changed)

if __name__=='__main__': main()
