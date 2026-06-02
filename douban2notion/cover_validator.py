import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import threading

import pendulum
import requests
from dotenv import load_dotenv
from tqdm import tqdm

from douban2notion.douban import (
    get_goodreads_cover,
    get_imdb_info,
    get_imdb_person_info,
    get_tmdb_person_photo_by_imdb_id,
    get_tmdb_person_photo_by_name,
)
from douban2notion.notion_helper import NotionHelper
from douban2notion.utils import get_icon, get_property_value
from douban2notion.cache_manager import cache_manager
from douban2notion.config import MAX_WORKERS, MAX_URL_WORKERS
from douban2notion.retry_utils import retry_on_exception, safe_request
from douban2notion.performance_monitor import timing, Timer

DOUBAN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://book.douban.com/",
}


load_dotenv()

# 使用统一的缓存管理器
AUTHOR_NAME_CACHE = cache_manager.get_cache("author_name")
OPENLIB_AUTHOR_PHOTO_CACHE = cache_manager.get_cache("openlib_author_photo")
DEFAULT_USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"


def now_date_payload():
    return {"date": {"start": pendulum.now("Asia/Shanghai").to_datetime_string(), "time_zone": "Asia/Shanghai"}}


@retry_on_exception(max_retries=2, delay=0.5, backoff=2.0)
def _check_image_url(url: str) -> bool:
    """检查图片URL是否有效（带重试）"""
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
        allow_redirects=True,
        stream=True,
    )
    content_type = (response.headers.get("Content-Type") or "").lower()
    return response.status_code == 200 and "image/" in content_type


def is_valid_image_url(url: Optional[str]) -> bool:
    """验证图片URL是否有效（线程安全）"""
    if not url:
        return False

    # doubanio.com 对云端IP返回418反爬，但URL本身有效（豆瓣API直接返回的信任URL）
    # 不做HTTP验证，直接信任doubanio.com封面URL
    if "doubanio.com" in url:
        return True

    # 先检查缓存
    cached_result = cache_manager.get("data_audit_url_validation", url)
    if cached_result is not None:
        return cached_result

    try:
        ok = _check_image_url(url)
    except Exception:
        ok = False

    # 线程安全地写入缓存
    cache_manager.set("data_audit_url_validation", url, ok)
    return ok


def batch_validate_urls(urls: List[str], max_workers: int = MAX_URL_WORKERS) -> Dict[str, bool]:
    """批量验证URL有效性（并行处理）"""
    if not urls:
        return {}

    # 过滤掉已经在缓存中的URL
    urls_to_check = [url for url in urls if url and not cache_manager.has("data_audit_url_validation", url)]
    results = {}

    # 如果所有URL都在缓存中，直接返回
    if not urls_to_check:
        return {url: cache_manager.get("data_audit_url_validation", url, False) for url in urls}

    # 并行验证URL
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(is_valid_image_url, url): url for url in urls_to_check}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                results[url] = future.result()
            except Exception:
                results[url] = False

    # 合并缓存中的结果
    for url in urls:
        if url not in results:
            results[url] = cache_manager.get("data_audit_url_validation", url, False)

    return results


def get_files_url(page, property_name: str) -> Optional[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    files = (prop.get("files") or [])
    if not files:
        return None
    return _file_object_url(files[0])


def get_icon_url(page) -> Optional[str]:
    icon = page.get("icon") or {}
    return _file_object_url(icon)


def get_cover_url(page) -> Optional[str]:
    cover = page.get("cover") or {}
    return _file_object_url(cover)


def _file_object_url(file_obj: Optional[Dict]) -> Optional[str]:
    if not file_obj:
        return None
    file_type = file_obj.get("type")
    if file_type == "external":
        return (file_obj.get("external") or {}).get("url")
    if file_type == "file":
        return (file_obj.get("file") or {}).get("url")
    if file_type == "file_upload":
        return (file_obj.get("file_upload") or {}).get("url")
    return None


def get_title_value(page, property_name: str = "Name") -> Optional[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    return get_property_value(prop)


def get_page_title(page) -> Optional[str]:
    """遍历属性找到type=='title'的属性值（兼容Name/标题/其他命名）"""
    for prop in (page.get("properties") or {}).values():
        if (prop or {}).get("type") == "title":
            return get_property_value(prop)
    return None


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


def _file_upload_media(upload_id: str, name: Optional[str] = None) -> Dict:
    payload = {"type": "file_upload", "file_upload": {"id": upload_id}}
    if name:
        payload["name"] = name
    return payload


def _existing_media_slots(page, property_name: Optional[str]) -> Dict[str, Optional[str]]:
    return {
        "property": get_files_url(page, property_name) if property_name else None,
        "icon": get_icon_url(page),
        "cover": get_cover_url(page),
    }


def _validate_media_slots(slots: Dict[str, Optional[str]], invalid_urls=None) -> Dict[str, bool]:
    invalid_urls = set(invalid_urls or [])
    urls = [url for url in slots.values() if url]
    if not urls:
        return {slot: False for slot in slots}
    validation_results = batch_validate_urls(urls, max_workers=3)
    return {
        slot: bool(url and url not in invalid_urls and validation_results.get(url, False))
        for slot, url in slots.items()
    }


def _media_upload_filename(page, default_name: str = "media") -> str:
    title = get_page_title(page) or default_name
    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', title)[:30] or default_name
    return f"{safe_name}.jpg"


def _copy_valid_media_to_invalid_slots(
    nh: NotionHelper,
    page: Dict,
    property_name: Optional[str],
    status_field: str,
    checked_field: Optional[str],
    source_field: Optional[str],
    issue_tags,
    invalid_urls=None,
) -> Optional[Tuple[bool, bool]]:
    slots = _existing_media_slots(page, property_name)
    valid_slots = _validate_media_slots(slots, invalid_urls=invalid_urls)
    required_slots = ["icon", "cover"]
    if property_name:
        required_slots.insert(0, "property")

    if all(valid_slots.get(slot, False) for slot in required_slots):
        update_check_fields(nh.client, page, status_field, checked_field, source_field, "Ok")
        remove_data_issue_tags(nh.client, page, issue_tags)
        return True, False

    source_slot = None
    source_url = None
    for slot in required_slots:
        if valid_slots.get(slot) and slots.get(slot):
            source_slot = slot
            source_url = slots.get(slot)
            break
    if not source_url:
        return None

    img_data = _download_image(source_url)
    if not img_data:
        print(f"  已有有效图片但下载失败，跳过外部替换: {source_slot}")
        return True, False
    upload_id = _notion_upload_binary(
        nh.client.options.auth,
        img_data,
        _media_upload_filename(page, default_name=property_name or "media"),
    )
    if not upload_id:
        print(f"  已有有效图片但上传失败，跳过外部替换: {source_slot}")
        return True, False

    update_payload = {"page_id": page.get("id")}
    if not valid_slots.get("icon"):
        update_payload["icon"] = _file_upload_media(upload_id)
    if not valid_slots.get("cover"):
        update_payload["cover"] = _file_upload_media(upload_id)
    if property_name and not valid_slots.get("property"):
        update_payload["properties"] = {
            property_name: {"files": [_file_upload_media(upload_id, property_name)]}
        }

    media_changed = any(key in update_payload for key in ("icon", "cover", "properties"))
    if media_changed:
        nh.client.pages.update(**update_payload)

    source = get_property_value(((page.get("properties") or {}).get(source_field) or {})) if source_field else None
    update_check_fields(nh.client, page, status_field, checked_field, source_field, "Ok", source)
    remove_data_issue_tags(nh.client, page, issue_tags)
    print(f"  复用已有有效图片: {source_slot} -> missing/broken slots")
    return True, media_changed


def update_check_fields(
    client,
    page,
    status_field: str,
    checked_field: Optional[str],
    source_field: Optional[str],
    status: str,
    source: Optional[str] = None,
    update_checked_at: bool = True,
):
    props = page.get("properties") or {}
    update_properties = {}
    if status_field in props:
        current_status = get_property_value(props.get(status_field) or {})
        if current_status != status:
            update_properties[status_field] = {"select": {"name": status}}
    if update_checked_at and checked_field and checked_field in props:
        update_properties[checked_field] = now_date_payload()
    if source_field and source_field in props and source:
        current_source = get_property_value(props.get(source_field) or {})
        if current_source != source:
            update_properties[source_field] = {"select": {"name": source}}
    if update_properties:
        client.pages.update(page_id=page.get("id"), properties=update_properties)


def remove_data_issue_tags(client, page, tags_to_remove):
    props = page.get("properties") or {}
    data_issue = props.get("DataIssue") or {}
    if data_issue.get("type") != "multi_select":
        return
    existing = [x.get("name") for x in (data_issue.get("multi_select") or []) if x.get("name")]
    if not existing:
        return
    remove_set = {str(x).strip() for x in (tags_to_remove or []) if str(x).strip()}
    if not remove_set:
        return
    final = [x for x in existing if x not in remove_set]
    if final == existing:
        return
    client.pages.update(
        page_id=page.get("id"),
        properties={"DataIssue": {"multi_select": [{"name": x} for x in final]}},
    )


def get_openlibrary_book_cover(isbn: Optional[str], title: Optional[str] = None, author: Optional[str] = None) -> Optional[str]:
    """从OpenLibrary获取书籍封面（ISBN优先，其次标题搜索）"""
    # 1. 尝试ISBN直接查询
    if isbn:
        candidates = [
            f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",
            f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false",
        ]
        for url in candidates:
            if is_valid_image_url(url):
                return url

    # 2. 通过标题搜索OpenLibrary
    if title:
        try:
            search_params = {"title": title, "limit": 3}
            if author:
                search_params["author"] = author
            response = requests.get(
                "https://openlibrary.org/search.json",
                params=search_params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if response.status_code == 200:
                for doc in (response.json().get("docs") or []):
                    cover_i = doc.get("cover_i")
                    if cover_i:
                        url = f"https://covers.openlibrary.org/b/id/{cover_i}-L.jpg?default=false"
                        if is_valid_image_url(url):
                            return url
        except Exception:
            pass

    if isbn:
        print(f"    📚 OpenLibrary: ✗ ISBN={isbn} 封面无效")
    else:
        print(f"    📚 OpenLibrary: ✗ 无ISBN，标题搜索未找到")
    return None


def get_google_books_cover(isbn: Optional[str] = None, title: Optional[str] = None, author: Optional[str] = None) -> Optional[str]:
    """从Google Books API获取封面（ISBN优先，其次标题+作者搜索）"""
    # 1. 尝试ISBN搜索
    if isbn:
        try:
            response = requests.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": f"isbn:{isbn}", "maxResults": 1},
                timeout=10,
            )
            if response.status_code == 200:
                items = response.json().get("items") or []
                if items:
                    url = _extract_google_books_cover(items[0])
                    if url:
                        return url
            elif response.status_code == 429:
                print(f"    📚 GoogleBooks: ✗ HTTP 429 限流")
                return None
        except Exception:
            pass

    # 2. 通过标题+作者搜索
    if title:
        try:
            query = f"intitle:{title}"
            if author:
                query += f"+inauthor:{author}"
            response = requests.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": query, "maxResults": 5},
                timeout=10,
            )
            if response.status_code == 200:
                for item in (response.json().get("items") or []):
                    url = _extract_google_books_cover(item)
                    if url:
                        return url
            elif response.status_code == 429:
                print(f"    📚 GoogleBooks: ✗ HTTP 429 限流")
                return None
        except Exception:
            pass

    print(f"    📚 GoogleBooks: ✗ 未找到可用封面")
    return None


def _extract_google_books_cover(item: dict) -> Optional[str]:
    """从Google Books API结果中提取封面URL"""
    image_links = (item.get("volumeInfo") or {}).get("imageLinks") or {}
    for key in ["extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"]:
        url = image_links.get(key)
        if url:
            url = url.replace("http://", "https://")
            if is_valid_image_url(url):
                return url
    return None


def get_wikidata_person_photo(name: str) -> Optional[str]:
    """从Wikidata获取人物照片（通过中文名搜索）"""
    if not name:
        return None

    cached = cache_manager.get("wikidata_person_photo", name)
    if cached is not None:
        return cached

    try:
        # 1. 搜索Wikidata实体
        search_url = "https://www.wikidata.org/w/api.php"
        search_resp = requests.get(
            search_url,
            params={
                "action": "wbsearchentities",
                "search": name,
                "language": "zh",
                "limit": 5,
                "format": "json",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if search_resp.status_code != 200:
            cache_manager.set("wikidata_person_photo", name, None)
            return None

        results = search_resp.json().get("search") or []
        entity_id = None
        for r in results:
            if r.get("label") == name or r.get("label", "").lower() == name.lower():
                entity_id = r.get("id")
                break
        if not entity_id and results:
            entity_id = results[0].get("id")

        if not entity_id:
            cache_manager.set("wikidata_person_photo", name, None)
            return None

        # 2. 获取实体详情，查找P18（图片）属性
        entity_resp = requests.get(
            f"https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": entity_id,
                "props": "claims",
                "format": "json",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if entity_resp.status_code != 200:
            cache_manager.set("wikidata_person_photo", name, None)
            return None

        claims = (entity_resp.json().get("entities") or {}).get(entity_id, {}).get("claims") or {}
        p18_claims = claims.get("P18") or []
        if not p18_claims:
            cache_manager.set("wikidata_person_photo", name, None)
            return None

        # 3. 从P18获取文件名，构造Commons缩略图URL
        for claim in p18_claims:
            mainsnak = claim.get("mainsnak") or {}
            datavalue = mainsnak.get("datavalue") or {}
            filename = datavalue.get("value")
            if not filename:
                continue
            # Wikimedia Commons缩略图API
            encoded_name = requests.utils.quote(filename)
            thumb_url = f"https://commons.wikimedia.org/w/thumb.php?f={encoded_name}&w=400"
            if is_valid_image_url(thumb_url):
                cache_manager.set("wikidata_person_photo", name, thumb_url)
                return thumb_url

        cache_manager.set("wikidata_person_photo", name, None)
        return None
    except Exception:
        cache_manager.set("wikidata_person_photo", name, None)
        return None


@retry_on_exception(max_retries=2, delay=0.5, backoff=2.0)
def _fetch_openlibrary_author_photo(author_name: str) -> Optional[str]:
    """从OpenLibrary获取作者照片（带重试）"""
    search_url = "https://openlibrary.org/search/authors.json"
    response = requests.get(
        search_url,
        params={"q": author_name},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    if response.status_code != 200:
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
                return url
    return None


def get_openlibrary_author_photo(author_name: Optional[str]) -> Optional[str]:
    """获取OpenLibrary作者照片（使用缓存管理器）"""
    if not author_name:
        return None

    cached_photo = cache_manager.get("openlib_author_photo", author_name)
    if cached_photo is not None:
        return cached_photo

    try:
        photo_url = _fetch_openlibrary_author_photo(author_name)
    except Exception:
        photo_url = None

    cache_manager.set("openlib_author_photo", author_name, photo_url)
    return photo_url


def get_author_name_by_id(notion_helper: NotionHelper, author_id: str) -> Tuple[Optional[str], Optional[str]]:
    """获取作者名称和Alt-Name（使用缓存管理器，自动查找title类型属性）

    Returns:
        (title_name, alt_name) 元组
    """
    cached = cache_manager.get("author_name", author_id)
    if cached is not None:
        return cached  # (title_name, alt_name) or None

    title_name = None
    alt_name = None
    try:
        page = notion_helper.client.pages.retrieve(page_id=author_id)
        # 遍历所有属性，找到type=="title"的那个（兼容Name/标题/其他命名）
        for prop in (page.get("properties") or {}).values():
            if (prop or {}).get("type") == "title":
                title_name = get_property_value(prop)
                break
        # 获取Alt-Name（中国人存英文名，外国人存中文名，辅助搜索用）
        alt_name = get_property_value((page.get("properties") or {}).get("Alt-Name") or {})
    except Exception:
        pass

    result = (title_name, alt_name) if title_name else None
    cache_manager.set("author_name", author_id, result)
    return result


def _extract_douban_subject_id(url: Optional[str]) -> Optional[str]:
    """从豆瓣URL中提取subject ID"""
    if not url:
        return None
    match = re.search(r"/subject/(\d+)", str(url))
    return match.group(1) if match else None


def _is_chinese_movie_by_title(title: Optional[str]) -> bool:
    """通过标题判断是否华语影视（含中文则视为华语）"""
    if not title:
        return False
    return bool(re.search(r'[\u4e00-\u9fff]', title))


def _scrape_douban_movie_poster_url(subject_id: str) -> Optional[str]:
    """从豆瓣电影页面爬取海报URL"""
    cached = cache_manager.get("douban_movie_poster_url", subject_id)
    if cached is not None:
        return cached

    url = f"https://movie.douban.com/subject/{subject_id}/"
    try:
        resp = requests.get(url, headers=DOUBAN_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            cache_manager.set("douban_movie_poster_url", subject_id, None)
            return None
        html = resp.text
    except Exception:
        cache_manager.set("douban_movie_poster_url", subject_id, None)
        return None

    # 模式1: id="mainpic" 的img标签
    match = re.search(r'<img[^>]*id="mainpic"[^>]*src="([^"]+)"', html)
    if match:
        poster_url = match.group(1)
        cache_manager.set("douban_movie_poster_url", subject_id, poster_url)
        return poster_url

    # 模式2: doubanio.com/view/subject 图片
    matches = re.findall(r'src="(https://img\d+\.doubanio\.com/view/subject/[^"]*)"', html)
    if matches:
        cache_manager.set("douban_movie_poster_url", subject_id, matches[0])
        return matches[0]

    cache_manager.set("douban_movie_poster_url", subject_id, None)
    return None


def _get_douban_movie_cover_via_upload(nh: NotionHelper, page: Dict) -> Tuple[Optional[str], Optional[str]]:
    """从豆瓣获取电影封面并上传到Notion（返回 upload_id, source_url）"""
    props = page.get("properties") or {}
    db_url_prop = props.get("DB_Url") or props.get("Url") or {}
    db_url = get_property_value(db_url_prop)

    subject_id = _extract_douban_subject_id(db_url)
    if not subject_id:
        return None, None

    poster_url = _scrape_douban_movie_poster_url(subject_id)
    if not poster_url:
        return None, None

    img_data = _download_image(poster_url)
    if not img_data:
        return None, None

    title = get_title_value(page, "Name") or "movie"
    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', title)[:30]
    token = nh.client.options.auth
    upload_id = _notion_upload_binary(token, img_data, f"{safe_name}.jpg")
    if not upload_id:
        return None, None

    return upload_id, poster_url


def _scrape_douban_book_cover_url(subject_id: str) -> Optional[str]:
    """从豆瓣书籍页面爬取封面URL（通过HTML解析，不依赖JS渲染）"""
    cached = cache_manager.get("douban_book_cover_url", subject_id)
    if cached is not None:
        return cached

    url = f"https://book.douban.com/subject/{subject_id}/"
    try:
        resp = requests.get(url, headers=DOUBAN_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            cache_manager.set("douban_book_cover_url", subject_id, None)
            return None
        html = resp.text
    except Exception:
        cache_manager.set("douban_book_cover_url", subject_id, None)
        return None

    # 尝试多种HTML模式匹配封面
    # 模式1: id="mainpic" 的img标签
    match = re.search(r'<img[^>]*id="mainpic"[^>]*src="([^"]+)"', html)
    if match:
        cover_url = match.group(1)
        cache_manager.set("douban_book_cover_url", subject_id, cover_url)
        return cover_url

    # 模式2: doubanio.com/view/subject 图片
    matches = re.findall(r'src="(https://img\d+\.doubanio\.com/view/subject/[^"]*)"', html)
    if matches:
        cache_manager.set("douban_book_cover_url", subject_id, matches[0])
        return matches[0]

    cache_manager.set("douban_book_cover_url", subject_id, None)
    return None


def _download_image(url: str) -> Optional[bytes]:
    """下载图片为二进制数据"""
    headers = DOUBAN_HEADERS if "douban" in url else {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", ""):
            return resp.content
    except Exception:
        pass
    return None


def _notion_upload_binary(token: str, img_data: bytes, filename: str = "cover.jpg") -> Optional[str]:
    """上传二进制图片到Notion，返回upload_id"""
    try:
        resp = requests.post(
            "https://api.notion.com/v1/file_uploads",
            json={"mode": "single_part", "filename": filename, "content_type": "image/jpeg"},
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=30,
        )
        upload_id = resp.json().get("id")
        if not upload_id:
            return None
    except Exception:
        return None

    try:
        resp = requests.post(
            f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
            files={"file": (filename, img_data, "image/jpeg")},
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    # 等待上传完成
    for _ in range(10):
        time.sleep(1)
        try:
            resp = requests.get(
                f"https://api.notion.com/v1/file_uploads/{upload_id}",
                headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            )
            status = resp.json().get("status")
            if status == "uploaded":
                return upload_id
            elif status == "failed":
                return None
        except Exception:
            return None
    return None


def _notion_set_cover_upload(
    token: str, page_id: str, upload_id: str, source_url: str,
    cover_prop: str, status_prop: str, checked_prop: str, source_prop: str,
):
    """通过file_upload设置页面封面"""
    now_str = pendulum.now("Asia/Shanghai").to_datetime_string()
    body = {
        "cover": {"type": "file_upload", "file_upload": {"id": upload_id}},
        "icon": {"type": "file_upload", "file_upload": {"id": upload_id}},
        "properties": {
            cover_prop: {"files": [_file_upload_media(upload_id, cover_prop)]},
            status_prop: {"select": {"name": "Ok"}},
            checked_prop: {"date": {"start": now_str, "time_zone": "Asia/Shanghai"}},
            source_prop: {"select": {"name": "Douban"}},
        },
    }
    try:
        resp = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _get_douban_book_cover_via_upload(nh: NotionHelper, page: Dict) -> Tuple[Optional[str], Optional[str]]:
    """从豆瓣获取书籍封面并上传到Notion（返回 upload_id, source_url）"""
    props = page.get("properties") or {}
    db_url_prop = props.get("DB_Url") or props.get("Url") or {}
    db_url = get_property_value(db_url_prop)
    title = get_title_value(page, "Name") or "book"

    subject_id = _extract_douban_subject_id(db_url)
    if not subject_id:
        print(f"    📚 Douban: ✗ 无DB_Url ({db_url})")
        return None, None

    cover_url = _scrape_douban_book_cover_url(subject_id)
    if not cover_url:
        print(f"    📚 Douban: ✗ subject={subject_id} 未找到封面")
        return None, None

    img_data = _download_image(cover_url)
    if not img_data:
        print(f"    📚 Douban: ✗ 下载失败 {cover_url[:80]}")
        return None, None

    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', title)[:30]
    token = nh.client.options.auth
    upload_id = _notion_upload_binary(token, img_data, f"{safe_name}.jpg")
    if not upload_id:
        print(f"    📚 Douban: ✗ 上传失败")
        return None, None

    print(f"    📚 Douban: ✓ {cover_url[:80]}")
    return upload_id, cover_url


def _validate_single_movie_cover(nh: NotionHelper, page: Dict) -> Tuple[bool, bool]:
    """验证单个电影封面（用于并行处理）"""
    page_id = page.get("id")

    existing_media_result = _copy_valid_media_to_invalid_slots(
        nh,
        page,
        "Cover",
        "CoverStatus",
        "CoverCheckedAt",
        "CoverSource",
        {"BrokenCover", "MissingCover"},
    )
    if existing_media_result is not None:
        return existing_media_result

    title = get_title_value(page, "Name")
    imdb_id = get_rich_text_value(page, "IMDB")
    is_chinese = _is_chinese_movie_by_title(title)
    print(f"  🎬 电影 [{title}] IMDB={imdb_id} {'华语' if is_chinese else '外文'} 封面无效，尝试查找替代封面...")

    # 华语影视：Douban优先（下载+上传）
    if is_chinese:
        upload_id, douban_url = _get_douban_movie_cover_via_upload(nh, page)
        if upload_id:
            token = nh.client.options.auth
            ok = _notion_set_cover_upload(
                token, page_id, upload_id, douban_url,
                "Cover", "CoverStatus", "CoverCheckedAt", "CoverSource",
            )
            if ok:
                print(f"  ✅ 电影 [{title}] 封面已更新: Douban (uploaded) {douban_url[:80]}")
                remove_data_issue_tags(nh.client, page, {"BrokenCover", "MissingCover"})
                return True, True
            else:
                print(f"  ⚠️ 电影 [{title}] 豆瓣封面上传失败，尝试IMDB...")

    # 外文影视或豆瓣失败：IMDB/TMDB
    if imdb_id:
        imdb_info = get_imdb_info(imdb_id)
        new_cover = (imdb_info or {}).get("poster")
        if new_cover and is_valid_image_url(new_cover):
            try:
                update_page_media(nh.client, page_id, "Cover", new_cover, write_property=True)
                print(f"  ✅ 电影 [{title}] 封面已更新: IMDB {new_cover[:80]}")
                update_check_fields(
                    nh.client, page, "CoverStatus", "CoverCheckedAt", "CoverSource", "Ok", "IMDB",
                )
                remove_data_issue_tags(nh.client, page, {"BrokenCover", "MissingCover"})
                return True, True
            except Exception as e:
                print(f"  ❌ 电影 [{title}] 更新失败: {str(e)[:50]}")

    # 外文影视无IMDB或IMDB失败：Douban兜底
    if not is_chinese:
        upload_id, douban_url = _get_douban_movie_cover_via_upload(nh, page)
        if upload_id:
            token = nh.client.options.auth
            ok = _notion_set_cover_upload(
                token, page_id, upload_id, douban_url,
                "Cover", "CoverStatus", "CoverCheckedAt", "CoverSource",
            )
            if ok:
                print(f"  ✅ 电影 [{title}] 封面已更新: Douban (uploaded) {douban_url[:80]}")
                remove_data_issue_tags(nh.client, page, {"BrokenCover", "MissingCover"})
                return True, True

    status = "Missing" if (not prop_cover and not icon_url and not cover_url) else "Broken"
    print(f"  ❌ 电影 [{title}] 未找到可用封面，状态: {status}")
    update_check_fields(
        nh.client, page, "CoverStatus", "CoverCheckedAt", "CoverSource", status
    )
    return True, False


def _query_needing_repair(nh: NotionHelper, database_id: str, status_field: str) -> List[Dict]:
    """查询状态为 Missing 或 Broken 的条目（由 data_audit 标记）"""
    filter_payload = {
        "or": [
            {"property": status_field, "select": {"equals": "Missing"}},
            {"property": status_field, "select": {"equals": "Broken"}},
        ]
    }
    results = []
    has_more = True
    start_cursor = None
    while has_more:
        response = nh.client.databases.query(
            database_id=database_id,
            filter=filter_payload,
            start_cursor=start_cursor,
            page_size=100,
        )
        start_cursor = response.get("next_cursor")
        has_more = response.get("has_more")
        results.extend(response.get("results"))
    return results


@timing
def validate_movie_covers(nh: NotionHelper, max_workers: int = MAX_WORKERS):
    """并行验证电影封面（仅查 Missing/Broken）"""
    pages = _query_needing_repair(nh, nh.movie_database_id, "CoverStatus")
    fixed = 0
    checked = 0

    print(f"[Movie] 开始验证 {len(pages)} 个电影封面 (并发数: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_page = {
            executor.submit(_validate_single_movie_cover, nh, page): page
            for page in pages
        }

        with tqdm(total=len(pages), desc="验证电影封面", unit="个") as pbar:
            for future in as_completed(future_to_page):
                try:
                    page_checked, page_fixed = future.result()
                    checked += page_checked
                    fixed += page_fixed
                except Exception as e:
                    print(f"  验证失败: {str(e)[:100]}")
                pbar.update(1)

    print(f"[Movie] checked={checked} fixed={fixed}")


def _validate_single_book_cover(nh: NotionHelper, page: Dict) -> Tuple[bool, bool]:
    """验证单个书籍封面（用于并行处理）"""
    page_id = page.get("id")

    existing_media_result = _copy_valid_media_to_invalid_slots(
        nh,
        page,
        "Cover",
        "CoverStatus",
        "CoverCheckedAt",
        "CoverSource",
        {"BrokenCover", "MissingCover"},
    )
    if existing_media_result is not None:
        return existing_media_result

    title = get_title_value(page, "Name")
    isbn = get_rich_text_value(page, "ISBN")
    author_rel = ((page.get("properties") or {}).get("Author") or {}).get("relation") or []
    author_name = None
    author_alt_name = None
    if author_rel:
        result = get_author_name_by_id(nh, author_rel[0].get("id"))
        if result:
            author_name, author_alt_name = result

    print(f"  🔍 书 [{title}] ISBN={isbn} 作者={author_name} (Alt: {author_alt_name}) 封面无效，尝试查找替代封面...")

    # 1. 优先尝试豆瓣（最高置信度，下载后上传）
    upload_id, douban_url = _get_douban_book_cover_via_upload(nh, page)
    if upload_id:
        token = nh.client.options.auth
        ok = _notion_set_cover_upload(
            token, page_id, upload_id, douban_url,
            "Cover", "CoverStatus", "CoverCheckedAt", "CoverSource",
        )
        if ok:
            print(f"  ✅ 书 [{title}] 封面已更新: Douban (uploaded) {douban_url[:80]}")
            remove_data_issue_tags(nh.client, page, {"BrokenCover", "MissingCover"})
            return True, True
        else:
            print(f"  ⚠️ 书 [{title}] 豆瓣封面上传失败，尝试其他源...")

    # 2. 回退到其他源（Goodreads/OpenLibrary/GoogleBooks）
    new_cover, source = _get_book_cover_parallel(title, author_name, isbn, author_alt_name)

    if not new_cover:
        status = "Missing" if (not prop_cover and not icon_url and not cover_url) else "Broken"
        print(f"  ❌ 书 [{title}] 未找到可用封面，状态: {status}")
        update_check_fields(
            nh.client, page, "CoverStatus", "CoverCheckedAt", "CoverSource", status
        )
        return True, False

    try:
        update_page_media(nh.client, page_id, "Cover", new_cover, write_property=True)
        print(f"  ✅ 书 [{title}] 封面已更新: {source} {new_cover[:80]}")
        update_check_fields(
            nh.client,
            page,
            "CoverStatus",
            "CoverCheckedAt",
            "CoverSource",
            "Ok",
            source,
        )
        remove_data_issue_tags(nh.client, page, {"BrokenCover", "MissingCover"})
        return True, True
    except Exception as e:
        print(f"  ❌ 书 [{title}] 更新失败: {str(e)[:50]}")
        return True, False


def _get_book_cover_parallel(title: str, author_name: Optional[str], isbn: Optional[str],
                              author_alt_name: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """并行尝试多个书籍封面源（优先使用author_name，Alt-Name作为备选）"""
    # 构建作者名列表：优先主名，Alt-Name作为备选
    author_names = [name for name in [author_name, author_alt_name] if name]

    def _search_with_author_names(search_func):
        """尝试用多个作者名搜索"""
        for name in author_names:
            result = search_func(author=name)
            if result:
                return result
        return None

    cover_sources = [
        ("Goodreads", lambda: _search_with_author_names(
            lambda author: get_goodreads_cover(title, author=author, isbn=isbn))),
        ("OpenLibrary", lambda: _search_with_author_names(
            lambda author: get_openlibrary_book_cover(isbn, title=title, author=author))),
        ("GoogleBooks", lambda: _search_with_author_names(
            lambda author: get_google_books_cover(isbn=isbn, title=title, author=author))),
    ]

    with ThreadPoolExecutor(max_workers=len(cover_sources)) as executor:
        future_to_source = {
            executor.submit(source_func): source_name
            for source_name, source_func in cover_sources
        }

        # 返回第一个成功的结果
        for future in as_completed(future_to_source):
            source_name = future_to_source[future]
            try:
                result = future.result()
                if result:
                    valid = is_valid_image_url(result)
                    print(f"    📚 {source_name}: {'✓' if valid else '✗'} {result[:80] if result else 'None'}")
                    if valid:
                        return result, source_name
                else:
                    print(f"    📚 {source_name}: ✗ 未找到")
            except Exception as e:
                print(f"    📚 {source_name}: ✗ 异常 {str(e)[:50]}")
                continue

    print(f"  ❌ 所有封面源都未找到可用封面")
    return None, None


@timing
def validate_book_covers(nh: NotionHelper, max_workers: int = MAX_WORKERS):
    """并行验证书籍封面（仅查 Missing/Broken）"""
    pages = _query_needing_repair(nh, nh.book_database_id, "CoverStatus")
    fixed = 0
    checked = 0

    print(f"[Book] 开始验证 {len(pages)} 个书籍封面 (并发数: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_page = {
            executor.submit(_validate_single_book_cover, nh, page): page
            for page in pages
        }

        with tqdm(total=len(pages), desc="验证书籍封面", unit="个") as pbar:
            for future in as_completed(future_to_page):
                try:
                    page_checked, page_fixed = future.result()
                    checked += page_checked
                    fixed += page_fixed
                except Exception as e:
                    print(f"  验证失败: {str(e)[:100]}")
                pbar.update(1)

    print(f"[Book] checked={checked} fixed={fixed}")


def _validate_single_person_photo(nh: NotionHelper, page: Dict, has_photo_property: bool, has_imdb_property: bool, imdb_enabled: bool) -> Tuple[bool, bool]:
    """验证单个人物照片（用于并行处理）"""
    page_id = page.get("id")
    name = get_page_title(page)

    existing_media_result = _copy_valid_media_to_invalid_slots(
        nh,
        page,
        "Photo" if has_photo_property else None,
        "PhotoStatus",
        "PhotoCheckedAt",
        "PhotoSource",
        {"BrokenPhoto", "MissingPhoto"},
        invalid_urls={DEFAULT_USER_ICON_URL},
    )
    if existing_media_result is not None:
        return existing_media_result

    print(f"  👤 [{name}] 照片无效，尝试查找替代照片...")

    # 获取 Alt-Name 用于辅助搜索
    alt_name = get_property_value((page.get("properties") or {}).get("Alt-Name") or {})
    search_names = [n for n in [name, alt_name] if n]
    if alt_name:
        print(f"    Alt-Name={alt_name}")

    new_photo = None
    source = None

    # 1. 尝试IMDB（演员/导演）
    if imdb_enabled and has_imdb_property:
        imdb_id = get_rich_text_value(page, "IMDB")
        print(f"    IMDB={imdb_id}")
        if imdb_id:
            person = get_imdb_person_info(imdb_id)
            photo = (person or {}).get("photo")
            if photo and is_valid_image_url(photo):
                new_photo = photo
                source = "IMDB"
                print(f"    IMDB照片: ✓ {photo[:80]}")
            else:
                print(f"    IMDB照片: ✗ 未找到")

    # 2. 尝试TMDB（通过IMDB ID）
    if not new_photo and imdb_enabled and has_imdb_property:
        imdb_id = get_rich_text_value(page, "IMDB")
        if imdb_id:
            try:
                photo = get_tmdb_person_photo_by_imdb_id(imdb_id)
                if photo and is_valid_image_url(photo):
                    new_photo = photo
                    source = "TMDB"
                    print(f"    TMDB照片(IMDB): ✓ {photo[:80]}")
            except Exception:
                pass

    # 3. 尝试TMDB（通过姓名，主名+别名）
    if not new_photo:
        for search_name in search_names:
            try:
                photo = get_tmdb_person_photo_by_name(search_name)
                if photo and is_valid_image_url(photo):
                    new_photo = photo
                    source = "TMDB"
                    print(f"    TMDB照片({search_name}): ✓ {photo[:80]}")
                    break
            except Exception:
                pass

    # 4. 尝试OpenLibrary（主名+别名）
    if not new_photo:
        for search_name in search_names:
            photo = get_openlibrary_author_photo(search_name)
            if photo and is_valid_image_url(photo):
                new_photo = photo
                source = "OpenLibrary"
                print(f"    OpenLibrary照片({search_name}): ✓ {photo[:80]}")
                break

    # 5. 尝试Wikidata（主名+别名）
    if not new_photo:
        for search_name in search_names:
            photo = get_wikidata_person_photo(search_name)
            if photo and is_valid_image_url(photo):
                new_photo = photo
                source = "Wikidata"
                print(f"    Wikidata照片({search_name}): ✓ {photo[:80]}")
                break

    if not new_photo or not is_valid_image_url(new_photo):
        status = "Missing" if (not prop_photo and not icon_url and not cover_url) else "Broken"
        print(f"  ❌ [{name}] 未找到可用照片，状态: {status}")
        update_check_fields(nh.client, page, "PhotoStatus", "PhotoCheckedAt", "PhotoSource", status)
        return True, False

    try:
        update_page_media(
            nh.client,
            page_id,
            property_name="Photo" if has_photo_property else None,
            image_url=new_photo,
            write_property=has_photo_property,
        )
        print(f"  ✅ [{name}] 照片已更新: {source} {new_photo[:80]}")
        update_check_fields(nh.client, page, "PhotoStatus", "PhotoCheckedAt", "PhotoSource", "Ok", source)
        remove_data_issue_tags(nh.client, page, {"BrokenPhoto", "MissingPhoto"})
        return True, True
    except Exception as e:
        print(f"  ❌ [{name}] 更新失败: {str(e)[:50]}")
        return True, False


@timing
def validate_people_photos(nh: NotionHelper, db_id: str, label: str, imdb_enabled: bool, max_workers: int = MAX_WORKERS):
    """并行验证人物照片（仅查 Missing/Broken）"""
    if not db_id:
        print(f"[{label}] skipped (db not found)")
        return

    schema = nh.get_database_schema(db_id)
    has_photo_property = "Photo" in (schema.get("properties") or {})
    has_imdb_property = "IMDB" in (schema.get("properties") or {})

    pages = _query_needing_repair(nh, db_id, "PhotoStatus")
    fixed = 0
    checked = 0

    print(f"[{label}] 开始验证 {len(pages)} 个人物照片 (并发数: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_page = {
            executor.submit(
                _validate_single_person_photo, nh, page, has_photo_property, has_imdb_property, imdb_enabled
            ): page
            for page in pages
        }

        with tqdm(total=len(pages), desc=f"验证{label}照片", unit="个") as pbar:
            for future in as_completed(future_to_page):
                try:
                    page_checked, page_fixed = future.result()
                    checked += page_checked
                    fixed += page_fixed
                except Exception as e:
                    print(f"  验证失败: {str(e)[:100]}")
                pbar.update(1)

    print(f"[{label}] checked={checked} fixed={fixed}")


def build_helper(kind: str) -> Optional[NotionHelper]:
    try:
        return NotionHelper(kind)
    except Exception as e:
        print(f"[{kind}] init failed: {str(e)[:120]}")
        return None


def run(scope: str, max_workers: int = MAX_WORKERS):
    movie_helper = None
    book_helper = None

    if scope in ("all", "movie", "actor", "director"):
        movie_helper = build_helper("movie")
    if scope in ("all", "book", "author"):
        book_helper = build_helper("book")

    if scope in ("all", "movie") and movie_helper and movie_helper.movie_database_id:
        validate_movie_covers(movie_helper, max_workers=max_workers)
    if scope in ("all", "book") and book_helper and book_helper.book_database_id:
        validate_book_covers(book_helper, max_workers=max_workers)
    if scope in ("all", "actor") and movie_helper:
        validate_people_photos(movie_helper, movie_helper.actor_database_id, "Actor", imdb_enabled=True, max_workers=max_workers)
    if scope in ("all", "director") and movie_helper:
        validate_people_photos(movie_helper, movie_helper.director_database_id, "Director", imdb_enabled=True, max_workers=max_workers)
    if scope in ("all", "author") and book_helper:
        validate_people_photos(book_helper, book_helper.author_database_id, "Author", imdb_enabled=False, max_workers=max_workers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scope",
        nargs="?",
        default="all",
        choices=["all", "movie", "book", "actor", "director", "author"],
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help=f"并发线程数 (默认: {MAX_WORKERS})",
    )
    args = parser.parse_args()
    run(args.scope, max_workers=args.workers)


if __name__ == "__main__":
    main()
