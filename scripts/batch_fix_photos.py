#!/usr/bin/env python3
"""批量修复 Director/Actor/Author 数据库中缺少头像的记录"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from douban2notion.notion_helper import NotionHelper
from douban2notion.utils import get_icon, get_property_value
from douban2notion.cache_manager import cache_manager
from douban2notion.config import MAX_WORKERS
from douban2notion.cover_validator import (
    get_wikidata_person_photo,
    get_openlibrary_author_photo,
    get_imdb_person_info,
    is_valid_image_url,
    update_page_media,
    update_check_fields,
    remove_data_issue_tags,
    _download_image,
    _notion_upload_binary,
    _notion_set_cover_upload,
    _query_needing_repair,
    get_page_title,
    get_files_url,
    get_icon_url,
    get_cover_url,
    get_rich_text_value,
)

load_dotenv()

# 数据库 ID 映射（使用 database_id，不是 data_source_id）
DATABASE_IDS = {
    "director": "1d7119b0-97c7-8167-a800-f8840ca98d02",
    "actor": "1d7119b0-97c7-8150-b869-ec25d47734fe",
    "author": "1d8119b0-97c7-812c-a5ce-f242ddf11555",
}

# 每个数据库的属性名
PROPERTY_NAMES = {
    "director": {"photo": "Photo", "imdb": "IMDB", "status": "PhotoStatus", "source": "PhotoSource", "checked": "PhotoCheckedAt"},
    "actor": {"photo": "Photo", "imdb": "IMDB", "status": "PhotoStatus", "source": "PhotoSource", "checked": "PhotoCheckedAt"},
    "author": {"photo": "Photo", "imdb": None, "status": "PhotoStatus", "source": "PhotoSource", "checked": "PhotoCheckedAt"},
}


def get_tmdb_person_photo_by_imdb_id(imdb_id: str) -> Optional[str]:
    """通过 TMDB + IMDb ID 获取头像"""
    tmdb_key = os.getenv("TMDB_API_KEY")
    if not tmdb_key or not imdb_id:
        return None
    try:
        url = f"https://api.themoviedb.org/3/find/{imdb_id}"
        resp = requests.get(
            url,
            params={"api_key": tmdb_key, "external_source": "imdb_id", "language": "en-US"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        persons = resp.json().get("person_results") or []
        if not persons:
            return None
        profile_path = persons[0].get("profile_path")
        if not profile_path:
            return None
        photo_url = f"https://image.tmdb.org/t/p/w500{profile_path}"
        if is_valid_image_url(photo_url):
            return photo_url
    except Exception:
        pass
    return None


def get_tmdb_person_photo_by_name(name: str) -> Optional[str]:
    """通过 TMDB + 姓名搜索获取头像"""
    tmdb_key = os.getenv("TMDB_API_KEY")
    if not tmdb_key or not name:
        return None
    try:
        url = f"https://api.themoviedb.org/3/search/person"
        resp = requests.get(
            url,
            params={"api_key": tmdb_key, "query": name, "language": "en-US"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results") or []
        if not results:
            return None
        profile_path = results[0].get("profile_path")
        if not profile_path:
            return None
        photo_url = f"https://image.tmdb.org/t/p/w500{profile_path}"
        if is_valid_image_url(photo_url):
            return photo_url
    except Exception:
        pass
    return None


def _try_find_photo(page: Dict, db_type: str) -> Tuple[Optional[str], Optional[str]]:
    """尝试从多个来源查找照片，返回 (photo_url, source)"""
    props = page.get("properties") or {}
    name = get_page_title(page)
    props_names = PROPERTY_NAMES[db_type]

    # 获取 Alt-Name
    alt_name = None
    alt_name_prop = props.get("Alt-Name") or {}
    if alt_name_prop.get("type") == "rich_text":
        alt_name = get_property_value(alt_name_prop)

    # 获取 IMDB ID
    imdb_id = None
    if props_names["imdb"]:
        imdb_prop = props.get(props_names["imdb"]) or {}
        if imdb_prop.get("type") == "rich_text":
            imdb_id = get_property_value(imdb_prop)

    search_names = [n for n in [name, alt_name] if n]

    # 1. 尝试 IMDB（仅 Director/Actor，最可靠）
    if imdb_id and db_type in ("director", "actor"):
        try:
            person = get_imdb_person_info(imdb_id)
            photo = (person or {}).get("photo")
            if photo and is_valid_image_url(photo):
                return photo, "IMDB"
        except Exception as e:
            print(f"    IMDB 查询异常: {e}", flush=True)

    # 2. 尝试 TMDB（通过 IMDB ID，可靠）
    if imdb_id and db_type in ("director", "actor"):
        try:
            photo = get_tmdb_person_photo_by_imdb_id(imdb_id)
            if photo:
                return photo, "TMDB"
        except Exception as e:
            print(f"    TMDB (IMDB) 查询异常: {e}", flush=True)

    # 3. 尝试 TMDB（通过姓名，可靠）
    for search_name in search_names:
        try:
            photo = get_tmdb_person_photo_by_name(search_name)
            if photo:
                return photo, "TMDB"
        except Exception as e:
            print(f"    TMDB (姓名) 查询异常: {e}", flush=True)

    # 4. 尝试 Wikidata（通用）
    for search_name in search_names:
        try:
            photo = get_wikidata_person_photo(search_name)
            if photo and is_valid_image_url(photo):
                return photo, "Wikidata"
        except Exception as e:
            print(f"    Wikidata 查询异常: {e}", flush=True)

    return None, None


def _fix_single_record(nh: NotionHelper, page: Dict, db_type: str, dry_run: bool = False) -> Tuple[bool, bool]:
    """修复单条记录的头像，返回 (checked, fixed)"""
    page_id = page.get("id")
    name = get_page_title(page)
    props_names = PROPERTY_NAMES[db_type]

    # 检查是否已有有效照片
    prop_photo = get_files_url(page, props_names["photo"])
    icon_url = get_icon_url(page)
    cover_url = get_cover_url(page)

    if prop_photo and is_valid_image_url(prop_photo):
        # 照片属性有效，但 icon/cover 可能丢失
        if icon_url and is_valid_image_url(icon_url) and cover_url and is_valid_image_url(cover_url):
            return True, False  # 已完整，跳过
        # 回填 icon/cover
        if not dry_run:
            try:
                update_page_media(nh.client, page_id, property_name=None, image_url=prop_photo, write_property=False)
                update_check_fields(nh.client, page, props_names["status"], props_names["checked"], props_names["source"], "Ok")
                remove_data_issue_tags(nh.client, page, {"BrokenPhoto", "MissingPhoto"})
                print(f"  ✅ [{name}] 从 Photo 属性回填 icon/cover", flush=True)
                return True, True
            except Exception as e:
                print(f"  ❌ [{name}] 回填失败: {e}", flush=True)
        return True, False

    print(f"  👤 [{name}] 查找头像...", flush=True)
    photo_url, source = _try_find_photo(page, db_type)

    if not photo_url:
        print(f"  ❌ [{name}] 未找到可用照片", flush=True)
        if not dry_run:
            update_check_fields(nh.client, page, props_names["status"], props_names["checked"], props_names["source"], "Missing")
        return True, False

    print(f"  📷 [{name}] 找到照片: {source} {photo_url[:80]}...", flush=True)

    if dry_run:
        print(f"  🔍 [DRY RUN] 跳过上传", flush=True)
        return True, False

    # 下载并上传到 Notion
    img_data = _download_image(photo_url)
    if not img_data:
        print(f"  ❌ [{name}] 下载图片失败", flush=True)
        return True, False

    token = nh.client.auth
    upload_id = _notion_upload_binary(token, img_data, filename=f"{name[:30]}.jpg")
    if not upload_id:
        print(f"  ❌ [{name}] 上传到 Notion 失败", flush=True)
        return True, False

    # 设置封面
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")
    body = {
        "cover": {"type": "file_upload", "file_upload": {"id": upload_id}},
        "icon": {"type": "file_upload", "file_upload": {"id": upload_id}},
        "properties": {
            props_names["photo"]: {"files": [{"type": "external", "name": "Photo", "external": {"url": photo_url}}]},
            props_names["status"]: {"select": {"name": "Ok"}},
            props_names["checked"]: {"date": {"start": now_str, "time_zone": "Asia/Shanghai"}},
            props_names["source"]: {"select": {"name": source}},
        },
    }
    try:
        resp = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"  ✅ [{name}] 照片已更新: {source}", flush=True)
            remove_data_issue_tags(nh.client, page, {"BrokenPhoto", "MissingPhoto"})
            return True, True
        else:
            print(f"  ❌ [{name}] 更新失败: {resp.status_code}", flush=True)
            return True, False
    except Exception as e:
        print(f"  ❌ [{name}] 更新异常: {e}", flush=True)
        return True, False


def batch_fix(db_type: str, dry_run: bool = False, max_workers: int = 4):
    """批量修复指定类型的数据库"""
    print(f"[{db_type}] 开始初始化...", flush=True)
    db_id = DATABASE_IDS.get(db_type)
    if not db_id:
        print(f"未知数据库类型: {db_type}", flush=True)
        return

    # 根据数据库类型选择 token
    if db_type in ("director", "actor"):
        token = os.getenv("MOVIE_NOTION_TOKEN")
        page_url = os.getenv("NOTION_MOVIE_URL")
    else:
        token = os.getenv("BOOK_NOTION_TOKEN")
        page_url = os.getenv("NOTION_BOOK_URL")

    print(f"[{db_type}] token={'已设置' if token else '未设置'}, page_url={'已设置' if page_url else '未设置'}", flush=True)

    if not token or not page_url:
        print(f"缺少环境变量: {'MOVIE_NOTION_TOKEN' if db_type in ('director', 'actor') else 'BOOK_NOTION_TOKEN'}", flush=True)
        return

    print(f"[{db_type}] 初始化 NotionHelper...", flush=True)
    nh = NotionHelper("movie" if db_type in ("director", "actor") else "book")
    print(f"[{db_type}] NotionHelper 初始化完成", flush=True)

    # 查询需要修复的记录
    print(f"[{db_type}] 查询需要修复的记录 (db_id={db_id})...", flush=True)
    try:
        pages = _query_needing_repair(nh, db_id, "PhotoStatus")
        print(f"[{db_type}] 查询完成，找到 {len(pages)} 条记录", flush=True)
    except Exception as e:
        print(f"[{db_type}] 查询失败: {e}", flush=True)
        return
    if not pages:
        print(f"[{db_type}] 没有需要修复的记录", flush=True)
        return

    print(f"\n{'='*60}")
    print(f"[{db_type}] 找到 {len(pages)} 条需要修复的记录")
    print(f"{'='*60}")

    fixed = 0
    checked = 0

    # 串行处理（避免 API 限流）
    for i, page in enumerate(pages):
        try:
            c, f = _fix_single_record(nh, page, db_type, dry_run)
            checked += c
            fixed += f
            print(f"  [{i+1}/{len(pages)}] 进度: {fixed}/{checked}")
        except Exception as e:
            print(f"  ❌ 处理失败: {e}")
        # 每处理一条暂停 0.3 秒，避免 API 限流
        time.sleep(0.3)

    print(f"\n[{db_type}] 完成: 检查={checked}, 修复={fixed}")


def main():
    import argparse
    print("脚本启动...", flush=True)

    parser = argparse.ArgumentParser(description="批量修复头像/照片")
    print("解析参数...", flush=True)
    parser.add_argument("scope", nargs="?", default="all", choices=["all", "director", "actor", "author"])
    parser.add_argument("--dry-run", action="store_true", help="仅查找，不上传")
    parser.add_argument("--workers", type=int, default=4, help="并发数")
    args = parser.parse_args()

    scopes = ["director", "actor", "author"] if args.scope == "all" else [args.scope]

    for scope in scopes:
        print(f"处理 {scope}...", flush=True)
        batch_fix(scope, dry_run=args.dry_run, max_workers=args.workers)
        print(f"{scope} 完成", flush=True)


if __name__ == "__main__":
    main()
