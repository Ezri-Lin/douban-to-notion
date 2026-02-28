import argparse
import json
import os
import re
from bs4 import BeautifulSoup
import pendulum
from retrying import retry
import requests
from dotenv import load_dotenv

load_dotenv()

from douban2notion.notion_helper import NotionHelper
from douban2notion import utils

DOUBAN_API_HOST = os.getenv("DOUBAN_API_HOST", "frodo.douban.com")
DOUBAN_API_KEY = os.getenv("DOUBAN_API_KEY", "0ac44ae016490db2204ce0a042db2916")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

from douban2notion.config import (
    movie_properties_type_dict,
    book_properties_type_dict,
    TAG_ICON_URL,
    USER_ICON_URL,
    MAX_ACTORS_RELATION,
    MAX_DIRECTORS_RELATION,
    MAX_CATEGORIES_RELATION,
    MAX_AUTHORS_RELATION,
    MAX_PUBLISHERS_MULTI_SELECT
)
from douban2notion.utils import get_icon

rating = {
    1: "⭐️",
    2: "⭐️⭐️",
    3: "⭐️⭐️⭐️",
    4: "⭐️⭐️⭐️⭐️",
    5: "⭐️⭐️⭐️⭐️⭐️",
}
movie_status = {
    "mark": "Mark",
    "doing": "Doing",
    "done": "Done",
}
book_status = {
    "mark": "Mark",
    "doing": "Doing",
    "done": "Done",
}
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

headers = {
    "host": DOUBAN_API_HOST,
    "authorization": f"Bearer {AUTH_TOKEN}" if AUTH_TOKEN else "",
    "user-agent": "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 15_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.16(0x18001023) NetType/WIFI Language/zh_CN",
    "referer": "https://servicewechat.com/wx2f9b06c1de1ccfca/84/page-frame.html",
}

# 外链封面可用性缓存，避免重复网络探测
COVER_URL_VALIDITY_CACHE = {}
AUTHOR_PHOTO_CACHE = {}
IMDB_INFO_CACHE = {}
IMDB_CAST_CREW_CACHE = {}
IMDB_PERSON_CACHE = {}
IMDB_SEARCH_CACHE = {}
TMDB_SEARCH_CACHE = {}
RELATION_NAME_CACHE = {}
DOUBAN_SUBJECT_DETAIL_CACHE = {}
IMDB_MEDIA_TYPE_CACHE = {}

# 豆瓣中文标题 -> IMDB英文检索词（通过环境变量配置，避免硬编码样本数据）
DEFAULT_IMDB_TITLE_ALIAS_MAP = {}
DEFAULT_TMDB_ID_OVERRIDE_MAP = {}

def _load_imdb_alias_map():
    alias_map = dict(DEFAULT_IMDB_TITLE_ALIAS_MAP)
    raw = os.getenv("IMDB_TITLE_ALIAS_JSON", "").strip()
    if not raw:
        return alias_map
    try:
        custom = json.loads(raw)
        if isinstance(custom, dict):
            alias_map.update({str(k): str(v) for k, v in custom.items() if k and v})
    except Exception:
        print("  IMDB_TITLE_ALIAS_JSON 解析失败，忽略自定义别名")
    return alias_map


IMDB_TITLE_ALIAS_MAP = _load_imdb_alias_map()


def _load_tmdb_id_override_map():
    """支持通过环境变量为特定条目指定TMDB ID，用于冷门中文条目回填。"""
    override_map = dict(DEFAULT_TMDB_ID_OVERRIDE_MAP)
    raw = os.getenv("TMDB_ID_OVERRIDE_JSON", "").strip()
    if not raw:
        return override_map
    try:
        custom = json.loads(raw)
        if isinstance(custom, dict):
            for key, value in custom.items():
                if not key or value is None:
                    continue
                if isinstance(value, dict):
                    override_map[str(key).strip()] = {
                        "id": str(value.get("id") or "").strip(),
                        "type": str(value.get("type") or "").strip().lower() or None,
                        "imdb_id": str(value.get("imdb_id") or "").strip() or None,
                        "original_title": str(value.get("original_title") or "").strip() or None,
                        "poster_url": str(value.get("poster_url") or "").strip() or None,
                    }
                else:
                    override_map[str(key).strip()] = {
                        "id": str(value).strip(),
                        "type": None,
                        "imdb_id": None,
                        "original_title": None,
                        "poster_url": None,
                    }
    except Exception:
        print("  TMDB_ID_OVERRIDE_JSON 解析失败，忽略自定义映射")
    return override_map


TMDB_ID_OVERRIDE_MAP = _load_tmdb_id_override_map()

def is_chinese_movie(title, countries=None, original_title=None):
    """
    判断是否为中文电影
    - 第一地区是中国相关（大陆/香港/台湾）→ 华语片
    - 第一地区是外国（即使后面有中国地区，如好莱坞合拍片）→ 外国片
    - 有 original_title 且与 title 不同 → 外国片
    - 默认华语
    """
    chinese_regions = ['中国大陆', '中国香港', '中国台湾', '中国', '香港', '台湾', 'China', 'Hong Kong', 'Taiwan']

    if countries:
        # 只看第一地区
        first_country = countries[0] if isinstance(countries, list) else str(countries).split()[0]
        return any(region in first_country for region in chinese_regions)

    if original_title and original_title != title:
        return False

    return True


def _extract_subject_countries(subject):
    """优先使用结构化 countries，缺失时从 card_subtitle 宽松解析地区字段。"""
    countries = subject.get("countries") or []
    if countries:
        return countries
    card_subtitle = subject.get("card_subtitle", "")
    if not card_subtitle:
        return []
    parts = re.split(r"\s*/\s*", card_subtitle)
    if len(parts) < 2:
        return []
    raw_countries = parts[1]
    return [c for c in re.split(r"[,\s、]+", raw_countries) if c]


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def fetch_subjects(user, type_, status):
    offset = 0
    page = 0
    url = f"https://{DOUBAN_API_HOST}/api/v2/user/{user}/interests"
    total = 0
    results = []
    while True:
        params = {
            "type": type_,
            "count": 50,
            "status": status,
            "start": offset,
            "apiKey": DOUBAN_API_KEY,
        }
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            # Avoid infinite loop on auth/network failures.
            snippet = response.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"fetch_subjects failed: status={response.status_code}, user={user}, type={type_}, "
                f"status_tag={status}, body={snippet}"
            )

        payload = response.json()
        interests = payload.get("interests") or []
        if len(interests) == 0:
            break
        results.extend(interests)
        print(f"total = {total}")
        print(f"size = {len(results)}")
        page += 1
        offset = page * 50
    return results



def _match_title_filter(title, only_titles=None):
    if not only_titles:
        return True
    probe = (title or "").strip().lower()
    if not probe:
        return False
    return any(str(item).strip().lower() in probe for item in only_titles if str(item).strip())


def _match_db_url_filter(db_url, only_db_urls=None):
    if not only_db_urls:
        return True
    probe = str(db_url or "").strip()
    if not probe:
        return False
    probe_id = _extract_douban_id_from_url(probe)
    for item in only_db_urls:
        candidate = str(item or "").strip()
        if not candidate:
            continue
        if candidate == probe:
            return True
        candidate_id = _extract_douban_id_from_url(candidate)
        if candidate_id and probe_id and candidate_id == probe_id:
            return True
        if candidate.isdigit() and probe_id and candidate == probe_id:
            return True
    return False


def insert_movie(
    douban_name,
    notion_helper,
    only_titles=None,
    only_db_urls=None,
    limit=0,
    existing_only=False,
    dedupe_duplicates=False,
    dedupe_only=False,
):
    notion_movies = notion_helper.query_all(database_id=notion_helper.movie_database_id)
    notion_movie_dict = {}
    notion_movie_duplicates = {}
    notion_movie_imdb_dict = {}
    notion_movie_title_year_dict = {}
    for i in notion_movies:
        movie = {}
        for key, value in i.get("properties").items():
            movie[key] = utils.get_property_value(value)
        db_url = movie.get("DB_Url") or movie.get("Url")
        current_movie = {
            "Remark": movie.get("Remark"),
            "Status": movie.get("Status"),
            "Date": movie.get("Date"),
            "Rating": movie.get("Rating"),
            "Wrong": movie.get("Wrong"),
            "Actor": movie.get("Actor"),
            "Director": movie.get("Director"),
            "IMDB": movie.get("IMDB"),
            "IMDB_Url": movie.get("IMDB_Url"),
            "Name": movie.get("Name"),
            "MovieName": movie.get("MovieName"),
            "Year": movie.get("Year"),
            "Season": movie.get("Season"),
            "Cover": movie.get("Cover"),
            "CoverSource": movie.get("CoverSource"),
            "CoverStatus": movie.get("CoverStatus"),
            "page_id": i.get("id")
        }
        if db_url:
            notion_movie_duplicates.setdefault(db_url, []).append(current_movie)
        if current_movie.get("IMDB"):
            notion_movie_imdb_dict[current_movie.get("IMDB")] = current_movie
        for unique_key in _build_movie_unique_keys(current_movie.get("Name"), current_movie.get("MovieName"), current_movie.get("Year")):
            notion_movie_title_year_dict[unique_key] = current_movie
    for db_url, records in notion_movie_duplicates.items():
        preferred = _choose_preferred_movie_record(records)
        if preferred:
            notion_movie_dict[db_url] = preferred
    if dedupe_duplicates:
        archived_count = _archive_duplicate_movie_pages(notion_helper, notion_movie_duplicates)
        compacted_duplicates = {}
        for db_url, records in notion_movie_duplicates.items():
            preferred = _choose_preferred_movie_record(records)
            if preferred:
                compacted_duplicates[db_url] = [preferred]
        notion_movie_duplicates = compacted_duplicates
        print(f"重复页归档完成: {archived_count}")
        if dedupe_only:
            return
    results = []
    for i in movie_status.keys():
        results.extend(fetch_subjects(douban_name, "movie", i))
    processed_count = 0
    for result in results:
        movie = {}
        if not result:
            print(result)
            continue
        subject = result.get("subject")
        douban_title = subject.get("title")  # 豆瓣标题
        db_url = subject.get("url")
        if not _match_title_filter(douban_title, only_titles):
            continue
        if not _match_db_url_filter(db_url, only_db_urls):
            continue
        if limit and processed_count >= limit:
            break
        processed_count += 1

        # 验证必要字段
        if not douban_title or douban_title == "未知电影":
            print(f"跳过无效电影: {douban_title}")
            continue
        if not subject.get("year"):
            print(f"跳过无年份电影: {douban_title}")
            continue

        # 判断是否为中文电影
        countries = _extract_subject_countries(subject)
        original_title = subject.get("original_title")  # 获取原名
        alias_titles = _extract_alias_titles(subject)
        is_chinese = is_chinese_movie(douban_title, countries, original_title)

        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)

        movie["Date"] = create_time.int_timestamp
        movie["DB_Url"] = db_url
        movie["Status"] = movie_status.get(result.get("status"))
        movie["DoubanRating"] = subject.get("rating", {}).get("value", 0) if subject.get("rating") else 0
        movie["Year"] = subject.get("year")

        if result.get("rating"):
            movie["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            movie["Remark"] = result.get("comment")

        # 存储原始信息，稍后根据语言设置Name和MovieName
        movie["_douban_title"] = douban_title
        movie["_original_title"] = original_title  # 存储原名
        movie["_alias_titles"] = alias_titles
        movie["_is_chinese"] = is_chinese
        season_number = _extract_season_number(douban_title) or _extract_season_number(original_title)
        season_label = _format_season_label(season_number)
        if season_label:
            if notion_helper.ensure_select_option(notion_helper.movie_database_id, "Season", season_label):
                movie["Season"] = season_label
            else:
                print(f"  Season选项不存在，跳过写入: {season_label}（请先在Notion手动添加该Select选项）")
        if notion_movie_dict.get(movie.get("DB_Url")):
            notion_movive = notion_movie_dict.get(movie.get("DB_Url"))
            subtype = subject.get("subtype", "movie")

            douban_title = movie.get("_douban_title")
            is_chinese = movie.get("_is_chinese")
            original_title = movie.get("_original_title")
            alias_titles = movie.get("_alias_titles") or []
            force_fix_wrong = bool(notion_movive.get("Wrong"))
            basic_changed = (
                notion_movive.get("Date") != movie.get("Date")
                or notion_movive.get("Remark") != movie.get("Remark")
                or notion_movive.get("Status") != movie.get("Status")
                or notion_movive.get("Rating") != movie.get("Rating")
                or notion_movive.get("Season") != movie.get("Season")
            )
            needs_metadata_backfill = (
                force_fix_wrong
                or
                not notion_movive.get("Actor")
                or not notion_movive.get("Director")
                or not notion_movive.get("Cover")
                or not notion_movive.get("IMDB")
                or (
                    notion_movive.get("IMDB")
                    and not _is_existing_imdb_consistent(
                        douban_title,
                        notion_movive.get("IMDB"),
                        original_title=original_title,
                        alias_titles=alias_titles,
                        expected_media_type=subtype,
                        expected_year=movie.get("Year"),
                        douban_url=movie.get("DB_Url"),
                    )
                )
                or not notion_movive.get("MovieName")
                or (is_chinese and not _relation_names_are_chinese(notion_helper, notion_movive.get("Actor") or []))
                or (is_chinese and not _relation_names_are_chinese(notion_helper, notion_movive.get("Director") or []))
                or (not is_chinese and _has_chinese(notion_movive.get("Name")))
                or (not is_chinese and notion_movive.get("Actor") and _relation_names_are_chinese(notion_helper, notion_movive.get("Actor") or []))
                or (not is_chinese and notion_movive.get("Director") and _relation_names_are_chinese(notion_helper, notion_movive.get("Director") or []))
            )
            # Fast path: most rows are unchanged; skip expensive IMDB/TMDB requests.
            if not basic_changed and not needs_metadata_backfill:
                continue

            # ── 获取 IMDB 信息 ──────────────────────────────────────
            resolved_original_title, resolved_alias_titles = _resolve_original_and_alias_titles(
                subject,
                douban_url=movie.get("DB_Url"),
                subtype=subtype,
            )
            if resolved_original_title:
                original_title = resolved_original_title
            if resolved_alias_titles:
                alias_titles = resolved_alias_titles
            movie["_original_title"] = original_title
            movie["_alias_titles"] = alias_titles
            lookup_year = _lookup_year_for_imdb(douban_title, subtype, movie.get("Year"))
            tmdb_poster = None
            notion_imdb_id = _normalize_imdb_id(notion_movive.get("IMDB"))
            existing_imdb_consistent = False
            if notion_imdb_id:
                existing_imdb_consistent = _is_existing_imdb_consistent(
                    douban_title,
                    notion_imdb_id,
                    original_title=original_title,
                    alias_titles=alias_titles,
                    expected_media_type=subtype,
                    expected_year=movie.get("Year"),
                    douban_url=movie.get("DB_Url"),
                )
            fallback_existing_imdb = notion_imdb_id if existing_imdb_consistent else None
            imdb_id = notion_imdb_id or get_imdb(movie.get("DB_Url"))
            if imdb_id and not _is_imdb_media_type_compatible(imdb_id, subtype):
                imdb_id = None
            if notion_imdb_id and not existing_imdb_consistent:
                print(f"  Notion现有IMDB({notion_imdb_id})与标题不一致，尝试重新检索: {douban_title}")
                imdb_id = None
            if imdb_id and not notion_imdb_id:
                existing_imdb_info = get_imdb_info(imdb_id)
                if existing_imdb_info and not _is_imdb_title_consistent(
                    douban_title,
                    existing_imdb_info.get("title"),
                    original_title=original_title,
                    alias_titles=alias_titles,
                ):
                    print(f"  现有IMDB({imdb_id})与标题不一致，重新检索: {douban_title}")
                    imdb_id = None
            if not imdb_id:
                found_by_search = False
                for search_title in _build_imdb_search_candidates(douban_title, original_title, aliases=alias_titles):
                    imdb_id = search_imdb_by_title(search_title, lookup_year, media_type=subtype)
                    if not imdb_id:
                        imdb_id = search_imdb_by_title(search_title, media_type=subtype)
                    if imdb_id:
                        found_by_search = True
                        break
                if not found_by_search and _should_try_tmdb_fallback(
                    is_chinese,
                    countries,
                    original_title,
                    douban_title=douban_title,
                    douban_url=movie.get("DB_Url"),
                ):
                    tmdb_query_title = _strip_season(douban_title)
                    if original_title and _has_latin(original_title):
                        tmdb_query_title = _strip_season(original_title)
                    tmdb_imdb_id, tmdb_original_title, tmdb_poster = search_tmdb_for_imdb(
                        tmdb_query_title,
                        lookup_year,
                        media_type=subtype,
                        douban_url=movie.get("DB_Url"),
                    )
                    if not original_title and tmdb_original_title:
                        original_title = tmdb_original_title
                    if tmdb_imdb_id:
                        imdb_id = tmdb_imdb_id
                        print(f"  TMDB兜底命中IMDB: {imdb_id}")
                        found_by_search = True
                if not found_by_search:
                    if subtype == "tv" and _extract_season_number(douban_title):
                        inherited_imdb_id = _inherit_series_imdb_from_existing_rows(
                            douban_title,
                            original_title,
                            notion_movie_dict,
                            target_year=movie.get("Year"),
                            alias_titles=alias_titles,
                        )
                        if inherited_imdb_id:
                            imdb_id = inherited_imdb_id
                            found_by_search = True
                            print(f"  从同系列条目继承IMDB: {imdb_id}")
                    if not found_by_search and fallback_existing_imdb:
                        imdb_id = fallback_existing_imdb
                        found_by_search = True
                        print(f"  重检未命中，保留Notion已有IMDB: {imdb_id}")
                    if not found_by_search:
                        print(f"  IMDB检索失败: {douban_title} ({movie.get('Year')}, {subtype})")
            clear_stale_imdb = bool(notion_imdb_id and not existing_imdb_consistent and not imdb_id)
            if clear_stale_imdb:
                print(f"  清理不一致IMDB: {notion_imdb_id}")
            imdb_info = None
            if imdb_id:
                imdb_info = get_imdb_info(imdb_id)
                movie["IMDB"] = imdb_id
                movie["IMDB_Url"] = f"https://www.imdb.com/title/{imdb_id}/"
                if _should_force_foreign_by_imdb(douban_title, (imdb_info or {}).get("title"), countries):
                    is_chinese = False

            # ── 计算正确的 Name / MovieName / Cover ─────────────────
            clean_douban_title = _strip_season(douban_title)
            clean_original_title = _strip_season(original_title)
            if is_chinese:
                movie["Name"] = clean_douban_title
                chinese_alias = _resolve_movie_alias_for_chinese(clean_douban_title, clean_original_title, imdb_info)
                if chinese_alias:
                    movie["MovieName"] = chinese_alias
            else:
                # 外文电影：Name=原名，MovieName=中文译名
                resolved_foreign_name = None
                if clean_original_title:
                    resolved_foreign_name = _normalize_full_title(clean_original_title)
                elif imdb_info and imdb_info.get('title'):
                    resolved_foreign_name = _normalize_full_title(_strip_season(imdb_info['title']))
                elif _get_alias_title(clean_douban_title):
                    resolved_foreign_name = _normalize_full_title(_get_alias_title(clean_douban_title))
                if resolved_foreign_name:
                    movie["Name"] = resolved_foreign_name
                else:
                    # Keep existing foreign name only when current IMDB mapping is still trusted.
                    # If IMDB is missing/replaced after consistency checks, do not preserve stale foreign names.
                    trusted_existing_foreign = (
                        _is_foreign_style_name(notion_movive.get("Name"), notion_movive.get("MovieName"))
                        and notion_movive.get("IMDB")
                        and notion_movive.get("IMDB") == movie.get("IMDB")
                    )
                    if trusted_existing_foreign:
                        movie["Name"] = notion_movive.get("Name")
                    else:
                        movie["Name"] = clean_douban_title
                movie["MovieName"] = clean_douban_title

            if imdb_info and imdb_info.get('rating'):
                movie["IMDBRating"] = imdb_info['rating']
            resolved_cover, resolved_cover_source, resolved_cover_status = _resolve_cover_from_sources(
                imdb_info,
                subject,
                current_cover=notion_movive.get("Cover"),
                tmdb_cover=tmdb_poster,
            )
            movie["Cover"] = resolved_cover
            movie["CoverStatus"] = resolved_cover_status
            if resolved_cover_source:
                movie["CoverSource"] = resolved_cover_source
            movie["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp

            # ── 判断是否有实质变化需要更新 ───────────────────────────
            current_name = notion_movive.get("Name")
            needs_update = (
                needs_metadata_backfill
                or
                notion_movive.get("Date") != movie.get("Date")
                or notion_movive.get("Remark") != movie.get("Remark")
                or notion_movive.get("Status") != movie.get("Status")
                or notion_movive.get("Rating") != movie.get("Rating")
                or current_name != movie.get("Name")
                or notion_movive.get("MovieName") != movie.get("MovieName")
                or notion_movive.get("Season") != movie.get("Season")
                or (movie.get("Cover") and notion_movive.get("Cover") != movie.get("Cover"))
                or (movie.get("CoverSource") and notion_movive.get("CoverSource") != movie.get("CoverSource"))
                or notion_movive.get("CoverStatus") != movie.get("CoverStatus")
                or (movie.get("IMDB") and notion_movive.get("IMDB") != movie.get("IMDB"))
                or (movie.get("IMDB_Url") and notion_movive.get("IMDB_Url") != movie.get("IMDB_Url"))
            )

            if needs_update:
                cast_crew = None
                if imdb_id:
                    cast_crew = get_imdb_cast_and_crew(imdb_id)

                # ── Actor ────────────────────────────────────────────
                if is_chinese:
                    # 中文片：如果有IMDB，优先每次按豆瓣中文名刷新关系；否则缺失时补齐
                    if cast_crew and cast_crew['actors'] and subject.get("actors"):
                        actor_ids = []
                        douban_actors = subject.get("actors")[0:MAX_ACTORS_RELATION]
                        for idx, douban_actor in enumerate(douban_actors):
                            actor_name = douban_actor.get("name")
                            if not actor_name:
                                continue
                            imdb_person_id = None
                            if idx < len(cast_crew['actors']):
                                imdb_person_id = (cast_crew['actors'][idx] or {}).get("id")
                            person_info = (
                                _build_person_info_payload(imdb_person_id, c_name=actor_name)
                                if imdb_person_id
                                else None
                            )
                            actor_ids.append(notion_helper.get_relation_id(
                                actor_name, notion_helper.actor_database_id, USER_ICON_URL, {}, person_info
                            ))
                        if actor_ids:
                            movie["Actor"] = actor_ids
                    elif not notion_movive.get("Actor") and subject.get("actors"):
                        movie["Actor"] = [
                            notion_helper.get_relation_id(
                                x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                            )
                            for x in subject.get("actors")[0:MAX_ACTORS_RELATION]
                        ]
                else:
                    # 外文片每次更新都以 IMDB 英文演职员覆盖，避免长期停留在中文关系
                    if cast_crew and cast_crew['actors']:
                        actor_ids = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            person_info = _build_person_info_payload(actor.get("id"), c_name=None)
                            actor_ids.append(notion_helper.get_relation_id(
                                actor['name'], notion_helper.actor_database_id, USER_ICON_URL, {}, person_info
                            ))
                        movie["Actor"] = _ensure_actor_relations(
                            actor_ids, subject, notion_helper, allow_douban_fallback=False
                        )
                    elif not notion_movive.get("Actor"):
                        fallback_actor_names = _pick_relation_names(
                            subject.get("actors"),
                            MAX_ACTORS_RELATION,
                            prefer_latin=True,
                        )
                        if fallback_actor_names:
                            movie["Actor"] = [
                                notion_helper.get_relation_id(
                                    name, notion_helper.actor_database_id, USER_ICON_URL
                                )
                                for name in fallback_actor_names
                            ]
                        elif notion_movive.get("Actor") and _relation_names_are_chinese(
                            notion_helper, notion_movive.get("Actor") or []
                        ):
                            movie["Actor"] = []

                # ── Director ─────────────────────────────────────────
                if is_chinese:
                    # 中文片：如果有IMDB，优先每次按豆瓣中文名刷新关系；否则缺失时补齐
                    if cast_crew and cast_crew['directors'] and subject.get("directors"):
                        director_ids = []
                        douban_directors = subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                        for idx, douban_director in enumerate(douban_directors):
                            director_name = douban_director.get("name")
                            if not director_name:
                                continue
                            imdb_person_id = None
                            if idx < len(cast_crew['directors']):
                                imdb_person_id = (cast_crew['directors'][idx] or {}).get("id")
                            person_info = (
                                _build_person_info_payload(imdb_person_id, c_name=director_name)
                                if imdb_person_id
                                else None
                            )
                            director_ids.append(notion_helper.get_relation_id(
                                director_name, notion_helper.director_database_id, USER_ICON_URL, {}, person_info
                            ))
                        if director_ids:
                            movie["Director"] = director_ids
                    elif not notion_movive.get("Director") and subject.get("directors"):
                        movie["Director"] = [
                            notion_helper.get_relation_id(
                                x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                            )
                            for x in subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                        ]
                else:
                    if cast_crew and cast_crew['directors']:
                        director_ids = []
                        for idx, director in enumerate(cast_crew['directors']):
                            person_info = _build_person_info_payload(director.get("id"), c_name=None)
                            director_ids.append(notion_helper.get_relation_id(
                                director['name'], notion_helper.director_database_id, USER_ICON_URL, {}, person_info
                            ))
                        movie["Director"] = director_ids
                    elif not notion_movive.get("Director") and subject.get("directors"):
                        # 当IMDb未提供导演条目时，优先英文导演名；若无英文则回退可用导演名避免字段为空。
                        fallback_director_names = _pick_relation_names(
                            subject.get("directors"),
                            MAX_DIRECTORS_RELATION,
                            prefer_latin=True,
                        )
                        if fallback_director_names:
                            movie["Director"] = [
                                notion_helper.get_relation_id(
                                    name, notion_helper.director_database_id, USER_ICON_URL
                                )
                                for name in fallback_director_names
                            ]
                    elif notion_movive.get("Director") and _relation_names_are_chinese(
                        notion_helper, notion_movive.get("Director") or []
                    ):
                        movie["Director"] = []

                # 清理临时字段
                movie.pop("_douban_title", None)
                movie.pop("_original_title", None)
                movie.pop("_alias_titles", None)
                movie.pop("_is_chinese", None)

                properties = utils.get_properties(movie, movie_properties_type_dict)
                if clear_stale_imdb:
                    properties["IMDB"] = {"rich_text": []}
                    properties["IMDB_Url"] = {"url": None}
                    properties["IMDBRating"] = {"number": None}
                movie_display = f"{movie.get('Name', 'N/A')}"
                if movie.get("MovieName"):
                    movie_display += f" / {movie.get('MovieName')}"
                print(f"更新: {movie_display}")
                notion_helper.get_date_relation(properties,create_time)

                # 如果有新封面，同时更新icon
                icon = None
                if movie.get("Cover"):
                    icon = get_icon(movie.get("Cover"))

                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                    icon=icon
            )
                duplicate_rows = notion_movie_duplicates.get(movie.get("DB_Url")) or []
                for duplicate_row in duplicate_rows:
                    duplicate_page_id = duplicate_row.get("page_id")
                    if not duplicate_page_id or duplicate_page_id == notion_movive.get("page_id"):
                        continue
                    notion_helper.update_page(
                        page_id=duplicate_page_id,
                        properties=properties,
                        icon=icon,
                    )
                notion_movive.update({
                    "Remark": movie.get("Remark"),
                    "Status": movie.get("Status"),
                    "Date": movie.get("Date"),
                    "Rating": movie.get("Rating"),
                    "Actor": movie.get("Actor", notion_movive.get("Actor")),
                    "Director": movie.get("Director", notion_movive.get("Director")),
                    "IMDB": (
                        movie["IMDB"]
                        if "IMDB" in movie
                        else (None if clear_stale_imdb else notion_movive.get("IMDB"))
                    ),
                    "IMDB_Url": (
                        movie["IMDB_Url"]
                        if "IMDB_Url" in movie
                        else (None if clear_stale_imdb else notion_movive.get("IMDB_Url"))
                    ),
                    "Name": movie.get("Name", notion_movive.get("Name")),
                    "MovieName": movie.get("MovieName", notion_movive.get("MovieName")),
                    "Year": movie.get("Year", notion_movive.get("Year")),
                    "Season": movie.get("Season", notion_movive.get("Season")),
                    "Cover": movie.get("Cover", notion_movive.get("Cover")),
                    "CoverSource": movie.get("CoverSource", notion_movive.get("CoverSource")),
                    "CoverStatus": movie.get("CoverStatus", notion_movive.get("CoverStatus")),
                })
                if notion_movive.get("IMDB"):
                    notion_movie_imdb_dict[notion_movive.get("IMDB")] = notion_movive
                for unique_key in _build_movie_unique_keys(
                    notion_movive.get("Name"), notion_movive.get("MovieName"), notion_movive.get("Year")
                ):
                    notion_movie_title_year_dict[unique_key] = notion_movive

        else:
            if existing_only:
                continue
            douban_title = movie.get("_douban_title")
            is_chinese = movie.get("_is_chinese")
            original_title = movie.get("_original_title")
            alias_titles = movie.get("_alias_titles") or []

            print(f"插入{douban_title} ({'中文片' if is_chinese else '外文片'})")

            # ── 获取 IMDB 信息 ──────────────────────────────────────
            subtype = subject.get("subtype", "movie")
            resolved_original_title, resolved_alias_titles = _resolve_original_and_alias_titles(
                subject,
                douban_url=movie.get("DB_Url"),
                subtype=subtype,
            )
            if resolved_original_title:
                original_title = resolved_original_title
            if resolved_alias_titles:
                alias_titles = resolved_alias_titles
            lookup_year = _lookup_year_for_imdb(douban_title, subtype, movie.get("Year"))
            tmdb_poster = None
            imdb_id = get_imdb(movie.get("DB_Url"))
            if imdb_id and not _is_imdb_media_type_compatible(imdb_id, subtype):
                imdb_id = None
            if not imdb_id:
                print(f"  豆瓣页面无IMDB信息，尝试搜索IMDB...")
                found_by_search = False
                for search_title in _build_imdb_search_candidates(douban_title, original_title, aliases=alias_titles):
                    imdb_id = search_imdb_by_title(search_title, lookup_year, media_type=subtype)
                    if not imdb_id:
                        imdb_id = search_imdb_by_title(search_title, media_type=subtype)
                    if imdb_id:
                        found_by_search = True
                        break
                if not found_by_search and _should_try_tmdb_fallback(
                    is_chinese,
                    countries,
                    original_title,
                    douban_title=douban_title,
                    douban_url=movie.get("DB_Url"),
                ):
                    tmdb_query_title = _strip_season(douban_title)
                    if original_title and _has_latin(original_title):
                        tmdb_query_title = _strip_season(original_title)
                    tmdb_imdb_id, tmdb_original_title, tmdb_poster = search_tmdb_for_imdb(
                        tmdb_query_title,
                        lookup_year,
                        media_type=subtype,
                        douban_url=movie.get("DB_Url"),
                    )
                    if not original_title and tmdb_original_title:
                        original_title = tmdb_original_title
                    if tmdb_imdb_id:
                        imdb_id = tmdb_imdb_id
                        print(f"  TMDB兜底命中IMDB: {imdb_id}")
                        found_by_search = True
                if not found_by_search:
                    if subtype == "tv" and _extract_season_number(douban_title):
                        inherited_imdb_id = _inherit_series_imdb_from_existing_rows(
                            douban_title,
                            original_title,
                            notion_movie_dict,
                            target_year=movie.get("Year"),
                            alias_titles=alias_titles,
                        )
                        if inherited_imdb_id:
                            imdb_id = inherited_imdb_id
                            found_by_search = True
                            print(f"  从同系列条目继承IMDB: {imdb_id}")
                    if not found_by_search:
                        print(f"  IMDB检索失败: {douban_title} ({movie.get('Year')}, {subtype})")

            imdb_info = None
            if imdb_id:
                movie["IMDB"] = imdb_id
                movie["IMDB_Url"] = f"https://www.imdb.com/title/{imdb_id}/"
                imdb_info = get_imdb_info(imdb_id)
                if _should_force_foreign_by_imdb(douban_title, (imdb_info or {}).get("title"), countries):
                    is_chinese = False

            # ── 设置 Name / MovieName ────────────────────────────────
            clean_douban_title = _strip_season(douban_title)
            clean_original_title = _strip_season(original_title)
            if is_chinese:
                movie["Name"] = clean_douban_title
                chinese_alias = _resolve_movie_alias_for_chinese(clean_douban_title, clean_original_title, imdb_info)
                if chinese_alias:
                    movie["MovieName"] = chinese_alias
            else:
                # 外文电影：Name=原名（优先豆瓣original_title，其次IMDB），MovieName=豆瓣中文译名
                if clean_original_title:
                    movie["Name"] = _normalize_full_title(clean_original_title)
                    print(f"  原名: {clean_original_title}")
                elif imdb_info and imdb_info.get('title'):
                    movie["Name"] = _normalize_full_title(_strip_season(imdb_info['title']))
                    print(f"  IMDB原名: {movie['Name']}")
                elif _get_alias_title(clean_douban_title):
                    movie["Name"] = _normalize_full_title(_get_alias_title(clean_douban_title))
                else:
                    movie["Name"] = clean_douban_title
                movie["MovieName"] = clean_douban_title
                print(f"  中文译名: {clean_douban_title}")

            # ── 封面：优先IMDB，回退豆瓣 ─────────────────────────────
            cover, cover_source, cover_status = _resolve_cover_from_sources(
                imdb_info,
                subject,
                tmdb_cover=tmdb_poster,
            )
            if imdb_info and imdb_info.get('rating'):
                movie["IMDBRating"] = imdb_info['rating']
            if not cover:
                print(f"  IMDB/豆瓣封面均获取失败")
            movie["CoverStatus"] = cover_status
            if cover_source:
                movie["CoverSource"] = cover_source
            movie["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp

            movie["Cover"] = cover
            movie["Medium"] = subject.get("type")

            # 清理临时字段
            movie.pop("_douban_title", None)
            movie.pop("_original_title", None)
            movie.pop("_alias_titles", None)
            movie.pop("_is_chinese", None)

            # 添加分类
            if subject.get("genres"):
                movie["Category"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in subject.get("genres")[0:MAX_CATEGORIES_RELATION]
                ]

            # 根据语言选择Actor/Director数据源
            if is_chinese:
                # 中文电影：有IMDB时优先走IMDB（保留豆瓣中文名到 C-Name），无IMDB回退豆瓣
                if imdb_id:
                    print(f"  中文条目使用IMDB补充演员/导演信息")
                    cast_crew = get_imdb_cast_and_crew(imdb_id)

                    if cast_crew['actors'] and subject.get("actors"):
                        actor_relations = []
                        douban_actors = subject.get("actors")[0:MAX_ACTORS_RELATION]
                        for idx, douban_actor in enumerate(douban_actors):
                            actor_name = douban_actor.get("name")
                            if not actor_name:
                                continue
                            imdb_person_id = None
                            if idx < len(cast_crew['actors']):
                                imdb_person_id = (cast_crew['actors'][idx] or {}).get("id")
                            person_info = (
                                _build_person_info_payload(imdb_person_id, c_name=actor_name)
                                if imdb_person_id
                                else None
                            )
                            actor_id = notion_helper.get_relation_id(
                                actor_name,
                                notion_helper.actor_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            actor_relations.append(actor_id)
                        if actor_relations:
                            movie["Actor"] = actor_relations

                    if cast_crew['directors'] and subject.get("directors"):
                        director_relations = []
                        douban_directors = subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                        for idx, douban_director in enumerate(douban_directors):
                            director_name = douban_director.get("name")
                            if not director_name:
                                continue
                            imdb_person_id = None
                            if idx < len(cast_crew['directors']):
                                imdb_person_id = (cast_crew['directors'][idx] or {}).get("id")
                            person_info = (
                                _build_person_info_payload(imdb_person_id, c_name=director_name)
                                if imdb_person_id
                                else None
                            )
                            director_id = notion_helper.get_relation_id(
                                director_name,
                                notion_helper.director_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            director_relations.append(director_id)
                        if director_relations:
                            movie["Director"] = director_relations
                else:
                    print(f"  使用豆瓣数据源获取演员/导演")
                    if subject.get("actors"):
                        actors = subject.get("actors")[0:MAX_ACTORS_RELATION]
                        movie["Actor"] = [
                            notion_helper.get_relation_id(
                                x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                            )
                            for x in actors
                        ]

                    if subject.get("directors"):
                        movie["Director"] = [
                            notion_helper.get_relation_id(
                                x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                            )
                            for x in subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                        ]
            else:
                # 外文电影：从IMDB获取Actor/Director，从豆瓣获取中文名
                if imdb_id:
                    print(f"  使用IMDB数据源获取演员/导演")
                    cast_crew = get_imdb_cast_and_crew(imdb_id)

                    # 添加演员（IMDB数据，包含详细信息和豆瓣中文名）
                    if cast_crew['actors']:
                        actor_relations = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            person_info = _build_person_info_payload(actor.get("id"), c_name=None)

                            actor_id = notion_helper.get_relation_id(
                                actor['name'],  # IMDB英文原名
                                notion_helper.actor_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            actor_relations.append(actor_id)
                        movie["Actor"] = _ensure_actor_relations(
                            actor_relations, subject, notion_helper, allow_douban_fallback=False
                        )
                    elif subject.get("actors"):
                        fallback_actor_names = _pick_relation_names(
                            subject.get("actors"),
                            MAX_ACTORS_RELATION,
                            prefer_latin=True,
                        )
                        if fallback_actor_names:
                            movie["Actor"] = [
                                notion_helper.get_relation_id(
                                    name, notion_helper.actor_database_id, USER_ICON_URL
                                )
                                for name in fallback_actor_names
                            ]

                    # 添加导演（IMDB数据，包含详细信息和豆瓣中文名）
                    if cast_crew['directors']:
                        director_relations = []
                        for idx, director in enumerate(cast_crew['directors']):
                            person_info = _build_person_info_payload(director.get("id"), c_name=None)

                            director_id = notion_helper.get_relation_id(
                                director['name'],  # IMDB英文原名
                                notion_helper.director_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            director_relations.append(director_id)
                        movie["Director"] = director_relations
                    elif subject.get("directors"):
                        fallback_director_names = _pick_relation_names(
                            subject.get("directors"),
                            MAX_DIRECTORS_RELATION,
                            prefer_latin=True,
                        )
                        if fallback_director_names:
                            movie["Director"] = [
                                notion_helper.get_relation_id(
                                    name, notion_helper.director_database_id, USER_ICON_URL
                                )
                                for name in fallback_director_names
                            ]
                else:
                    if not imdb_id:
                        print(f"  外文条目未获取到IMDB，跳过演员/导演写入，避免中文音译污染")
                    # 外文条目在无IMDB时不再回退豆瓣，避免写入“xxx·xxx”中文音译人名
                    if not movie.get("Director") and subject.get("directors"):
                        fallback_director_names = _pick_relation_names(
                            subject.get("directors"),
                            MAX_DIRECTORS_RELATION,
                            prefer_latin=True,
                        )
                        if fallback_director_names:
                            movie["Director"] = [
                                notion_helper.get_relation_id(
                                    name, notion_helper.director_database_id, USER_ICON_URL
                                )
                                for name in fallback_director_names
                            ]
            properties = utils.get_properties(movie, movie_properties_type_dict)
            notion_helper.get_date_relation(properties,create_time)

            duplicate_movie = None
            if movie.get("IMDB"):
                duplicate_movie = notion_movie_imdb_dict.get(movie.get("IMDB"))
            if not duplicate_movie:
                for unique_key in _build_movie_unique_keys(movie.get("Name"), movie.get("MovieName"), movie.get("Year")):
                    duplicate_movie = notion_movie_title_year_dict.get(unique_key)
                    if duplicate_movie:
                        break
            if duplicate_movie:
                print(f"  命中唯一校验，改为更新: {movie.get('Name')}")
                icon = get_icon(cover) if cover else None
                notion_helper.update_page(
                    page_id=duplicate_movie.get("page_id"),
                    properties=properties,
                    icon=icon,
                )
                notion_movie_dict[movie.get("DB_Url")] = duplicate_movie
                continue

            parent = {
                "database_id": notion_helper.movie_database_id,
                "type": "database_id",
            }
            created_page = notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover)
            )
            created_movie = {
                "Remark": movie.get("Remark"),
                "Status": movie.get("Status"),
                "Date": movie.get("Date"),
                "Rating": movie.get("Rating"),
                "Actor": movie.get("Actor"),
                "Director": movie.get("Director"),
                "IMDB": movie.get("IMDB"),
                "IMDB_Url": movie.get("IMDB_Url"),
                "Name": movie.get("Name"),
                "MovieName": movie.get("MovieName"),
                "Year": movie.get("Year"),
                "Season": movie.get("Season"),
                "Cover": movie.get("Cover"),
                "CoverSource": movie.get("CoverSource"),
                "CoverStatus": movie.get("CoverStatus"),
                "page_id": (created_page or {}).get("id"),
            }
            notion_movie_dict[movie.get("DB_Url")] = created_movie
            if created_movie.get("IMDB"):
                notion_movie_imdb_dict[created_movie.get("IMDB")] = created_movie
            for unique_key in _build_movie_unique_keys(created_movie.get("Name"), created_movie.get("MovieName"), created_movie.get("Year")):
                notion_movie_title_year_dict[unique_key] = created_movie

def get_imdb(link):
    """从豆瓣页面获取IMDB编号（豆瓣已不再显示IMDB信息）"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36'}
        response = requests.get(link, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, features="lxml")

        # 尝试从页面文本中查找IMDB编号
        page_text = response.text
        imdb_pattern = r'tt\d{7,8}'
        import re
        imdb_matches = re.findall(imdb_pattern, page_text)
        if imdb_matches:
            return imdb_matches[0]

        # 旧方法（可能不再有效）
        info = soup.find(id='info')
        if info:
            for span in info.find_all('span', {'class': 'pl'}):
                if span.string and 'IMDb:' == span.string:
                    imdb_id = span.next_sibling.string.strip()
                    return imdb_id
    except Exception as e:
        print(f"  从豆瓣获取IMDB编号失败: {str(e)[:50]}")
    return None

def _strip_season(title):
    """去掉标题中的季数后缀，如'黑镜 第三季'/'Succession Season 3'"""
    if not title:
        return title
    title = re.sub(r'\s*第\s*[零〇一二三四五六七八九十百两\d]{1,5}\s*季$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*Season\s*\d{1,2}$', '', title, flags=re.IGNORECASE)
    return title.strip()


def _extract_season_number(title):
    if not title:
        return None

    chinese_match = re.search(r'第\s*([零〇一二三四五六七八九十百两\d]{1,5})\s*季', title, flags=re.IGNORECASE)
    if chinese_match:
        return _parse_chinese_or_digit_number(chinese_match.group(1))

    english_match = re.search(r'Season\s*(\d{1,2})', title, flags=re.IGNORECASE)
    if english_match:
        return int(english_match.group(1))

    return None


def _parse_chinese_or_digit_number(value):
    value = value.strip()
    if value.isdigit():
        return int(value)

    value = value.replace("两", "二").replace("〇", "零")
    char_map = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

    if value == "十":
        return 10
    if "十" in value:
        parts = value.split("十")
        tens = char_map.get(parts[0], 1) if parts[0] else 1
        ones = char_map.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones

    total = 0
    for ch in value:
        if ch not in char_map:
            return None
        total = total * 10 + char_map[ch]
    return total if total > 0 else None


def _format_season_label(season_number):
    if not season_number:
        return None
    return f"Season {season_number}"


def _build_imdb_search_titles(douban_title):
    """构建 IMDB 搜索词：原中文标题 + 可能的英文别名。"""
    clean_title = _strip_season(douban_title).strip()
    titles = [clean_title]
    alias = _get_alias_title(clean_title)
    if alias:
        # 已知别名优先，降低中文直搜误匹配
        titles = [alias, clean_title]
    # 去重并保持顺序
    seen = set()
    result = []
    for t in titles:
        if not t or t in seen:
            continue
        seen.add(t)
        result.append(t)
    return result


def _build_imdb_search_candidates(douban_title, original_title=None, aliases=None):
    """合并豆瓣标题与原名，优先使用可识别的英文检索词。"""
    merged = []
    source_titles = [douban_title, original_title]
    if aliases and isinstance(aliases, list):
        source_titles.extend(aliases)
    for source_title in source_titles:
        for candidate in _build_imdb_search_titles(source_title or ""):
            if candidate and candidate not in merged:
                merged.append(candidate)
    return merged


def _lookup_year_for_imdb(douban_title, media_type, year):
    """TV 分季条目应匹配剧集主ID，避免用分季年份误导检索。"""
    if media_type == "tv" and _extract_season_number(douban_title):
        return None
    return year


def _get_alias_title(title):
    if not title:
        return None
    for key, alias in IMDB_TITLE_ALIAS_MAP.items():
        if key in title:
            return alias
    return None


def _normalize_full_title(title):
    """保留完整英文主副标题，只做空白与冒号格式规整。"""
    if not title:
        return title
    text = re.sub(r"\s+", " ", title).strip()
    text = re.sub(r"\s*:\s*", ": ", text)
    return text


def _normalize_title_key(text):
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", str(text)).strip().lower()
    normalized = normalized.replace("：", ":")
    return normalized


def _normalize_imdb_id(imdb_id):
    candidate = (imdb_id or "").strip()
    if not re.match(r"^tt\d{7,8}$", candidate):
        return None
    return candidate


def _is_existing_imdb_consistent(
    douban_title,
    imdb_id,
    original_title=None,
    alias_titles=None,
    expected_media_type=None,
    expected_year=None,
    douban_url=None,
):
    imdb_id = _normalize_imdb_id(imdb_id)
    if not imdb_id:
        return False
    if not _is_imdb_media_type_compatible(imdb_id, expected_media_type):
        return False
    imdb_info = get_imdb_info(imdb_id) or {}
    imdb_title = imdb_info.get("title")
    if not imdb_title:
        # Unknown title: keep existing value unless stronger evidence is found.
        return True
    title_consistent = _is_imdb_title_consistent(
        douban_title,
        imdb_title,
        original_title=original_title,
        alias_titles=alias_titles,
    )
    if not title_consistent:
        return False

    # Use TMDB as an additional validator for ambiguous same-name movie/tv items.
    if TMDB_API_KEY and expected_media_type in {"movie", "tv"}:
        tmdb_query_title = _strip_season(original_title or douban_title)
        tmdb_imdb_id, _, _ = search_tmdb_for_imdb(
            tmdb_query_title,
            expected_year,
            media_type=expected_media_type,
            douban_url=douban_url,
        )
        tmdb_imdb_id = _normalize_imdb_id(tmdb_imdb_id)
        if tmdb_imdb_id and tmdb_imdb_id != imdb_id:
            return False

    # For movies with explicit year, run one canonical re-search to avoid
    # same-title movie/tv cross-binding (e.g. identical franchise names).
    if expected_media_type == "movie" and expected_year and original_title and _has_latin(original_title):
        verified_imdb_id = search_imdb_by_title(
            _strip_season(original_title),
            expected_year,
            media_type="movie",
        )
        verified_imdb_id = _normalize_imdb_id(verified_imdb_id)
        if verified_imdb_id and verified_imdb_id != imdb_id:
            return False
    return True


def _extract_douban_id_from_url(url):
    if not url:
        return None
    match = re.search(r"/subject/(\d+)", str(url))
    if not match:
        return None
    return match.group(1)


def _extract_alias_titles(subject):
    aliases = []
    if not subject:
        return aliases
    aka = subject.get("aka")
    if isinstance(aka, list):
        for item in aka:
            text = str(item or "").strip()
            if text and text not in aliases:
                aliases.append(text)
    return aliases


def _extract_people_names(people, limit):
    names = []
    for item in (people or []):
        name = str((item or {}).get("name") or "").strip()
        if not name or name in names:
            continue
        names.append(name)
        if len(names) >= limit:
            break
    return names


def _pick_relation_names(people, limit, prefer_latin=False):
    names = _extract_people_names(people, limit=limit * 2 if limit else 10)
    if not names:
        return []
    if prefer_latin:
        latin_names = [x for x in names if _has_latin(x) and not _has_chinese(x)]
        if latin_names:
            return latin_names[:limit]
        return []
    return names[:limit]


def _fetch_douban_subject_detail(douban_url=None, subtype=None):
    douban_id = _extract_douban_id_from_url(douban_url)
    if not douban_id:
        return {}
    cache_key = f"{subtype or ''}:{douban_id}"
    if cache_key in DOUBAN_SUBJECT_DETAIL_CACHE:
        return DOUBAN_SUBJECT_DETAIL_CACHE.get(cache_key) or {}
    endpoints = []
    if subtype in {"movie", "tv"}:
        endpoints.append(f"https://{DOUBAN_API_HOST}/api/v2/{subtype}/{douban_id}")
    endpoints.append(f"https://{DOUBAN_API_HOST}/api/v2/subject/{douban_id}")
    for url in endpoints:
        try:
            response = requests.get(
                url,
                params={"apikey": DOUBAN_API_KEY},
                headers=headers,
                timeout=15,
            )
            if response.status_code != 200:
                continue
            data = response.json() or {}
            DOUBAN_SUBJECT_DETAIL_CACHE[cache_key] = data
            return data
        except Exception:
            continue
    DOUBAN_SUBJECT_DETAIL_CACHE[cache_key] = {}
    return {}


def _resolve_original_and_alias_titles(subject, douban_url=None, subtype=None):
    """补齐原名/别名，用于提升 IMDb/TMDB 检索命中率。"""
    original_title = str((subject or {}).get("original_title") or "").strip()
    aliases = _extract_alias_titles(subject)
    has_latin_candidate = _has_latin(original_title) or any(_has_latin(x) for x in aliases)
    if has_latin_candidate:
        return original_title or None, aliases

    detail = _fetch_douban_subject_detail(douban_url=douban_url, subtype=subtype)
    detail_original_title = str((detail or {}).get("original_title") or "").strip()
    if detail_original_title:
        original_title = detail_original_title
        if subject is not None:
            subject["original_title"] = detail_original_title
    detail_aliases = _extract_alias_titles(detail)
    for alias in detail_aliases:
        if alias not in aliases:
            aliases.append(alias)
    if subject is not None and detail_aliases:
        subject["aka"] = detail_aliases
    if subject is not None and not subject.get("actors") and detail.get("actors"):
        subject["actors"] = detail.get("actors")
    if subject is not None and not subject.get("directors") and detail.get("directors"):
        subject["directors"] = detail.get("directors")
    return original_title or None, aliases


def _get_tmdb_override(douban_title=None, douban_url=None):
    douban_id = _extract_douban_id_from_url(douban_url)
    if douban_id and TMDB_ID_OVERRIDE_MAP.get(douban_id):
        return TMDB_ID_OVERRIDE_MAP.get(douban_id)
    title_text = (douban_title or "").strip()
    if title_text and TMDB_ID_OVERRIDE_MAP.get(title_text):
        return TMDB_ID_OVERRIDE_MAP.get(title_text)
    normalized_title = _normalize_title_key(title_text)
    if normalized_title:
        for key, override in TMDB_ID_OVERRIDE_MAP.items():
            if _normalize_title_key(key) == normalized_title:
                return override
    return None


def _has_chinese(text):
    return bool(text) and re.search(r"[\u4e00-\u9fff]", str(text)) is not None


def _has_latin(text):
    return bool(text) and re.search(r"[A-Za-z]", str(text)) is not None


def _is_chinese_region_first(countries):
    if not countries:
        return False
    first_country = countries[0] if isinstance(countries, list) else str(countries).split()[0]
    chinese_regions = ["中国大陆", "中国香港", "中国台湾", "中国", "香港", "台湾", "China", "Hong Kong", "Taiwan"]
    return any(region in str(first_country) for region in chinese_regions)


def _should_force_foreign_by_imdb(douban_title, imdb_title, countries=None):
    """
    当豆瓣语言判定不稳定时，用 IMDb 标题兜底：
    - IMDb 标题明显是拉丁字母且非中文
    - 且与豆瓣标题不是同一字符串
    """
    if _is_chinese_region_first(countries):
        return False
    if not imdb_title:
        return False
    if not _has_latin(imdb_title) or _has_chinese(imdb_title):
        return False
    douban_key = _normalize_title_key(douban_title)
    imdb_key = _normalize_title_key(imdb_title)
    if not douban_key or not imdb_key:
        return False
    return douban_key != imdb_key


def _should_try_tmdb_fallback(is_chinese, countries=None, original_title=None, douban_title=None, douban_url=None):
    # Explicit override should always be attempted, even for Chinese titles.
    if _get_tmdb_override(douban_title=douban_title, douban_url=douban_url):
        return True
    if not TMDB_API_KEY:
        return False
    if not is_chinese:
        return True
    if countries and not _is_chinese_region_first(countries):
        return True
    if original_title and _has_latin(original_title) and not _has_chinese(original_title):
        return True
    return False


def _build_movie_unique_keys(name, movie_name, year):
    keys = []
    year_text = str(year or "").strip()
    for candidate in (name, movie_name):
        title_key = _normalize_title_key(candidate)
        if not title_key:
            continue
        keys.append(f"{title_key}|{year_text}" if year_text else title_key)
    # 去重并保持顺序
    seen = set()
    result = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _movie_record_quality(record):
    """Higher score means better candidate when same DB_Url has duplicate rows."""
    if not record:
        return -1
    score = 0
    if _normalize_imdb_id((record.get("IMDB") or "").strip()):
        score += 8
    if record.get("Actor"):
        score += 2
    if record.get("Director"):
        score += 2
    if record.get("Cover"):
        score += 1
    if record.get("MovieName"):
        score += 1
    if record.get("Name"):
        score += 1
    return score


def _choose_preferred_movie_record(records):
    if not records:
        return None
    best = None
    best_score = -1
    for item in records:
        s = _movie_record_quality(item)
        if s > best_score:
            best_score = s
            best = item
    return best


def _archive_duplicate_movie_pages(notion_helper, notion_movie_duplicates):
    archived_count = 0
    for db_url, records in (notion_movie_duplicates or {}).items():
        if not db_url or len(records) <= 1:
            continue
        preferred = _choose_preferred_movie_record(records)
        preferred_page_id = (preferred or {}).get("page_id")
        for record in records:
            page_id = (record or {}).get("page_id")
            if not page_id or page_id == preferred_page_id:
                continue
            notion_helper.archive_page(page_id)
            archived_count += 1
            print(f"归档重复条目: {db_url} -> {page_id}")
    return archived_count


def _parse_year_int(value):
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    matched = re.search(r"\b(\d{4})\b", text)
    if matched:
        return int(matched.group(1))
    return None


def _compact_series_title_key(text):
    normalized = _normalize_title_key(_strip_season(text or ""))
    if not normalized:
        return None
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def _inherit_series_imdb_from_existing_rows(
    douban_title,
    original_title,
    notion_movie_dict,
    target_year=None,
    alias_titles=None,
):
    """Season-level rows can inherit IMDb id from existing same-series parent rows."""
    candidate_exact_keys = set()
    candidate_compact_keys = set()
    for title in _build_imdb_search_candidates(douban_title, original_title, aliases=alias_titles):
        exact_key = _normalize_title_key(_strip_season(title))
        compact_key = _compact_series_title_key(title)
        if exact_key:
            candidate_exact_keys.add(exact_key)
        if compact_key:
            candidate_compact_keys.add(compact_key)
    if not candidate_exact_keys and not candidate_compact_keys:
        return None

    target_year_int = _parse_year_int(target_year)
    best = None
    best_score = -1
    for row in (notion_movie_dict or {}).values():
        imdb_id = _normalize_imdb_id((row or {}).get("IMDB"))
        if not imdb_id:
            continue
        if not _is_existing_imdb_consistent(
            douban_title,
            imdb_id,
            original_title=original_title,
            alias_titles=alias_titles,
            expected_media_type="tv",
            expected_year=target_year,
        ):
            continue

        row_exact_keys = {
            x for x in (
                _normalize_title_key(_strip_season((row or {}).get("Name") or "")),
                _normalize_title_key(_strip_season((row or {}).get("MovieName") or "")),
            ) if x
        }
        row_compact_keys = {
            x for x in (
                _compact_series_title_key((row or {}).get("Name") or ""),
                _compact_series_title_key((row or {}).get("MovieName") or ""),
            ) if x
        }
        exact_match = bool(candidate_exact_keys & row_exact_keys)
        compact_match = bool(candidate_compact_keys & row_compact_keys)
        if not exact_match and not compact_match:
            continue

        row_year_int = _parse_year_int((row or {}).get("Year"))
        if target_year_int and row_year_int and abs(target_year_int - row_year_int) > 3:
            continue

        score = 10
        if exact_match:
            score += 2
        if compact_match:
            score += 1
        season_value = str((row or {}).get("Season") or "")
        if season_value == "Season 1":
            score += 3
        elif not season_value:
            score += 1
        if target_year_int and row_year_int:
            score += max(0, 3 - abs(target_year_int - row_year_int))
        if score > best_score:
            best_score = score
            best = imdb_id
    return best


def _is_foreign_style_name(name, movie_name=None):
    if not name:
        return False
    if _has_latin(name) and not _has_chinese(name):
        return True
    if movie_name and _has_chinese(movie_name) and _has_latin(name):
        return True
    return False


def _build_person_info_payload(person_id, c_name=None):
    """Build person payload even when IMDb detail is partially unavailable."""
    if not person_id:
        return None
    person_info_data = get_imdb_person_info(person_id) or {}
    nation = get_person_nation_from_birthplace(person_info_data.get("birthplace"))
    return {
        "photo": person_info_data.get("photo"),
        "photo_source": person_info_data.get("photo_source", "IMDB") if person_info_data.get("photo") else None,
        "nation": nation,
        "imdb_id": person_id,
        "bio": person_info_data.get("bio"),
        "c_name": c_name,
    }


def _resolve_movie_alias_for_chinese(clean_douban_title, clean_original_title, imdb_info):
    """For Chinese rows keep Chinese Name, but try to fill MovieName with an alias/original title."""
    if clean_original_title and _normalize_title_key(clean_original_title) != _normalize_title_key(clean_douban_title):
        return _normalize_full_title(clean_original_title)
    imdb_title = (imdb_info or {}).get("title")
    if imdb_title and _normalize_title_key(imdb_title) != _normalize_title_key(clean_douban_title):
        return _normalize_full_title(_strip_season(imdb_title))
    return None


def _resolve_cover_from_sources(imdb_info, subject, current_cover=None, tmdb_cover=None):
    """Prefer IMDb poster, then TMDB, then keep current cover, finally fallback to Douban cover."""
    poster = (imdb_info or {}).get("poster")
    if poster:
        return poster, "IMDB", "Ok"
    if tmdb_cover:
        return tmdb_cover, "TMDB", "Ok"
    if current_cover:
        return current_cover, "Current", "Ok"
    douban_cover = ((subject or {}).get("pic") or {}).get("normal") or ((subject or {}).get("pic") or {}).get("large")
    if douban_cover:
        return douban_cover, "Douban", "Ok"
    return None, None, "Missing"


def _relation_names_are_chinese(notion_helper, relation_ids):
    if not relation_ids:
        return True
    for relation in relation_ids:
        relation_id = relation.get("id") if isinstance(relation, dict) else relation
        if not relation_id:
            continue
        if relation_id in RELATION_NAME_CACHE:
            relation_name = RELATION_NAME_CACHE.get(relation_id)
        else:
            try:
                relation_page = notion_helper.client.pages.retrieve(page_id=relation_id)
                relation_name = utils.get_property_value((relation_page.get("properties") or {}).get("Name") or {})
            except Exception:
                relation_name = None
            RELATION_NAME_CACHE[relation_id] = relation_name
        if relation_name and _has_latin(relation_name) and not _has_chinese(relation_name):
            return False
    return True


def _ensure_actor_relations(actor_ids, subject, notion_helper, allow_douban_fallback=True):
    """补齐演员关系数量；可选择是否允许用豆瓣演员兜底。"""
    if not isinstance(actor_ids, list):
        actor_ids = []
    if len(actor_ids) >= MAX_ACTORS_RELATION or not allow_douban_fallback:
        return actor_ids

    for actor in (subject.get("actors") or []):
        if len(actor_ids) >= MAX_ACTORS_RELATION:
            break
        name = actor.get("name")
        if not name:
            continue
        rel_id = notion_helper.get_relation_id(name, notion_helper.actor_database_id, USER_ICON_URL)
        if rel_id and rel_id not in actor_ids:
            actor_ids.append(rel_id)
    return actor_ids


def _search_imdb_suggest(title, year=None, media_type="movie"):
    """优先走 IMDB suggest 接口，英文检索更稳定。"""
    if not title:
        return None
    # IMDb suggest 对纯中文 query 噪声极高，容易误命中无关条目。
    if _has_chinese(title) and not _has_latin(title):
        return None
    try:
        first_char = title[0].lower() if title[0].isascii() else "x"
        suggest_url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{requests.utils.quote(title)}.json"
        response = requests.get(
            suggest_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
            timeout=15,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        candidates = data.get("d", [])
        if not candidates:
            return None

        year_int = int(year) if year and str(year).isdigit() else None
        best_id = None
        best_score = -999
        for c in candidates:
            imdb_id = c.get("id")
            if not imdb_id or not imdb_id.startswith("tt"):
                continue
            score = 0
            kind = (c.get("q") or "").lower()
            candidate_year = c.get("y")
            if media_type == "tv":
                if not ("tv" in kind or "mini-series" in kind or "series" in kind):
                    continue
                score += 3
            elif media_type == "movie":
                if "tv" in kind or "series" in kind or "episode" in kind:
                    continue
            if year_int and isinstance(candidate_year, int) and abs(candidate_year - year_int) > 2:
                continue
            if year_int and isinstance(candidate_year, int):
                if candidate_year == year_int:
                    score += 4
                elif abs(candidate_year - year_int) <= 1:
                    score += 2
            label = (c.get("l") or "").lower()
            if title.lower() in label:
                score += 1
            if score > best_score:
                best_score = score
                best_id = imdb_id
        return best_id
    except Exception:
        return None


def _normalize_ascii_title(text):
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _is_imdb_title_consistent(douban_title, imdb_title, original_title=None, alias_titles=None):
    """判断既有 IMDB 条目是否与豆瓣标题语义一致，避免沿用错误的旧IMDB。"""
    if not imdb_title:
        return False
    imdb_norm = _normalize_ascii_title(imdb_title)
    if not imdb_norm:
        return False

    imdb_words = set(re.findall(r"[a-z0-9]+", imdb_title.lower()))
    for candidate in _build_imdb_search_candidates(douban_title, original_title, aliases=alias_titles):
        candidate_norm = _normalize_ascii_title(candidate)
        if not candidate_norm:
            continue
        if candidate_norm in imdb_norm or imdb_norm in candidate_norm:
            shorter = min(len(candidate_norm), len(imdb_norm))
            longer = max(len(candidate_norm), len(imdb_norm))
            if longer > 0 and (shorter / longer) >= 0.8:
                return True

        candidate_words = set(re.findall(r"[a-z0-9]+", candidate.lower()))
        if len(candidate_words) < 3:
            continue
        overlap = candidate_words & imdb_words
        extra_words = imdb_words - candidate_words
        if (
            len(overlap) >= 3
            and (len(overlap) / len(candidate_words)) >= 0.75
            and len(extra_words) <= 2
        ):
            return True
    return False


def search_imdb_by_title(title, year=None, media_type="movie"):
    """通过电影/剧集名称在IMDB搜索
    media_type: "movie" 搜索电影(ttype=ft)，"tv" 搜索剧集(ttype=tv)
    """
    cache_key = (title or "", str(year or ""), media_type or "movie")
    if cache_key in IMDB_SEARCH_CACHE:
        return IMDB_SEARCH_CACHE[cache_key]
    try:
        year_int = int(year) if year and str(year).isdigit() else None
        suggest_id = _search_imdb_suggest(title, year=year, media_type=media_type)
        if suggest_id:
            if not _is_imdb_media_type_compatible(suggest_id, media_type):
                print(f"  IMDB suggest候选类型不匹配，忽略: {suggest_id}")
                suggest_id = None
        if suggest_id:
            suggest_info = get_imdb_info(suggest_id)
            suggest_title = (suggest_info or {}).get("title")
            suggest_year = (suggest_info or {}).get("year")
            if year_int and suggest_year and str(suggest_year).isdigit() and abs(int(suggest_year) - year_int) > 2:
                print(f"  IMDB suggest候选年份偏差过大，忽略: {suggest_id} ({suggest_year})")
                suggest_id = None
        if suggest_id:
            if _is_imdb_title_consistent(title, suggest_title):
                print(f"  通过IMDB suggest找到: {suggest_id} ({title})")
                IMDB_SEARCH_CACHE[cache_key] = suggest_id
                return suggest_id
            print(f"  IMDB suggest候选不一致，忽略: {suggest_id} ({suggest_title})")

        # 构建搜索URL
        search_query = title
        if year:
            search_query += f" {year}"

        ttype = "tv" if media_type == "tv" else "ft"
        search_url = f"https://www.imdb.com/find?q={requests.utils.quote(search_query)}&s=tt&ttype={ttype}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        response = requests.get(search_url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 取前几条候选，做标题一致性校验，避免命中错误作品
            results = soup.find_all('a', href=lambda x: x and '/title/tt' in x)
            for result in results[:8]:
                href = result.get('href')
                imdb_id_match = re.search(r'/(tt\d+)/', href)
                if not imdb_id_match:
                    continue
                imdb_id = imdb_id_match.group(1)
                if not _is_imdb_media_type_compatible(imdb_id, media_type):
                    continue
                imdb_info = get_imdb_info(imdb_id)
                imdb_title = (imdb_info or {}).get("title")
                imdb_year = (imdb_info or {}).get("year")
                if year_int and imdb_year and str(imdb_year).isdigit() and abs(int(imdb_year) - year_int) > 2:
                    continue
                if _is_imdb_title_consistent(title, imdb_title):
                    print(f"  通过IMDB搜索找到: {imdb_id}")
                    IMDB_SEARCH_CACHE[cache_key] = imdb_id
                    return imdb_id
    except Exception as e:
        print(f"  IMDB搜索失败: {str(e)[:50]}")
    IMDB_SEARCH_CACHE[cache_key] = None
    return None


def _tmdb_detail_by_id(tmdb_id, search_type):
    if not tmdb_id:
        return None, None, None
    detail_url = f"https://api.themoviedb.org/3/{search_type}/{tmdb_id}"
    detail_resp = requests.get(
        detail_url,
        params={"api_key": TMDB_API_KEY, "append_to_response": "external_ids", "language": "en-US"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    if detail_resp.status_code != 200:
        return None, None, None
    detail = detail_resp.json()
    imdb_id = _normalize_imdb_id((detail.get("external_ids") or {}).get("imdb_id"))
    original_title = detail.get("original_name") if search_type == "tv" else detail.get("original_title")
    poster_path = detail.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
    return imdb_id, original_title, poster_url


def search_tmdb_for_imdb(title, year=None, media_type="movie", douban_url=None):
    """TMDB fallback: find IMDb id/original title/poster by title or configured TMDB id."""
    if not title:
        return None, None, None

    douban_id = _extract_douban_id_from_url(douban_url)
    cache_key = (title or "", str(year or ""), media_type or "movie", douban_id or "")
    if cache_key in TMDB_SEARCH_CACHE:
        return TMDB_SEARCH_CACHE[cache_key]

    try:
        is_tv = media_type == "tv"
        search_type = "tv" if is_tv else "movie"
        override = _get_tmdb_override(douban_title=title, douban_url=douban_url)

        if override:
            override_id = (override.get("id") or "").strip()
            override_type = (override.get("type") or search_type).strip().lower()
            if override_type not in {"movie", "tv"}:
                override_type = search_type

            override_imdb_id = _normalize_imdb_id(override.get("imdb_id"))
            override_original_title = _normalize_full_title((override.get("original_title") or "").strip()) or None
            override_poster = (override.get("poster_url") or "").strip() or None
            if override_poster and override_poster.startswith("/"):
                override_poster = f"https://image.tmdb.org/t/p/w500{override_poster}"

            if override_id and TMDB_API_KEY:
                imdb_id, original_title, poster_url = _tmdb_detail_by_id(override_id, override_type)
                result_tuple = (
                    imdb_id or override_imdb_id,
                    original_title or override_original_title,
                    poster_url or override_poster,
                )
                TMDB_SEARCH_CACHE[cache_key] = result_tuple
                return result_tuple

            result_tuple = (override_imdb_id, override_original_title, override_poster)
            TMDB_SEARCH_CACHE[cache_key] = result_tuple
            return result_tuple

        if not TMDB_API_KEY:
            TMDB_SEARCH_CACHE[cache_key] = (None, None, None)
            return None, None, None

        search_url = f"https://api.themoviedb.org/3/search/{search_type}"
        params = {
            "api_key": TMDB_API_KEY,
            "query": title,
            "language": "zh-CN",
            "include_adult": "false",
        }
        if year and str(year).isdigit():
            if is_tv:
                params["first_air_date_year"] = str(year)
            else:
                params["year"] = str(year)

        response = requests.get(search_url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if response.status_code != 200:
            TMDB_SEARCH_CACHE[cache_key] = (None, None, None)
            return None, None, None
        results = response.json().get("results") or []
        if not results:
            TMDB_SEARCH_CACHE[cache_key] = (None, None, None)
            return None, None, None

        # Prefer exact year when possible.
        chosen = results[0]
        if year and str(year).isdigit():
            y = str(year)
            for item in results[:5]:
                date_value = item.get("first_air_date") if is_tv else item.get("release_date")
                if date_value and str(date_value).startswith(y):
                    chosen = item
                    break

        tmdb_id = chosen.get("id")
        if not tmdb_id:
            TMDB_SEARCH_CACHE[cache_key] = (None, None, None)
            return None, None, None
        result_tuple = _tmdb_detail_by_id(tmdb_id, search_type)
        TMDB_SEARCH_CACHE[cache_key] = result_tuple
        return result_tuple
    except Exception:
        TMDB_SEARCH_CACHE[cache_key] = (None, None, None)
        return None, None, None


def _build_tmdb_profile_url(profile_path):
    if not profile_path:
        return None
    return f"https://image.tmdb.org/t/p/w500{profile_path}"


def _get_imdb_media_type_via_tmdb(imdb_id):
    imdb_id = _normalize_imdb_id(imdb_id)
    if not imdb_id:
        return None
    if imdb_id in IMDB_MEDIA_TYPE_CACHE:
        return IMDB_MEDIA_TYPE_CACHE.get(imdb_id)
    if not TMDB_API_KEY:
        IMDB_MEDIA_TYPE_CACHE[imdb_id] = None
        return None
    try:
        url = f"https://api.themoviedb.org/3/find/{imdb_id}"
        response = requests.get(
            url,
            params={
                "api_key": TMDB_API_KEY,
                "external_source": "imdb_id",
                "language": "en-US",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if response.status_code != 200:
            IMDB_MEDIA_TYPE_CACHE[imdb_id] = None
            return None
        payload = response.json() or {}
        movie_results = payload.get("movie_results") or []
        tv_results = payload.get("tv_results") or []
        media_type = None
        if movie_results and not tv_results:
            media_type = "movie"
        elif tv_results and not movie_results:
            media_type = "tv"
        IMDB_MEDIA_TYPE_CACHE[imdb_id] = media_type
        return media_type
    except Exception:
        IMDB_MEDIA_TYPE_CACHE[imdb_id] = None
        return None


def _is_imdb_media_type_compatible(imdb_id, expected_media_type):
    if expected_media_type not in {"movie", "tv"}:
        return True
    resolved_media_type = _get_imdb_media_type_via_tmdb(imdb_id)
    if not resolved_media_type:
        return True
    return resolved_media_type == expected_media_type


def get_tmdb_person_photo_by_imdb_id(imdb_person_id):
    """通过 TMDB find + IMDb 人物ID 获取头像（高置信度兜底）。"""
    if not TMDB_API_KEY or not imdb_person_id:
        return None
    try:
        url = f"https://api.themoviedb.org/3/find/{imdb_person_id}"
        response = requests.get(
            url,
            params={
                "api_key": TMDB_API_KEY,
                "external_source": "imdb_id",
                "language": "en-US",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        person_results = response.json().get("person_results") or []
        if not person_results:
            return None
        profile_path = person_results[0].get("profile_path")
        photo_url = _build_tmdb_profile_url(profile_path)
        if photo_url and _is_valid_image_url(photo_url):
            return photo_url
    except Exception:
        return None
    return None


def get_imdb_person_info(person_id):
    """从IMDB获取演员/导演详细信息"""
    if not person_id:
        return None
    if person_id in IMDB_PERSON_CACHE:
        return IMDB_PERSON_CACHE[person_id]

    result = {
        'name': None,
        'photo': None,
        'photo_source': None,
        'bio': None,
        'birthplace': None  # 可以从中推断国籍
    }

    try:
        url = f"https://www.imdb.com/name/{person_id}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 从JSON-LD获取信息
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        if 'name' in data and not result['name']:
                            result['name'] = data['name']
                        if 'image' in data and not result['photo'] and _is_valid_image_url(data['image']):
                            result['photo'] = data['image']
                            result['photo_source'] = 'IMDB'
                        if 'birthPlace' in data:
                            # birthPlace可能是字符串或对象
                            birthplace = data['birthPlace']
                            if isinstance(birthplace, dict):
                                result['birthplace'] = birthplace.get('name', '')
                            else:
                                result['birthplace'] = str(birthplace)
                except:
                    continue

            # 查找照片
            if not result['photo']:
                img = soup.find('img', {'class': lambda x: x and 'ipc-image' in x})
                if img and img.get('src'):
                    photo_url = img['src']
                    if '@._' in photo_url:
                        photo_url = photo_url.split('@._')[0] + '@.jpg'
                    if _is_valid_image_url(photo_url):
                        result['photo'] = photo_url
                        result['photo_source'] = 'IMDB'

            # 查找姓名（如果JSON-LD没有）
            if not result['name']:
                # 尝试从页面标题或h1标签获取
                title_tag = soup.find('h1')
                if title_tag:
                    result['name'] = title_tag.get_text().strip()

    except Exception as e:
        print(f"  获取人物信息失败 ({person_id}): {str(e)[:50]}")

    if result.get('photo') and not _is_valid_image_url(result.get('photo')):
        result['photo'] = None
        result['photo_source'] = None
    if not result.get('photo'):
        tmdb_photo = get_tmdb_person_photo_by_imdb_id(person_id)
        if tmdb_photo:
            result['photo'] = tmdb_photo
            result['photo_source'] = 'TMDB'

    final_result = result if (result['name'] or result['photo']) else None
    IMDB_PERSON_CACHE[person_id] = final_result
    return final_result

def get_person_nation_from_birthplace(birthplace):
    """从出生地推断国籍"""
    if not birthplace:
        return None

    birthplace_lower = birthplace.lower()

    # 国家关键词映射
    nation_keywords = {
        'USA': ['usa', 'united states', 'california', 'new york', 'texas', 'florida'],
        'UK': ['uk', 'united kingdom', 'england', 'london', 'scotland', 'wales'],
        'China': ['china', 'beijing', 'shanghai', 'hong kong', 'taiwan'],
        'Japan': ['japan', 'tokyo', 'osaka'],
        'Korea': ['korea', 'seoul'],
        'France': ['france', 'paris'],
        'Germany': ['germany', 'berlin'],
        'Canada': ['canada', 'toronto', 'vancouver'],
        'Australia': ['australia', 'sydney', 'melbourne'],
        'India': ['india', 'mumbai', 'delhi'],
    }

    for nation, keywords in nation_keywords.items():
        for keyword in keywords:
            if keyword in birthplace_lower:
                return nation

    return None

def get_imdb_cast_and_crew(imdb_id):
    """从IMDB获取演员和导演列表，优先使用JSON-LD，回退到fullcredits页面"""
    if not imdb_id:
        return {'actors': [], 'directors': []}
    if imdb_id in IMDB_CAST_CREW_CACHE:
        return IMDB_CAST_CREW_CACHE[imdb_id]
    result = {
        'actors': [],
        'directors': []
    }

    try:
        # 方法1：从IMDB主页的JSON-LD获取（最可靠）
        url = f"https://www.imdb.com/title/{imdb_id}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 从JSON-LD提取演员和导演
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                try:
                    data = json.loads(script.string)
                    if not isinstance(data, dict):
                        continue

                    # 导演
                    directors = data.get('director', [])
                    if not directors:
                        # TV 条目常见 creator 字段，无 director 字段。
                        directors = data.get('creator', [])
                    if isinstance(directors, dict):
                        directors = [directors]
                    for d in directors:
                        if isinstance(d, dict) and d.get('name'):
                            url_part = d.get('url', '')
                            person_id_match = re.search(r'/name/(nm\d+)', url_part)
                            person_id = person_id_match.group(1) if person_id_match else None
                            if len(result['directors']) < MAX_DIRECTORS_RELATION:
                                result['directors'].append({
                                    'name': d['name'],
                                    'id': person_id
                                })

                    # 演员
                    actors = data.get('actor', [])
                    if isinstance(actors, dict):
                        actors = [actors]
                    for a in actors:
                        if isinstance(a, dict) and a.get('name'):
                            url_part = a.get('url', '')
                            person_id_match = re.search(r'/name/(nm\d+)', url_part)
                            person_id = person_id_match.group(1) if person_id_match else None
                            if len(result['actors']) < MAX_ACTORS_RELATION:
                                result['actors'].append({
                                    'name': a['name'],
                                    'id': person_id
                                })

                    if result['actors'] or result['directors']:
                        break
                except:
                    continue

        if result['actors'] or result['directors']:
            # 如果JSON-LD数量不足，则继续用fullcredits补齐
            if len(result['actors']) >= MAX_ACTORS_RELATION and len(result['directors']) >= MAX_DIRECTORS_RELATION:
                print(f"  从IMDB获取到 {len(result['actors'])} 个演员，{len(result['directors'])} 个导演")
                return result

        # 方法2：回退到fullcredits页面（备用）
        url = f"https://www.imdb.com/title/{imdb_id}/fullcredits"
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 先记录已有ID，再从fullcredits补齐
            seen_ids = set()
            for x in result['actors']:
                if x.get('id'):
                    seen_ids.add(x['id'])
            for x in result['directors']:
                if x.get('id'):
                    seen_ids.add(x['id'])

            # 先查找导演section
            for h4 in soup.find_all('h4', id=True):
                section_id = h4.get('id', '')
                if 'direct' in section_id.lower() or 'direct' in h4.get_text().lower():
                    table = h4.find_next('table')
                    if table:
                        for link in table.find_all('a', href=lambda x: x and '/name/nm' in x):
                            name = link.get_text().strip()
                            person_id_match = re.search(r'/name/(nm\d+)', link.get('href', ''))
                            if name and person_id_match and len(result['directors']) < MAX_DIRECTORS_RELATION:
                                pid = person_id_match.group(1)
                                if pid not in seen_ids:
                                    result['directors'].append({'name': name, 'id': pid})
                                    seen_ids.add(pid)

            # 再查找演员section (cast_list table)
            cast_table = soup.find('table', {'class': 'cast_list'})
            if not cast_table:
                cast_table = soup.find('div', id='cast')

            if cast_table:
                for link in cast_table.find_all('a', href=lambda x: x and '/name/nm' in x):
                    name = link.get_text().strip()
                    if not name or len(name) < 2:
                        continue
                    person_id_match = re.search(r'/name/(nm\d+)', link.get('href', ''))
                    if person_id_match and len(result['actors']) < MAX_ACTORS_RELATION:
                        pid = person_id_match.group(1)
                        if pid not in seen_ids:
                            result['actors'].append({'name': name, 'id': pid})
                            seen_ids.add(pid)

        print(f"  从IMDB获取到 {len(result['actors'])} 个演员，{len(result['directors'])} 个导演")

    except Exception as e:
        print(f"  获取演职人员失败: {str(e)[:50]}")

    IMDB_CAST_CREW_CACHE[imdb_id] = result
    return result

def get_imdb_info(imdb_id):
    """从IMDB获取电影信息（海报、原名、评分）"""
    if not imdb_id:
        return None
    if imdb_id in IMDB_INFO_CACHE:
        return IMDB_INFO_CACHE[imdb_id]

    result = {
        'poster': None,
        'title': None,
        'rating': None,
        'year': None,
    }

    try:
        url = f"https://www.imdb.com/title/{imdb_id}/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 获取海报
            poster = soup.find('img', {'class': lambda x: x and 'ipc-image' in x})
            if poster and poster.get('src'):
                poster_url = poster['src']
                if '@._' in poster_url:
                    poster_url = poster_url.split('@._')[0] + '@.jpg'
                if _is_valid_image_url(poster_url):
                    result['poster'] = poster_url

            # 标题：优先从 h1 获取（IMDB h1 显示英文/本地化标题，JSON-LD 可能返回原语言罗马化）
            title_tag = soup.find('h1')
            if title_tag:
                result['title'] = title_tag.get_text().strip()

            # 从JSON-LD结构化数据中获取评分和海报
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        # 如果h1没取到标题，用JSON-LD兜底
                        if 'name' in data and not result['title']:
                            result['title'] = data['name']
                        # 获取评分
                        if 'aggregateRating' in data:
                            rating_value = data['aggregateRating'].get('ratingValue')
                            if rating_value:
                                result['rating'] = float(rating_value)
                        # 获取年份
                        date_published = str(data.get("datePublished") or "").strip()
                        if date_published:
                            year_match = re.match(r"^(\d{4})", date_published)
                            if year_match:
                                result['year'] = year_match.group(1)
                        # 获取海报
                        if 'image' in data and not result['poster'] and _is_valid_image_url(data['image']):
                            result['poster'] = data['image']
                except:
                    continue

            if result['poster'] or result['title'] or result['rating']:
                info_str = []
                if result['title']:
                    info_str.append(f"原名: {result['title']}")
                if result['rating']:
                    info_str.append(f"评分: {result['rating']}")
                if result['year']:
                    info_str.append(f"年份: {result['year']}")
                if result['poster']:
                    info_str.append("海报")
                print(f"  从IMDB获取成功 ({imdb_id}): {', '.join(info_str)}")

    except Exception as e:
        print(f"  从IMDB获取信息失败 ({imdb_id}): {str(e)[:50]}")

    final_result = result if (result['poster'] or result['title'] or result['rating']) else None
    IMDB_INFO_CACHE[imdb_id] = final_result
    return final_result

def get_goodreads_cover(title, author=None, isbn=None):
    """从Goodreads获取书籍封面（如果Goodreads不可用会返回None）"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }

        # 优先使用ISBN搜索
        if isbn:
            search_url = f"https://www.goodreads.com/search?q={isbn}"
        else:
            # 使用书名和作者搜索
            search_query = title
            if author:
                search_query += f" {author}"
            search_url = f"https://www.goodreads.com/search?q={requests.utils.quote(search_query)}"

        # 增加超时和重试
        for attempt in range(2):
            try:
                response = requests.get(search_url, headers=headers, timeout=20, allow_redirects=True)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, features="lxml")

                    # 查找第一个搜索结果的封面
                    cover_img = soup.find('img', {'class': lambda x: x and 'bookCover' in str(x)})
                    if cover_img and cover_img.get('src'):
                        cover_url = cover_img['src']
                        # Goodreads的图片URL优化
                        if cover_url and 'nophoto' not in cover_url:
                            # 替换为更大的图片尺寸
                            cover_url = cover_url.replace('._SX98_', '._SX318_').replace('._SY160_', '')
                            print(f"从Goodreads获取封面成功: {title}")
                            return cover_url
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    print(f"  Goodreads请求超时，重试中...")
                    continue
                else:
                    print(f"  Goodreads连接超时，跳过")
                    break
            except Exception as e:
                print(f"  Goodreads请求出错: {str(e)[:50]}")
                break
    except Exception as e:
        print(f"从Goodreads获取封面失败 ({title}): {str(e)[:100]}")
    return None


def _is_valid_image_url(url):
    if not url:
        return False
    if url in COVER_URL_VALIDITY_CACHE:
        return COVER_URL_VALIDITY_CACHE[url]
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
            allow_redirects=True,
            stream=True,
        )
        content_type = (response.headers.get("Content-Type") or "").lower()
        is_valid = response.status_code == 200 and ("image/" in content_type)
    except Exception:
        is_valid = False
    COVER_URL_VALIDITY_CACHE[url] = is_valid
    return is_valid


def _to_webp_variant(url):
    if not url or url.endswith(".webp"):
        return url
    if "." not in url.rsplit("/", 1)[-1]:
        return url
    return url.rsplit(".", 1)[0] + ".webp"


def _pick_first_valid_cover(candidates):
    for candidate in candidates:
        if candidate and _is_valid_image_url(candidate):
            return candidate
    return None


def _get_douban_book_cover(subject):
    pic = subject.get("pic") or {}
    candidates = []
    for key in ("large", "normal", "small"):
        url = pic.get(key)
        if not url:
            continue
        # 先试原链接，再试webp变体，兼容豆瓣图片迁移
        candidates.append(url)
        webp_url = _to_webp_variant(url)
        if webp_url != url:
            candidates.append(webp_url)
    return _pick_first_valid_cover(candidates)


def _get_openlibrary_cover(isbn):
    if not isbn:
        return None
    candidates = [
        f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",
        f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false",
    ]
    return _pick_first_valid_cover(candidates)


def _get_openlibrary_author_photo(author_name):
    if not author_name:
        return None
    if author_name in AUTHOR_PHOTO_CACHE:
        return AUTHOR_PHOTO_CACHE[author_name]
    try:
        response = requests.get(
            "https://openlibrary.org/search/authors.json",
            params={"q": author_name},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if response.status_code != 200:
            AUTHOR_PHOTO_CACHE[author_name] = None
            return None
        for doc in (response.json().get("docs") or []):
            author_key = doc.get("key")
            if not author_key:
                continue
            author_id = author_key.strip("/").split("/")[-1]
            detail = requests.get(
                f"https://openlibrary.org/authors/{author_id}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if detail.status_code != 200:
                continue
            for photo_id in (detail.json().get("photos") or []):
                url = f"https://covers.openlibrary.org/a/id/{photo_id}-L.jpg?default=false"
                if _is_valid_image_url(url):
                    AUTHOR_PHOTO_CACHE[author_name] = url
                    return url
    except Exception:
        pass
    AUTHOR_PHOTO_CACHE[author_name] = None
    return None


def _get_book_cover(subject, title):
    isbn = subject.get("isbn")
    authors = subject.get("author") or []
    author_name = authors[0] if authors else None
    goodreads_cover = get_goodreads_cover(title, author=author_name, isbn=isbn)
    if _is_valid_image_url(goodreads_cover):
        return goodreads_cover, "Goodreads"
    douban_cover = _get_douban_book_cover(subject)
    if douban_cover:
        return douban_cover, "Douban"
    return _get_openlibrary_cover(isbn), "OpenLibrary"


def _extract_book_year(subject):
    for date_str in subject.get("pubdate") or []:
        year_match = re.search(r"\d{4}", date_str)
        if year_match:
            return year_match.group()
    return None


def _extract_publisher_list(subject):
    press = []
    for item in subject.get("press") or []:
        press.extend(x.strip() for x in str(item).split(",") if x.strip())
    return press[0:MAX_PUBLISHERS_MULTI_SELECT]


def _normalize_relation_ids(value):
    if not isinstance(value, list):
        return []
    ids = []
    for item in value:
        if isinstance(item, dict):
            item_id = item.get("id")
            if item_id:
                ids.append(item_id)
        elif item:
            ids.append(item)
    return sorted(ids)


def _normalize_multi_select_names(value):
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                names.append(name)
        elif item:
            names.append(item)
    return sorted(names)


def insert_book(douban_name, notion_helper):
    notion_books = notion_helper.query_all(database_id=notion_helper.book_database_id)
    notion_book_dict = {}
    for i in notion_books:
        book = {}
        for key, value in i.get("properties").items():
            book[key] = utils.get_property_value(value)
        db_url = book.get("DB_Url") or book.get("Url")
        notion_book_dict[db_url] = {
            "Name": book.get("Name"),
            "Remark": book.get("Remark"),
            "Status": book.get("Status"),
            "Date": book.get("Date"),
            "Rating": book.get("Rating"),
            "Cover": book.get("Cover"),
            "CoverStatus": book.get("CoverStatus"),
            "CoverSource": book.get("CoverSource"),
            "CoverCheckedAt": book.get("CoverCheckedAt"),
            "ISBN": book.get("ISBN"),
            "ISBN_13": book.get("ISBN_13"),
            "GD_Url": book.get("GD_Url"),
            "Intro": book.get("Intro"),
            "DoubanRating": book.get("DoubanRating"),
            "Raters": book.get("Raters"),
            "Year": book.get("Year"),
            "Author": _normalize_relation_ids(book.get("Author")),
            "Category": _normalize_relation_ids(book.get("Category")),
            "Publisher": _normalize_multi_select_names(book.get("Publisher")),
            "page_id": i.get("id"),
        }
    print(f"notion {len(notion_book_dict)}")
    results = []
    for i in book_status.keys():
        results.extend(fetch_subjects(douban_name, "book", i))
    for result in results:
        book = {}
        if not result:
            continue
        subject = result.get("subject")
        create_time = pendulum.parse(result.get("create_time"), tz=utils.tz)
        create_time = create_time.replace(second=0)

        book["Name"] = subject.get("title")
        book["Date"] = create_time.int_timestamp
        book["DB_Url"] = subject.get("url")
        book["Status"] = book_status.get(result.get("status"))
        book_cover, book_cover_source = _get_book_cover(subject, book.get("Name"))
        book["Cover"] = book_cover
        book["CoverSource"] = book_cover_source if book_cover else None
        book["CoverStatus"] = "Ok" if book_cover else "Missing"
        book["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp
        book["ISBN"] = subject.get("isbn")
        book["ISBN_13"] = subject.get("isbn13") or subject.get("isbn")
        book["Intro"] = subject.get("intro")
        book["Publisher"] = _extract_publisher_list(subject)
        if subject.get("tags"):
            book["Category"] = [
                notion_helper.get_relation_id(x, notion_helper.category_database_id, TAG_ICON_URL)
                for x in subject.get("tags")[0:MAX_CATEGORIES_RELATION]
            ]
        if subject.get("author"):
            author_ids = []
            for author_name in subject.get("author")[0:MAX_AUTHORS_RELATION]:
                author_photo = _get_openlibrary_author_photo(author_name)
                person_info = {"photo": author_photo, "photo_source": "OpenLibrary"} if author_photo else {"photo_source": "OpenLibrary"}
                author_ids.append(
                    notion_helper.get_relation_id(
                        author_name, notion_helper.author_database_id, USER_ICON_URL, {}, person_info
                    )
                )
            book["Author"] = author_ids
        if result.get("rating"):
            book["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["Remark"] = result.get("comment")
        if subject.get("rating"):
            book["DoubanRating"] = subject.get("rating").get("value", 0)
            book["Raters"] = subject.get("rating").get("count", 0)
        year = _extract_book_year(subject)
        if year:
            book["Year"] = year

        existing_book = notion_book_dict.get(book.get("DB_Url"))
        if existing_book:
            # 新封面抓取失败时保留旧封面，避免把已有封面覆盖成空
            if not book.get("Cover") and existing_book.get("Cover"):
                book["Cover"] = existing_book.get("Cover")
            needs_update = (
                existing_book.get("Cover") != book.get("Cover")
                or existing_book.get("Date") != book.get("Date")
                or existing_book.get("Remark") != book.get("Remark")
                or existing_book.get("Status") != book.get("Status")
                or existing_book.get("Rating") != book.get("Rating")
                or existing_book.get("ISBN") != book.get("ISBN")
                or existing_book.get("ISBN_13") != book.get("ISBN_13")
                or existing_book.get("GD_Url") != book.get("GD_Url")
                or existing_book.get("Intro") != book.get("Intro")
                or existing_book.get("DoubanRating") != book.get("DoubanRating")
                or existing_book.get("Raters") != book.get("Raters")
                or existing_book.get("Year") != book.get("Year")
                or existing_book.get("CoverStatus") != book.get("CoverStatus")
                or existing_book.get("CoverSource") != book.get("CoverSource")
                or existing_book.get("Author") != _normalize_relation_ids(book.get("Author"))
                or existing_book.get("Category") != _normalize_relation_ids(book.get("Category"))
                or existing_book.get("Publisher") != _normalize_multi_select_names(book.get("Publisher"))
            )
            if needs_update:
                print(f"更新{book.get('Name')}")
                properties = utils.get_properties(book, book_properties_type_dict)
                notion_helper.get_date_relation(properties, create_time)
                icon = get_icon(book.get("Cover")) if book.get("Cover") else None
                notion_helper.update_page(
                    page_id=existing_book.get("page_id"),
                    properties=properties,
                    icon=icon,
                )

        else:
            print(f"插入{book.get('Name')}")
            properties = utils.get_properties(book, book_properties_type_dict)
            notion_helper.get_date_relation(properties, create_time)
            parent = {
                "database_id": notion_helper.book_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(book.get("Cover"))
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("type")
    parser.add_argument("--only-title", action="append", default=[], help="仅同步标题包含该关键词的条目，可重复传入")
    parser.add_argument("--only-db-url", action="append", default=[], help="仅同步指定豆瓣条目（支持完整URL/subject id），可重复传入")
    parser.add_argument("--limit", type=int, default=0, help="最多处理条目数（0表示不限制）")
    parser.add_argument("--existing-only", action="store_true", help="仅更新Notion中已存在条目，不新增页面")
    parser.add_argument("--dedupe-duplicates", action="store_true", help="按DB_Url归档重复电影页面，只保留最佳条目")
    parser.add_argument("--dedupe-only", action="store_true", help="仅执行重复页归档，不拉取豆瓣数据")
    options = parser.parse_args()
    type = options.type
    if options.dedupe_only:
        options.dedupe_duplicates = True
    notion_helper = NotionHelper(type)
    is_movie = True if type=="movie" else False
    douban_name = os.getenv("DOUBAN_NAME", None)
    if is_movie:
        insert_movie(
            douban_name,
            notion_helper,
            only_titles=options.only_title,
            only_db_urls=options.only_db_url,
            limit=options.limit,
            existing_only=options.existing_only,
            dedupe_duplicates=options.dedupe_duplicates,
            dedupe_only=options.dedupe_only,
        )
    else:
        insert_book(douban_name,notion_helper)
if __name__ == "__main__":
    main()
