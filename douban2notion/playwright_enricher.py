#!/usr/bin/env python3
"""Enrich Notion databases by scraping Douban pages with Playwright.
Covers Actor/Director photos and Book covers."""
import json, os, sys, re, time, html as html_mod, urllib.request
import requests, pendulum
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(line_buffering=True)

MOVIE_TOKEN = os.getenv('MOVIE_NOTION_TOKEN', os.getenv('MOVIE_TOKEN', ''))
BOOK_TOKEN = os.getenv('BOOK_NOTION_TOKEN', os.getenv('BOOK_TOKEN', ''))

DB_CONFIG = {
    'Actor': {
        'id': '1d7119b0-97c7-8150-b869-ec25d47734fe', 'token': MOVIE_TOKEN,
        'person_prop': '电影', 'cover_prop': 'Photo', 'status_prop': 'PhotoStatus',
        'checked_prop': 'PhotoCheckedAt', 'source_prop': 'PhotoSource',
    },
    'Director': {
        'id': '1d7119b0-97c7-8167-a800-f8840ca98d02', 'token': MOVIE_TOKEN,
        'person_prop': '电影', 'cover_prop': 'Photo', 'status_prop': 'PhotoStatus',
        'checked_prop': 'PhotoCheckedAt', 'source_prop': 'PhotoSource',
    },
    'Book': {
        'id': '1d8119b0-97c7-8166-a0e9-d9b649f7f6f9', 'token': BOOK_TOKEN,
        'cover_prop': 'Cover', 'status_prop': 'CoverStatus',
        'checked_prop': 'CoverCheckedAt', 'source_prop': 'CoverSource',
    },
    'Author': {
        'id': '1d8119b0-97c7-812c-a5ce-f242ddf11555', 'token': BOOK_TOKEN,
        'person_prop': '书籍', 'cover_prop': 'Photo', 'status_prop': 'PhotoStatus',
        'checked_prop': 'PhotoCheckedAt', 'source_prop': 'PhotoSource',
    },
}

DOUBAN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'https://movie.douban.com/',
}


# ─── Notion helpers ─────────────────────────────────────────────

def notion_query(db_id, token):
    results = []
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        req = urllib.request.Request(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            data=json.dumps(body).encode(),
            headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results.extend(data['results'])
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def notion_get_page(page_id, token):
    req = urllib.request.Request(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28'}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_name(page):
    t = (page.get('properties') or {}).get('Name', {}).get('title', [])
    return html_mod.unescape(t[0]['plain_text']) if t else None


def get_field(page, field):
    v = (page.get('properties') or {}).get(field, {}).get('rich_text', [])
    return html_mod.unescape(v[0]['plain_text']) if v else None


def get_relation_ids(page, prop_name):
    rel = (page.get('properties') or {}).get(prop_name, {}).get('relation', [])
    return [r['id'] for r in rel]


def get_douban_subject_id(page):
    db_url = (page.get('properties') or {}).get('DB_Url', {}).get('url', '')
    if db_url:
        match = re.search(r'subject/(\d+)', db_url)
        if match:
            return match.group(1)
    return None


# ─── Playwright scraping ────────────────────────────────────────

def scrape_douban_celebrities(subject_id):
    """Scrape celebrity photos from a Douban movie page."""
    url = f'https://movie.douban.com/subject/{subject_id}/'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            time.sleep(6)
            content = page.content()
        except Exception:
            browser.close()
            return []
        browser.close()

    if len(content) < 5000:
        return []

    celeb_pattern = r'<li class="celebrity">\s*<a[^>]*title="([^"]+)"[^>]*>\s*<div class="avatar"[^>]*style="background-image:\s*url\(([^)]+)\)"'
    matches = re.findall(celeb_pattern, content, re.DOTALL)

    results = []
    for full_name, photo_url in matches:
        parts = full_name.split(' ', 1)
        cn_name = parts[0]
        en_name = parts[1] if len(parts) > 1 else ''
        results.append({'cn_name': cn_name, 'en_name': en_name, 'photo_url': photo_url})
    return results


def scrape_douban_book_cover(subject_id):
    """Scrape book cover from Douban."""
    url = f'https://book.douban.com/subject/{subject_id}/'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            time.sleep(5)
            content = page.content()
        except Exception:
            browser.close()
            return None
        browser.close()

    if len(content) < 5000:
        return None

    cover_pattern = r'<img[^>]*id="mainpic"[^>]*src="([^"]+)"'
    match = re.search(cover_pattern, content)
    if match:
        return match.group(1)

    alt_pattern = r'src="(https://img\d+\.doubanio\.com/view/subject/[^"]*)"'
    matches = re.findall(alt_pattern, content)
    return matches[0] if matches else None


# ─── Image download ─────────────────────────────────────────────

def download_image(url):
    headers = DOUBAN_HEADERS if 'douban' in url else {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code == 200 and 'image' in resp.headers.get('Content-Type', ''):
            return resp.content
    except Exception:
        pass
    return None


# ─── Notion upload ──────────────────────────────────────────────

def notion_upload_binary(token, img_data, filename='photo.jpg'):
    try:
        resp = requests.post(
            'https://api.notion.com/v1/file_uploads',
            json={'mode': 'single_part', 'filename': filename, 'content_type': 'image/jpeg'},
            headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28'},
            timeout=30,
        )
        upload_id = resp.json().get('id')
        if not upload_id:
            return None
    except Exception:
        return None

    try:
        resp = requests.post(
            f'https://api.notion.com/v1/file_uploads/{upload_id}/send',
            files={'file': (filename, img_data, 'image/jpeg')},
            headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28'},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    for _ in range(10):
        time.sleep(1)
        try:
            resp = requests.get(
                f'https://api.notion.com/v1/file_uploads/{upload_id}',
                headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28'},
            )
            status = resp.json().get('status')
            if status == 'uploaded':
                return upload_id
            elif status == 'failed':
                return None
        except Exception:
            return None
    return None


def notion_set_cover(token, page_id, upload_id, source_url, cover_prop, status_prop, checked_prop, source_prop):
    now_str = pendulum.now('Asia/Shanghai').to_datetime_string()
    source_name = 'Douban' if 'douban' in source_url else 'TMDB'
    body = {
        'cover': {'type': 'file_upload', 'file_upload': {'id': upload_id}},
        'icon': {'type': 'file_upload', 'file_upload': {'id': upload_id}},
        'properties': {
            cover_prop: {'files': [{'type': 'external', 'name': cover_prop, 'external': {'url': source_url}}]},
            status_prop: {'select': {'name': 'Ok'}},
            checked_prop: {'date': {'start': now_str, 'time_zone': 'Asia/Shanghai'}},
            source_prop: {'select': {'name': source_name}},
        },
    }
    try:
        resp = requests.patch(
            f'https://api.notion.com/v1/pages/{page_id}',
            json=body,
            headers={'Authorization': f'Bearer {token}', 'Notion-Version': '2022-06-28'},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ─── Enrichment logic ───────────────────────────────────────────

def find_person_photo(person_name, movie_ids, token):
    """Find a person's photo by scraping their linked movies on Douban."""
    for movie_id in movie_ids[:5]:
        try:
            movie_page = notion_get_page(movie_id, token)
        except Exception:
            continue

        subject_id = get_douban_subject_id(movie_page)
        if not subject_id:
            continue

        celebrities = scrape_douban_celebrities(subject_id)
        if not celebrities:
            time.sleep(3)
            continue

        for celeb in celebrities:
            cn_match = celeb['cn_name'] == person_name
            en_match = celeb['en_name'].lower() == person_name.lower() if celeb['en_name'] else False
            if cn_match or en_match:
                return celeb['photo_url']

        time.sleep(3)
    return None


def enrich_person_db(db_name, config, dry_run=False):
    """Enrich Actor/Director photos."""
    db_id = config['id']
    token = config['token']
    person_prop = config['person_prop']
    cover_prop = config['cover_prop']
    status_prop = config['status_prop']
    checked_prop = config['checked_prop']
    source_prop = config['source_prop']

    print(f'\n=== Enriching {db_name} photos ===')
    pages = notion_query(db_id, token)
    needs_work = []
    for p in pages:
        name = get_name(p)
        if name and p.get('cover') is None:
            movie_ids = get_relation_ids(p, person_prop)
            needs_work.append({'page': p, 'name': name, 'movie_ids': movie_ids})

    print(f'Total: {len(pages)}, Needs cover: {len(needs_work)}')

    enriched = 0
    failed = 0
    for i, item in enumerate(needs_work):
        name = item['name']
        page_id = item['page']['id']

        if not item['movie_ids']:
            print(f'  [{i+1}/{len(needs_work)}] {name}: no linked movies')
            failed += 1
            continue

        image_url = find_person_photo(name, item['movie_ids'], token)

        if dry_run:
            status = 'found' if image_url else 'not found'
            print(f'  [{i+1}/{len(needs_work)}] {name}: {status}')
            if image_url:
                enriched += 1
            continue

        if not image_url:
            print(f'  [{i+1}/{len(needs_work)}] {name}: no photo found')
            failed += 1
            continue

        img_data = download_image(image_url)
        if not img_data:
            print(f'  [{i+1}/{len(needs_work)}] {name}: download failed')
            failed += 1
            continue

        safe_name = name.replace('/', '_').replace(' ', '_')[:30]
        upload_id = notion_upload_binary(token, img_data, f'{safe_name}.jpg')
        if not upload_id:
            print(f'  [{i+1}/{len(needs_work)}] {name}: upload failed')
            failed += 1
            continue

        ok = notion_set_cover(token, page_id, upload_id, image_url, cover_prop, status_prop, checked_prop, source_prop)
        if ok:
            print(f'  [{i+1}/{len(needs_work)}] {name}: uploaded')
            enriched += 1
        else:
            print(f'  [{i+1}/{len(needs_work)}] {name}: cover set failed')
            failed += 1

        time.sleep(1)

    print(f'\n{db_name} done: {enriched} enriched, {failed} failed')


def enrich_book_covers(dry_run=False):
    """Enrich Book covers by scraping Douban book pages."""
    config = DB_CONFIG['Book']
    db_id = config['id']
    token = config['token']

    print('\n=== Enriching Book covers ===')
    pages = notion_query(db_id, token)
    needs_work = [p for p in pages if p.get('cover') is None]

    print(f'Total: {len(pages)}, Needs cover: {len(needs_work)}')

    enriched = 0
    failed = 0
    for i, page in enumerate(needs_work):
        name = get_name(page)
        subject_id = get_douban_subject_id(page)

        if not subject_id:
            print(f'  [{i+1}/{len(needs_work)}] {name}: no subject ID')
            failed += 1
            continue

        cover_url = scrape_douban_book_cover(subject_id)

        if dry_run:
            status = 'found' if cover_url else 'not found'
            print(f'  [{i+1}/{len(needs_work)}] {name}: {status}')
            if cover_url:
                enriched += 1
            time.sleep(2)
            continue

        if not cover_url:
            print(f'  [{i+1}/{len(needs_work)}] {name}: no cover found')
            failed += 1
            time.sleep(2)
            continue

        img_data = download_image(cover_url)
        if not img_data:
            print(f'  [{i+1}/{len(needs_work)}] {name}: download failed')
            failed += 1
            time.sleep(2)
            continue

        upload_id = notion_upload_binary(token, img_data, f'{name[:20]}.jpg')
        if not upload_id:
            print(f'  [{i+1}/{len(needs_work)}] {name}: upload failed')
            failed += 1
            time.sleep(2)
            continue

        ok = notion_set_cover(token, page['id'], upload_id, cover_url,
                              config['cover_prop'], config['status_prop'],
                              config['checked_prop'], config['source_prop'])
        if ok:
            print(f'  [{i+1}/{len(needs_work)}] {name}: uploaded')
            enriched += 1
        else:
            print(f'  [{i+1}/{len(needs_work)}] {name}: cover set failed')
            failed += 1

        time.sleep(2)

    print(f'\nBook done: {enriched} enriched, {failed} failed')


# ─── Douban author scraping ─────────────────────────────────────

def _author_name_matches(author_name, douban_name):
    """Check if a Notion author name matches a Douban author name."""
    # Normalize: strip prefixes like [美], [英], etc.
    clean_author = re.sub(r'^\[.*?\]\s*', '', author_name).strip()
    clean_douban = re.sub(r'^\[.*?\]\s*', '', douban_name).strip()
    # Strip trailing annotations (编), (选), etc.
    clean_author = re.sub(r'[（(][^）)]*[）)]$', '', clean_author).strip()
    clean_douban = re.sub(r'[（(][^）)]*[）)]$', '', clean_douban).strip()
    # Case-insensitive comparison
    return clean_author.lower() == clean_douban.lower()


def scrape_douban_author_photo_from_book(subject_id, author_name):
    """Scrape a Douban book page for an author link, then fetch the author photo.

    Returns photo URL or None.
    """
    url = f'https://book.douban.com/subject/{subject_id}/'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            time.sleep(6)
            content = page.content()
        except Exception:
            browser.close()
            return None
        browser.close()

    if len(content) < 5000:
        return None

    # Find author links: /author/{id} with matching name
    # Pattern: <a href="/author/12345">Author Name</a>
    author_links = re.findall(
        r'href="(?:https?://book\.douban\.com)?/author/(\d+)"[^>]*>([^<]+)</a>',
        content
    )
    for author_id, douban_name in author_links:
        if _author_name_matches(author_name, douban_name.strip()):
            return _fetch_douban_author_page_photo(author_id)

    # Also try: find all /author/ IDs and check the info section for name matching
    info_match = re.search(r'<div id="info"[^>]*>(.*?)</div>', content, re.DOTALL)
    if info_match:
        info = info_match.group(1)
        # Find author names in info
        author_entries = re.findall(
            r'href="(?:https?://book\.douban\.com)?/author/(\d+)"[^>]*>([^<]+)',
            info
        )
        for author_id, douban_name in author_entries:
            if _author_name_matches(author_name, douban_name.strip()):
                return _fetch_douban_author_page_photo(author_id)

    return None


def _fetch_douban_author_page_photo(author_id):
    """Fetch photo from a Douban author page."""
    url = f'https://book.douban.com/author/{author_id}/'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            time.sleep(5)
            content = page.content()
        except Exception:
            browser.close()
            return None
        browser.close()

    if len(content) < 5000:
        return None

    # Author photo is in <img> with author-related class or src containing img/author/
    author_imgs = re.findall(
        r'<img[^>]*src="(https://img\d+\.doubanio\.com/img/author/[^"]+)"',
        content
    )
    if author_imgs:
        return author_imgs[0]
    return None


# ─── Open Library helpers ────────────────────────────────────────

def openlibrary_search_author(name):
    """Search Open Library for an author by name, return OL key or None."""
    import urllib.parse
    # Strip Chinese prefixes like [美], [英], etc.
    clean = re.sub(r'^\[.*?\]\s*', '', name)
    # Strip trailing annotations like (编), (选), etc.
    clean = re.sub(r'[（(][^）)]*[）)]$', '', clean).strip()

    for search_name in [clean, name]:
        if not search_name:
            continue
        url = f'https://openlibrary.org/search/authors.json?q={urllib.parse.quote(search_name)}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            docs = data.get('docs', [])
            if docs:
                return docs[0].get('key')
        except Exception:
            time.sleep(2)
    return None


def openlibrary_author_photo(ol_key):
    """Fetch author detail from Open Library, return photo URL or None."""
    url = f'https://openlibrary.org/authors/{ol_key}.json'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        photos = data.get('photos', [])
        if photos:
            return f'https://covers.openlibrary.org/a/id/{photos[0]}-L.jpg'
    except Exception:
        pass
    return None


# ─── Enrichment logic ───────────────────────────────────────────

def enrich_author_photos(dry_run=False):
    """Enrich Author photos. Tries Douban author pages first, then Open Library."""
    config = DB_CONFIG['Author']
    db_id = config['id']
    token = config['token']
    cover_prop = config['cover_prop']
    status_prop = config['status_prop']
    checked_prop = config['checked_prop']
    source_prop = config['source_prop']
    book_prop = config.get('person_prop', '书籍')

    print('\n=== Enriching Author photos ===')
    pages = notion_query(db_id, token)
    needs_work = []
    for p in pages:
        name = get_name(p)
        status = (p.get('properties') or {}).get(status_prop, {}).get('select', {})
        if name and (p.get('cover') is None or (status and status.get('name') == 'Missing')):
            alt_name = get_field(p, 'Alt-Name')
            book_ids = get_relation_ids(p, book_prop)
            needs_work.append({'page': p, 'name': name, 'alt_name': alt_name, 'book_ids': book_ids})

    print(f'Total: {len(pages)}, Needs photo: {len(needs_work)}')

    enriched = 0
    failed = 0
    for i, item in enumerate(needs_work):
        name = item['name']
        alt_name = item['alt_name']
        page_id = item['page']['id']
        book_ids = item['book_ids']
        photo_url = None
        source = None

        # 1) Try Douban: scrape linked books for /author/ link
        for book_id in book_ids[:3]:
            try:
                book_page = notion_get_page(book_id, token)
            except Exception:
                continue
            subject_id = get_douban_subject_id(book_page)
            if not subject_id:
                continue

            photo_url = scrape_douban_author_photo_from_book(subject_id, name)
            if photo_url:
                source = 'Douban'
                break
            time.sleep(2)

        # 2) Fallback: Open Library (try alt_name first, then name)
        if not photo_url:
            for search_name in ([alt_name, name] if alt_name else [name]):
                if not search_name:
                    continue
                ol_key = openlibrary_search_author(search_name)
                if ol_key:
                    photo_url = openlibrary_author_photo(ol_key)
                    if photo_url:
                        source = 'OpenLibrary'
                        break
                time.sleep(1)

        if dry_run:
            if photo_url:
                print(f'  [{i+1}/{len(needs_work)}] {name}: found via {source}')
                enriched += 1
            else:
                print(f'  [{i+1}/{len(needs_work)}] {name}: no photo found')
            time.sleep(1)
            continue

        if not photo_url:
            print(f'  [{i+1}/{len(needs_work)}] {name}: no photo found')
            failed += 1
            time.sleep(1)
            continue

        img_data = download_image(photo_url)
        if not img_data:
            print(f'  [{i+1}/{len(needs_work)}] {name}: download failed')
            failed += 1
            time.sleep(1)
            continue

        safe_name = name.replace('/', '_').replace(' ', '_')[:30]
        upload_id = notion_upload_binary(token, img_data, f'{safe_name}.jpg')
        if not upload_id:
            print(f'  [{i+1}/{len(needs_work)}] {name}: upload failed')
            failed += 1
            time.sleep(1)
            continue

        ok = notion_set_cover(token, page_id, upload_id, photo_url,
                              cover_prop, status_prop, checked_prop, source_prop)
        if ok:
            print(f'  [{i+1}/{len(needs_work)}] {name}: uploaded via {source}')
            enriched += 1
        else:
            print(f'  [{i+1}/{len(needs_work)}] {name}: cover set failed')
            failed += 1

        time.sleep(1)

    print(f'\nAuthor done: {enriched} enriched, {failed} failed')


def main():

    dry_run = '--dry-run' in sys.argv
    scope = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] not in ('--dry-run',) else 'all'

    if scope in ('all', 'actor'):
        enrich_person_db('Actor', DB_CONFIG['Actor'], dry_run)
    if scope in ('all', 'director'):
        enrich_person_db('Director', DB_CONFIG['Director'], dry_run)
    if scope in ('all', 'book'):
        enrich_book_covers(dry_run)
    if scope in ('all', 'author'):
        enrich_author_photos(dry_run)


if __name__ == '__main__':
    main()
