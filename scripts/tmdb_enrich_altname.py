#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch-enrich Alt-Name for Actor, Director, and Author records using TMDB API.

Logic:
- Query all records from Notion database
- For each record missing Alt-Name, search TMDB by name
- Use also_known_as to find the other-language name
- For Chinese names: find English name from also_known_as
- For English names: find Chinese name from also_known_as
- Rate limit: 0.35s between Notion writes, 0.25s between TMDB requests
"""
import os
import sys
import time
import json
import urllib.parse
import urllib.request

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Load .env
ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '.env')
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

MOVIE_TOKEN = os.environ['MOVIE_NOTION_TOKEN']
BOOK_TOKEN = os.environ['BOOK_NOTION_TOKEN']
TMDB_KEY = os.environ['TMDB_API_KEY']
RATE_LIMIT = 0.35
TMDB_LIMIT = 0.25

# Database IDs and their tokens
DB_CONFIG = {
    'Actor':    {'id': '1d7119b0-97c7-8150-b869-ec25d47734fe', 'token': MOVIE_TOKEN},
    'Director': {'id': '1d7119b0-97c7-8167-a800-f8840ca98d02', 'token': MOVIE_TOKEN},
    'Author':   {'id': '1d8119b0-97c7-812c-a5ce-f242ddf11555', 'token': BOOK_TOKEN},
}

def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }

def query_all_pages(database_id, token):
    """Query all pages from a Notion database, handling pagination."""
    pages = []
    start_cursor = None
    while True:
        body = {'page_size': 100}
        if start_cursor:
            body['start_cursor'] = start_cursor
        req = urllib.request.Request(
            f'https://api.notion.com/v1/databases/{database_id}/query',
            data=json.dumps(body).encode(),
            headers=notion_headers(token),
            method='POST',
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        pages.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        start_cursor = data.get('next_cursor')
    return pages

def get_title(page):
    """Get title from Name property."""
    for prop in page.get('properties', {}).values():
        if prop.get('type') == 'title':
            texts = prop.get('title', [])
            return ''.join(t.get('plain_text', '') for t in texts)
    return ''

def get_rich_text(page, prop_name):
    """Get plain text from a rich_text property."""
    prop = page.get('properties', {}).get(prop_name, {})
    if prop.get('type') == 'rich_text':
        return ''.join(t.get('plain_text', '') for t in prop.get('rich_text', []))
    return ''

def set_rich_text(page_id, prop_name, value, token):
    """Set a rich_text property on a Notion page."""
    body = {
        'properties': {
            prop_name: {
                'rich_text': [{'text': {'content': value}}]
            }
        }
    }
    req = urllib.request.Request(
        f'https://api.notion.com/v1/pages/{page_id}',
        data=json.dumps(body).encode(),
        headers=notion_headers(token),
        method='PATCH',
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def has_cjk(text):
    """Check if text contains CJK characters."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0x20000 <= cp <= 0x2A6DF or 0xF900 <= cp <= 0xFAFF):
            return True
    return False

def tmdb_search_and_get_details(name):
    """Search TMDB for a person and return full details with also_known_as."""
    params = urllib.parse.urlencode({"api_key": TMDB_KEY, "query": name, "language": "en"})
    url = f"https://api.themoviedb.org/3/search/person?{params}"
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [TMDB] Search error: {e}", flush=True)
        return None

    results = data.get('results', [])
    if not results:
        return None

    tmdb_id = results[0].get('id')
    # Get full details with also_known_as
    detail_url = f"https://api.themoviedb.org/3/person/{tmdb_id}?api_key={TMDB_KEY}"
    try:
        req = urllib.request.Request(detail_url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [TMDB] Detail error: {e}", flush=True)
        return None

def find_alt_name(original_name, tmdb_detail):
    """
    Find the other-language name from TMDB person details.

    Strategy:
    - If original is Chinese → find English name from also_known_as (or primary name)
    - If original is English → find Chinese name from also_known_as (or primary name)
    Returns alt_name string or None.
    """
    if not tmdb_detail:
        return None

    primary_name = tmdb_detail.get('name', '')
    aka_list = tmdb_detail.get('also_known_as', [])
    is_original_chinese = has_cjk(original_name)

    if is_original_chinese:
        # Want English alt-name
        candidates = []
        # Check primary name first
        if primary_name and not has_cjk(primary_name) and primary_name != original_name:
            candidates.append(primary_name)
        # Check also_known_as for English names
        for aka in aka_list:
            if not has_cjk(aka) and aka != original_name and aka not in candidates:
                candidates.append(aka)
        return candidates[0] if candidates else None
    else:
        # Want Chinese alt-name
        candidates = []
        # Check primary name first
        if primary_name and has_cjk(primary_name) and primary_name != original_name:
            candidates.append(primary_name)
        # Check also_known_as for Chinese names
        for aka in aka_list:
            if has_cjk(aka) and aka != original_name and aka not in candidates:
                candidates.append(aka)
        return candidates[0] if candidates else None

def process_database(db_name, db_id, token):
    """Process all records in a database that are missing Alt-Name."""
    print(f"\n{'='*60}", flush=True)
    print(f"Processing {db_name} database ({db_id})", flush=True)
    print(f"{'='*60}", flush=True)

    pages = query_all_pages(db_id, token)
    print(f"Total records: {len(pages)}", flush=True)

    # Filter to those missing Alt-Name
    missing = []
    for p in pages:
        name = get_title(p)
        alt = get_rich_text(p, 'Alt-Name')
        if name and not alt:
            missing.append(p)

    print(f"Missing Alt-Name: {len(missing)}", flush=True)

    enriched = 0
    skipped = 0
    errors = 0

    for i, page in enumerate(missing):
        name = get_title(page)
        page_id = page['id']
        print(f"[{i+1}/{len(missing)}] {name} ({page_id[:8]}...)", flush=True)

        detail = tmdb_search_and_get_details(name)
        time.sleep(TMDB_LIMIT)

        if not detail:
            print(f"  -> Not found on TMDB", flush=True)
            skipped += 1
            continue

        alt_name = find_alt_name(name, detail)
        if not alt_name:
            aka = detail.get('also_known_as', [])
            print(f"  -> No alt name (primary={detail.get('name')}, aka={aka[:3]})", flush=True)
            skipped += 1
            continue

        try:
            set_rich_text(page_id, 'Alt-Name', alt_name, token)
            print(f"  -> Set Alt-Name: {alt_name}", flush=True)
            enriched += 1
        except Exception as e:
            print(f"  -> ERROR writing to Notion: {e}", flush=True)
            errors += 1

        time.sleep(RATE_LIMIT)

    print(f"\n{db_name} complete: {enriched} enriched, {skipped} skipped, {errors} errors", flush=True)
    return enriched, skipped, errors

def main():
    print("Alt-Name Enrichment Script (TMDB)", flush=True)
    print(f"TMDB Key: {TMDB_KEY[:4]}...", flush=True)

    total_enriched = 0
    total_skipped = 0
    total_errors = 0

    # Process all three databases
    for db_name in ['Actor', 'Director', 'Author']:
        cfg = DB_CONFIG[db_name]
        enriched, skipped, errors = process_database(db_name, cfg['id'], cfg['token'])
        total_enriched += enriched
        total_skipped += skipped
        total_errors += errors

    print(f"\n{'='*60}", flush=True)
    print(f"GRAND TOTAL: {total_enriched} enriched, {total_skipped} skipped, {total_errors} errors", flush=True)
    print(f"{'='*60}", flush=True)

if __name__ == '__main__':
    main()
