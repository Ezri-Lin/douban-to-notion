import argparse
import os
from typing import Optional

import requests
from dotenv import load_dotenv

from douban2notion.douban import get_goodreads_cover, get_imdb_info, get_imdb_person_info
from douban2notion.notion_helper import NotionHelper
from douban2notion.utils import get_icon, get_property_value


load_dotenv()

URL_VALIDATION_CACHE = {}
AUTHOR_NAME_CACHE = {}
OPENLIB_AUTHOR_PHOTO_CACHE = {}


def is_valid_image_url(url: Optional[str]) -> bool:
    if not url:
        return False
    if url in URL_VALIDATION_CACHE:
        return URL_VALIDATION_CACHE[url]
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
            allow_redirects=True,
            stream=True,
        )
        content_type = (response.headers.get("Content-Type") or "").lower()
        ok = response.status_code == 200 and "image/" in content_type
    except Exception:
        ok = False
    URL_VALIDATION_CACHE[url] = ok
    return ok


def get_files_url(page, property_name: str) -> Optional[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    files = (prop.get("files") or [])
    if not files:
        return None
    first = files[0]
    if first.get("type") == "external":
        return ((first.get("external") or {}).get("url"))
    return None


def get_icon_url(page) -> Optional[str]:
    icon = page.get("icon") or {}
    if icon.get("type") == "external":
        return (icon.get("external") or {}).get("url")
    return None


def get_cover_url(page) -> Optional[str]:
    cover = page.get("cover") or {}
    if cover.get("type") == "external":
        return (cover.get("external") or {}).get("url")
    return None


def get_title_value(page, property_name: str = "Name") -> Optional[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    return get_property_value(prop)


def get_rich_text_value(page, property_name: str) -> Optional[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    return get_property_value(prop)


def update_page_media(client, page_id: str, property_name: Optional[str], image_url: str, write_property: bool = True):
    payload = {
        "page_id": page_id,
        "icon": get_icon(image_url),
        "cover": get_icon(image_url),
    }
    if write_property and property_name:
        payload["properties"] = {
            property_name: {
                "files": [{"type": "external", "name": property_name, "external": {"url": image_url}}]
            }
        }
    client.pages.update(**payload)


def get_openlibrary_book_cover(isbn: Optional[str]) -> Optional[str]:
    if not isbn:
        return None
    candidates = [
        f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",
        f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false",
    ]
    for url in candidates:
        if is_valid_image_url(url):
            return url
    return None


def get_openlibrary_author_photo(author_name: Optional[str]) -> Optional[str]:
    if not author_name:
        return None
    if author_name in OPENLIB_AUTHOR_PHOTO_CACHE:
        return OPENLIB_AUTHOR_PHOTO_CACHE[author_name]

    try:
        search_url = "https://openlibrary.org/search/authors.json"
        response = requests.get(
            search_url,
            params={"q": author_name},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if response.status_code != 200:
            OPENLIB_AUTHOR_PHOTO_CACHE[author_name] = None
            return None

        docs = response.json().get("docs") or []
        for doc in docs:
            author_key = doc.get("key")
            if not author_key:
                continue
            author_id = author_key.strip("/").split("/")[-1]
            if not author_id:
                continue

            author_detail = requests.get(
                f"https://openlibrary.org/authors/{author_id}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if author_detail.status_code != 200:
                continue
            photos = (author_detail.json().get("photos") or [])
            for photo_id in photos:
                url = f"https://covers.openlibrary.org/a/id/{photo_id}-L.jpg?default=false"
                if is_valid_image_url(url):
                    OPENLIB_AUTHOR_PHOTO_CACHE[author_name] = url
                    return url
    except Exception:
        pass

    OPENLIB_AUTHOR_PHOTO_CACHE[author_name] = None
    return None


def get_author_name_by_id(notion_helper: NotionHelper, author_id: str) -> Optional[str]:
    if author_id in AUTHOR_NAME_CACHE:
        return AUTHOR_NAME_CACHE[author_id]
    try:
        page = notion_helper.client.pages.retrieve(page_id=author_id)
        name = get_title_value(page, "Name")
    except Exception:
        name = None
    AUTHOR_NAME_CACHE[author_id] = name
    return name


def validate_movie_covers(nh: NotionHelper):
    pages = nh.query_all(database_id=nh.movie_database_id)
    fixed = 0
    checked = 0
    for page in pages:
        checked += 1
        page_id = page.get("id")
        prop_cover = get_files_url(page, "Cover")
        icon_url = get_icon_url(page)
        cover_url = get_cover_url(page)
        valid = is_valid_image_url(prop_cover) and is_valid_image_url(icon_url) and is_valid_image_url(cover_url)
        if valid:
            continue

        imdb_id = get_rich_text_value(page, "IMDB")
        if not imdb_id:
            continue
        imdb_info = get_imdb_info(imdb_id)
        new_cover = (imdb_info or {}).get("poster")
        if not new_cover or not is_valid_image_url(new_cover):
            continue
        try:
            update_page_media(nh.client, page_id, "Cover", new_cover, write_property=True)
            fixed += 1
        except Exception:
            continue
    print(f"[Movie] checked={checked} fixed={fixed}")


def validate_book_covers(nh: NotionHelper):
    pages = nh.query_all(database_id=nh.book_database_id)
    fixed = 0
    checked = 0
    for page in pages:
        checked += 1
        page_id = page.get("id")
        prop_cover = get_files_url(page, "Cover")
        icon_url = get_icon_url(page)
        cover_url = get_cover_url(page)
        valid = is_valid_image_url(prop_cover) and is_valid_image_url(icon_url) and is_valid_image_url(cover_url)
        if valid:
            continue

        title = get_title_value(page, "Name")
        isbn = get_rich_text_value(page, "ISBN")
        author_rel = ((page.get("properties") or {}).get("Author") or {}).get("relation") or []
        author_name = None
        if author_rel:
            author_name = get_author_name_by_id(nh, author_rel[0].get("id"))

        new_cover = get_goodreads_cover(title, author=author_name, isbn=isbn)
        if not is_valid_image_url(new_cover):
            new_cover = get_openlibrary_book_cover(isbn)
        if not new_cover:
            continue

        try:
            update_page_media(nh.client, page_id, "Cover", new_cover, write_property=True)
            fixed += 1
        except Exception:
            continue
    print(f"[Book] checked={checked} fixed={fixed}")


def validate_people_photos(nh: NotionHelper, db_id: str, label: str, imdb_enabled: bool):
    if not db_id:
        print(f"[{label}] skipped (db not found)")
        return

    schema = nh.get_database_schema(db_id)
    has_photo_property = "Photo" in (schema.get("properties") or {})
    has_imdb_property = "IMDB" in (schema.get("properties") or {})

    pages = nh.query_all(database_id=db_id)
    fixed = 0
    checked = 0
    for page in pages:
        checked += 1
        page_id = page.get("id")
        name = get_title_value(page, "Name")

        prop_photo = get_files_url(page, "Photo") if has_photo_property else None
        icon_url = get_icon_url(page)
        cover_url = get_cover_url(page)
        valid_photo = is_valid_image_url(prop_photo) if has_photo_property else True
        valid_icon = is_valid_image_url(icon_url)
        valid_cover = is_valid_image_url(cover_url)
        if valid_photo and valid_icon and valid_cover:
            continue

        new_photo = None
        if imdb_enabled and has_imdb_property:
            imdb_id = get_rich_text_value(page, "IMDB")
            if imdb_id:
                person = get_imdb_person_info(imdb_id)
                new_photo = (person or {}).get("photo")
        else:
            new_photo = get_openlibrary_author_photo(name)

        if not new_photo or not is_valid_image_url(new_photo):
            continue

        try:
            update_page_media(
                nh.client,
                page_id,
                property_name="Photo" if has_photo_property else None,
                image_url=new_photo,
                write_property=has_photo_property,
            )
            fixed += 1
        except Exception:
            continue
    print(f"[{label}] checked={checked} fixed={fixed}")


def build_helper(kind: str) -> Optional[NotionHelper]:
    try:
        return NotionHelper(kind)
    except Exception as e:
        print(f"[{kind}] init failed: {str(e)[:120]}")
        return None


def run(scope: str):
    movie_helper = None
    book_helper = None

    if scope in ("all", "movie", "actor", "director"):
        movie_helper = build_helper("movie")
    if scope in ("all", "book", "author"):
        book_helper = build_helper("book")

    if scope in ("all", "movie") and movie_helper and movie_helper.movie_database_id:
        validate_movie_covers(movie_helper)
    if scope in ("all", "book") and book_helper and book_helper.book_database_id:
        validate_book_covers(book_helper)
    if scope in ("all", "actor") and movie_helper:
        validate_people_photos(movie_helper, movie_helper.actor_database_id, "Actor", imdb_enabled=True)
    if scope in ("all", "director") and movie_helper:
        validate_people_photos(movie_helper, movie_helper.director_database_id, "Director", imdb_enabled=True)
    if scope in ("all", "author") and book_helper:
        validate_people_photos(book_helper, book_helper.author_database_id, "Author", imdb_enabled=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scope",
        nargs="?",
        default="all",
        choices=["all", "movie", "book", "actor", "director", "author"],
    )
    args = parser.parse_args()
    run(args.scope)


if __name__ == "__main__":
    main()
