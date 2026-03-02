import argparse
import html
import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pendulum
import requests
from dotenv import load_dotenv

from douban2notion.douban import (
    get_imdb_info,
    get_imdb_person_info,
    search_imdb_by_title,
    _is_imdb_title_consistent,
)
from douban2notion.notion_helper import NotionHelper
from douban2notion.utils import get_property_value


load_dotenv()

URL_VALIDATION_CACHE: Dict[str, bool] = {}

ISSUE_MISSING_IMDB = "MissingIMDB"
ISSUE_INVALID_IMDB = "InvalidIMDB"
ISSUE_MISSING_COVER = "MissingCover"
ISSUE_BROKEN_COVER = "BrokenCover"
ISSUE_MISSING_ACTOR = "MissingActor"
ISSUE_MISSING_DIRECTOR = "MissingDirector"
ISSUE_MISSING_AUTHOR = "MissingAuthor"
ISSUE_MISSING_NAME = "MissingName"
ISSUE_MISSING_MOVIE_NAME = "MissingMovieName"
ISSUE_MISSING_PHOTO = "MissingPhoto"
ISSUE_BROKEN_PHOTO = "BrokenPhoto"
ISSUE_FOREIGN_NAME_CHINESE = "ForeignNameChinese"
ISSUE_FOREIGN_ACTOR_CHINESE = "ForeignActorChinese"
ISSUE_FOREIGN_DIRECTOR_CHINESE = "ForeignDirectorChinese"
ISSUE_DUPLICATE_DB_URL = "DuplicateDBUrl"
ISSUE_DUPLICATE_IMDB = "DuplicateIMDB"
ISSUE_IMDB_TITLE_MISMATCH = "IMDBTitleMismatch"
ISSUE_MISSING_DOUBAN_RATING = "MissingDoubanRating"
ISSUE_MISSING_IMDB_RATING = "MissingIMDBRating"
ISSUE_PERSON_NAME_IMDB_MISMATCH = "PersonNameIMDBMismatch"

MANAGED_ISSUES = {
    ISSUE_MISSING_IMDB,
    ISSUE_INVALID_IMDB,
    ISSUE_MISSING_COVER,
    ISSUE_BROKEN_COVER,
    ISSUE_MISSING_ACTOR,
    ISSUE_MISSING_DIRECTOR,
    ISSUE_MISSING_AUTHOR,
    ISSUE_MISSING_NAME,
    ISSUE_MISSING_MOVIE_NAME,
    ISSUE_MISSING_PHOTO,
    ISSUE_BROKEN_PHOTO,
    ISSUE_FOREIGN_NAME_CHINESE,
    ISSUE_FOREIGN_ACTOR_CHINESE,
    ISSUE_FOREIGN_DIRECTOR_CHINESE,
    ISSUE_DUPLICATE_DB_URL,
    ISSUE_DUPLICATE_IMDB,
    ISSUE_IMDB_TITLE_MISMATCH,
    ISSUE_MISSING_DOUBAN_RATING,
    ISSUE_MISSING_IMDB_RATING,
    ISSUE_PERSON_NAME_IMDB_MISMATCH,
}


def _log(message: str) -> None:
    print(message, flush=True)


def _has_chinese(text: Optional[str]) -> bool:
    return bool(text) and re.search(r"[\u4e00-\u9fff]", str(text)) is not None


def _has_latin(text: Optional[str]) -> bool:
    return bool(text) and re.search(r"[A-Za-z]", str(text)) is not None


def _is_roman_token(token: str) -> bool:
    t = str(token or "").strip().upper()
    if not t:
        return False
    return bool(re.fullmatch(r"[IVXLCDM]+", t))


def _has_meaningful_latin_segment(text: Optional[str]) -> bool:
    """
    判断混合标题中的拉丁字母是否具有“语义词”特征。
    仅罗马数字（如 II/III/IV）不作为外文污染判定依据。
    """
    raw = str(text or "")
    if not _has_latin(raw):
        return False
    tokens = re.findall(r"[A-Za-z]+", raw)
    if not tokens:
        return False
    for token in tokens:
        if len(token) >= 3 and not _is_roman_token(token):
            return True
    return False


def _now_date_payload() -> Dict:
    return {
        "date": {
            "start": pendulum.now("Asia/Shanghai").to_datetime_string(),
            "time_zone": "Asia/Shanghai",
        }
    }


def _get_title_value(page: Dict, property_name: str = "Name") -> Optional[str]:
    prop = ((page.get("properties") or {}).get(property_name) or {})
    return get_property_value(prop)


def _get_rich_text_value(page: Dict, property_name: str) -> Optional[str]:
    prop = ((page.get("properties") or {}).get(property_name) or {})
    return get_property_value(prop)


def _get_multi_select_names(page: Dict, property_name: str) -> List[str]:
    prop = ((page.get("properties") or {}).get(property_name) or {})
    if prop.get("type") != "multi_select":
        return []
    values = prop.get("multi_select") or []
    names = []
    for item in values:
        name = str((item or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _get_relation_ids(page: Dict, property_name: str) -> List[str]:
    prop = ((page.get("properties") or {}).get(property_name) or {})
    if prop.get("type") != "relation":
        return []
    relation = prop.get("relation") or []
    return [x.get("id") for x in relation if x.get("id")]


def _get_files_url(page: Dict, property_name: str) -> Optional[str]:
    prop = ((page.get("properties") or {}).get(property_name) or {})
    if prop.get("type") != "files":
        return None
    files = prop.get("files") or []
    if not files:
        return None
    first = files[0]
    if first.get("type") == "external":
        return ((first.get("external") or {}).get("url"))
    return None


def _get_icon_url(page: Dict) -> Optional[str]:
    icon = page.get("icon") or {}
    if icon.get("type") == "external":
        return ((icon.get("external") or {}).get("url"))
    return None


def _get_cover_url(page: Dict) -> Optional[str]:
    cover = page.get("cover") or {}
    if cover.get("type") == "external":
        return ((cover.get("external") or {}).get("url"))
    return None


def _normalize_movie_imdb_id(imdb_id: Optional[str]) -> Optional[str]:
    candidate = str(imdb_id or "").strip()
    if not re.match(r"^tt\d{7,8}$", candidate):
        return None
    return candidate


def _has_rating_value(value) -> bool:
    if value is None:
        return False
    try:
        return float(value) > 0
    except Exception:
        return False


def _is_imdb_binding_consistent(name: Optional[str], movie_name: Optional[str], imdb_title: Optional[str]) -> bool:
    """用现有一致性规则做保守判断，抓明显错绑。"""
    if not imdb_title:
        return True
    candidates = []
    if name:
        candidates.append(str(name).strip())
    if movie_name:
        candidates.append(str(movie_name).strip())
    if not candidates:
        return True
    for title in candidates:
        aliases = [x for x in candidates if x != title]
        if _is_imdb_title_consistent(
            title,
            imdb_title,
            original_title=(aliases[0] if aliases else None),
            alias_titles=aliases,
        ):
            return True
    return False


def _to_int_year(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    year = int(text)
    if year < 1888 or year > 2100:
        return None
    return year


def _normalize_media_type(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if text in {"tv", "movie"}:
        return text
    return "movie"


def _is_tv_season_page(props: Dict) -> bool:
    medium = str(get_property_value(props.get("Medium") or {}) or "").strip().lower()
    season = str(get_property_value(props.get("Season") or {}) or "").strip()
    return bool(season) and (medium == "tv" or not medium)


def _is_binding_still_plausible_via_search(
    current_imdb: str,
    name: Optional[str],
    movie_name: Optional[str],
    year_value: Optional[str],
    media_value: Optional[str],
) -> bool:
    media_type = _normalize_media_type(media_value)
    year = _to_int_year(year_value)
    candidates = []
    for title in [name, movie_name]:
        title_text = str(title or "").strip()
        if not title_text:
            continue
        if title_text not in candidates:
            candidates.append(title_text)

    for title in candidates:
        result = search_imdb_by_title(title, year=year, media_type=media_type)
        if not result:
            result = search_imdb_by_title(title, media_type=media_type)
        if result and result == current_imdb:
            return True
    return False


def _normalize_person_imdb_id(imdb_id: Optional[str]) -> Optional[str]:
    candidate = str(imdb_id or "").strip()
    if not re.match(r"^nm\d{7,8}$", candidate):
        return None
    return candidate


def _should_check_person_name_mismatch(name: Optional[str]) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if "·" in text:
        return True
    # 仅对外文/混合名称做IMDb一致性比对，避免中文名误报且大幅降低远程请求量。
    return _has_latin(text)


def _normalize_ascii_name_key(text: Optional[str]) -> str:
    value = html.unescape(str(text or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def _is_person_name_consistent_with_imdb(local_name: Optional[str], imdb_name: Optional[str]) -> bool:
    if not local_name or not imdb_name:
        return True
    local = html.unescape(str(local_name)).strip()
    imdb_canonical = html.unescape(str(imdb_name)).strip()
    if not local or not imdb_canonical:
        return True
    if local == imdb_canonical:
        return True

    # 中点音译（如“范·迪塞尔”）统一视为外文名错误。
    if "·" in local:
        return False

    local_key = _normalize_ascii_name_key(local)
    imdb_key = _normalize_ascii_name_key(imdb_canonical)
    if local_key and imdb_key:
        if local_key in imdb_key or imdb_key in local_key:
            return True
        local_words = set(re.findall(r"[a-z0-9]+", local.lower()))
        imdb_words = set(re.findall(r"[a-z0-9]+", imdb_canonical.lower()))
        overlap = local_words & imdb_words
        if local_words and (len(overlap) / len(local_words)) >= 0.6:
            return True
        return False

    # 中文名与IMDb英文名无法可靠自动比对，避免误报。
    return True


def _is_valid_image_url(url: Optional[str], check_remote: bool) -> bool:
    if not url:
        return False
    if not check_remote:
        return True
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


def _build_people_name_map(nh: NotionHelper, db_id: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not db_id:
        return result
    try:
        pages = nh.query_all(database_id=db_id)
    except Exception:
        return result
    for page in pages:
        pid = page.get("id")
        name = _get_title_value(page, "Name")
        if pid and name:
            result[pid] = name
    return result


def _relation_has_chinese(relation_ids: Sequence[str], people_name_map: Dict[str, str]) -> bool:
    for relation_id in relation_ids or []:
        name = people_name_map.get(relation_id)
        if not name:
            continue
        if _has_chinese(name):
            return True
    return False


def _merge_issue_tags(existing: Sequence[str], computed: Sequence[str]) -> List[str]:
    existing_set = {str(x).strip() for x in (existing or []) if str(x).strip()}
    computed_set = {str(x).strip() for x in (computed or []) if str(x).strip()}
    custom = {x for x in existing_set if x not in MANAGED_ISSUES}
    final = sorted(custom | computed_set)
    return final


def _update_page_properties(client, page: Dict, updates: Dict) -> bool:
    if not updates:
        return False
    client.pages.update(page_id=page.get("id"), properties=updates)
    return True


def _append_select_update_if_changed(page: Dict, field_name: str, new_value: str, updates: Dict) -> None:
    properties = page.get("properties") or {}
    if field_name not in properties:
        return
    current = get_property_value(properties.get(field_name) or {})
    if current != new_value:
        updates[field_name] = {"select": {"name": new_value}}


def _append_date_update_if_exists(page: Dict, field_name: str, updates: Dict) -> None:
    properties = page.get("properties") or {}
    if field_name in properties:
        updates[field_name] = _now_date_payload()


def _append_status_and_checked_at_if_changed(
    page: Dict,
    status_field: str,
    status_value: str,
    checked_field: str,
    updates: Dict,
) -> None:
    _append_select_update_if_changed(page, status_field, status_value, updates)
    if status_field in updates:
        _append_date_update_if_exists(page, checked_field, updates)


def _append_data_issue_update(page: Dict, issues: Sequence[str], updates: Dict) -> None:
    properties = page.get("properties") or {}
    if "DataIssue" not in properties:
        return
    existing = _get_multi_select_names(page, "DataIssue")
    final = _merge_issue_tags(existing, issues)
    if sorted(existing) != sorted(final):
        updates["DataIssue"] = {"multi_select": [{"name": x} for x in final]}


def audit_movie(
    nh: NotionHelper,
    check_remote: bool,
    limit: int = 0,
    enable_imdb_title_mismatch: bool = False,
) -> Tuple[int, int]:
    pages = nh.query_all(database_id=nh.movie_database_id)
    if limit > 0:
        pages = pages[:limit]
    _log(f"[Movie] start total={len(pages)} check_remote={check_remote} mismatch_check={enable_imdb_title_mismatch}")

    actor_name_map = _build_people_name_map(nh, nh.actor_database_id)
    director_name_map = _build_people_name_map(nh, nh.director_database_id)

    db_url_map: Dict[str, List[str]] = defaultdict(list)
    imdb_map: Dict[str, List[bool]] = defaultdict(list)

    for page in pages:
        props = page.get("properties") or {}
        db_url = get_property_value(props.get("DB_Url") or props.get("Url") or {})
        imdb_raw = _get_rich_text_value(page, "IMDB")
        imdb_norm = _normalize_movie_imdb_id(imdb_raw)
        is_tv_season = _is_tv_season_page(props)
        if db_url:
            db_url_map[str(db_url).strip()].append(page.get("id"))
        if imdb_norm:
            imdb_map[imdb_norm].append(is_tv_season)

    changed = 0
    checked = 0
    for page in pages:
        checked += 1
        props = page.get("properties") or {}
        page_id = page.get("id")
        issues: Set[str] = set()

        name = _get_title_value(page, "Name")
        movie_name = _get_rich_text_value(page, "MovieName")
        imdb_raw = _get_rich_text_value(page, "IMDB")
        imdb_norm = _normalize_movie_imdb_id(imdb_raw)
        douban_rating = get_property_value(props.get("DoubanRating") or {})
        imdb_rating = get_property_value(props.get("IMDBRating") or {})
        actor_ids = _get_relation_ids(page, "Actor")
        director_ids = _get_relation_ids(page, "Director")
        year_value = get_property_value(props.get("Year") or {})
        medium_value = get_property_value(props.get("Medium") or {})
        db_url = get_property_value(props.get("DB_Url") or props.get("Url") or {})
        db_url = str(db_url).strip() if db_url else None

        if not name:
            issues.add(ISSUE_MISSING_NAME)
        if not movie_name:
            issues.add(ISSUE_MISSING_MOVIE_NAME)

        if not imdb_raw:
            issues.add(ISSUE_MISSING_IMDB)
        elif not imdb_norm:
            issues.add(ISSUE_INVALID_IMDB)
        if not _has_rating_value(douban_rating):
            issues.add(ISSUE_MISSING_DOUBAN_RATING)
        if imdb_norm and not _has_rating_value(imdb_rating):
            issues.add(ISSUE_MISSING_IMDB_RATING)

        if not actor_ids:
            issues.add(ISSUE_MISSING_ACTOR)
        if not director_ids:
            issues.add(ISSUE_MISSING_DIRECTOR)

        prop_cover = _get_files_url(page, "Cover")
        icon_url = _get_icon_url(page)
        cover_url = _get_cover_url(page)

        has_any_cover = bool(prop_cover or icon_url or cover_url)
        if not has_any_cover:
            issues.add(ISSUE_MISSING_COVER)
        elif check_remote:
            urls = [x for x in [prop_cover, icon_url, cover_url] if x]
            if not all(_is_valid_image_url(url, check_remote=True) for url in urls):
                issues.add(ISSUE_BROKEN_COVER)

        if name and _has_latin(name) and not _has_chinese(name):
            if _relation_has_chinese(actor_ids, actor_name_map):
                issues.add(ISSUE_FOREIGN_ACTOR_CHINESE)
            if _relation_has_chinese(director_ids, director_name_map):
                issues.add(ISSUE_FOREIGN_DIRECTOR_CHINESE)
        elif name and _has_chinese(name) and _has_meaningful_latin_segment(name):
            issues.add(ISSUE_FOREIGN_NAME_CHINESE)

        if db_url and len(db_url_map.get(db_url) or []) > 1:
            issues.add(ISSUE_DUPLICATE_DB_URL)
        if imdb_norm and len(imdb_map.get(imdb_norm) or []) > 1:
            imdb_rows = imdb_map.get(imdb_norm) or []
            if not all(imdb_rows):
                issues.add(ISSUE_DUPLICATE_IMDB)
        if imdb_norm and enable_imdb_title_mismatch:
            imdb_info = get_imdb_info(imdb_norm) or {}
            imdb_title = imdb_info.get("title")
            is_consistent = _is_imdb_binding_consistent(name, movie_name, imdb_title)
            if not is_consistent and not _is_binding_still_plausible_via_search(
                imdb_norm, name, movie_name, year_value, medium_value
            ):
                issues.add(ISSUE_IMDB_TITLE_MISMATCH)

        updates: Dict = {}
        _append_data_issue_update(page, sorted(issues), updates)

        if ISSUE_MISSING_COVER in issues:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Missing", "CoverCheckedAt", updates)
        elif ISSUE_BROKEN_COVER in issues:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Broken", "CoverCheckedAt", updates)
        else:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Ok", "CoverCheckedAt", updates)

        if _update_page_properties(nh.client, page, updates):
            changed += 1
        if checked % 50 == 0:
            _log(f"[Movie] progress {checked}/{len(pages)} changed={changed}")

    _log(f"[Movie] checked={checked} changed={changed}")
    return checked, changed


def _audit_people_common(
    nh: NotionHelper,
    db_id: Optional[str],
    label: str,
    check_remote: bool,
    enable_person_name_mismatch: bool = False,
    limit: int = 0,
) -> Tuple[int, int]:
    if not db_id:
        _log(f"[{label}] skipped (db not found)")
        return 0, 0

    pages = nh.query_all(database_id=db_id)
    if limit > 0:
        pages = pages[:limit]
    _log(f"[{label}] start total={len(pages)} check_remote={check_remote}")

    changed = 0
    checked = 0
    for page in pages:
        checked += 1
        issues: Set[str] = set()

        name = _get_title_value(page, "Name")
        imdb_raw = _get_rich_text_value(page, "IMDB")

        if not name:
            issues.add(ISSUE_MISSING_NAME)
        if name and "·" in str(name):
            # 外国人物使用中文音译中点名称（如“范·迪塞尔”）视为错误，后续应收敛到 IMDb 英文名。
            issues.add(ISSUE_FOREIGN_NAME_CHINESE)

        if imdb_raw and not _normalize_person_imdb_id(imdb_raw):
            issues.add(ISSUE_INVALID_IMDB)
        imdb_norm = _normalize_person_imdb_id(imdb_raw)
        if enable_person_name_mismatch and imdb_norm and _should_check_person_name_mismatch(name):
            person_info = get_imdb_person_info(imdb_norm) or {}
            imdb_name = person_info.get("name")
            if not _is_person_name_consistent_with_imdb(name, imdb_name):
                issues.add(ISSUE_PERSON_NAME_IMDB_MISMATCH)

        prop_photo = _get_files_url(page, "Photo")
        icon_url = _get_icon_url(page)
        cover_url = _get_cover_url(page)

        has_any_photo = bool(prop_photo or icon_url or cover_url)
        if not has_any_photo:
            issues.add(ISSUE_MISSING_PHOTO)
        elif check_remote:
            urls = [x for x in [prop_photo, icon_url, cover_url] if x]
            if not all(_is_valid_image_url(url, check_remote=True) for url in urls):
                issues.add(ISSUE_BROKEN_PHOTO)

        updates: Dict = {}
        _append_data_issue_update(page, sorted(issues), updates)

        if ISSUE_MISSING_PHOTO in issues:
            _append_status_and_checked_at_if_changed(page, "PhotoStatus", "Missing", "PhotoCheckedAt", updates)
        elif ISSUE_BROKEN_PHOTO in issues:
            _append_status_and_checked_at_if_changed(page, "PhotoStatus", "Broken", "PhotoCheckedAt", updates)
        else:
            _append_status_and_checked_at_if_changed(page, "PhotoStatus", "Ok", "PhotoCheckedAt", updates)

        if _update_page_properties(nh.client, page, updates):
            changed += 1
        if checked % 50 == 0:
            _log(f"[{label}] progress {checked}/{len(pages)} changed={changed}")

    _log(f"[{label}] checked={checked} changed={changed}")
    return checked, changed


def audit_actor(
    nh: NotionHelper, check_remote: bool, enable_person_name_mismatch: bool = False, limit: int = 0
) -> Tuple[int, int]:
    return _audit_people_common(
        nh, nh.actor_database_id, "Actor", check_remote, enable_person_name_mismatch, limit
    )


def audit_director(
    nh: NotionHelper, check_remote: bool, enable_person_name_mismatch: bool = False, limit: int = 0
) -> Tuple[int, int]:
    return _audit_people_common(
        nh, nh.director_database_id, "Director", check_remote, enable_person_name_mismatch, limit
    )


def audit_author(
    nh: NotionHelper, check_remote: bool, enable_person_name_mismatch: bool = False, limit: int = 0
) -> Tuple[int, int]:
    return _audit_people_common(
        nh, nh.author_database_id, "Author", check_remote, enable_person_name_mismatch, limit
    )


def audit_book(nh: NotionHelper, check_remote: bool, limit: int = 0) -> Tuple[int, int]:
    pages = nh.query_all(database_id=nh.book_database_id)
    if limit > 0:
        pages = pages[:limit]
    _log(f"[Book] start total={len(pages)} check_remote={check_remote}")

    db_url_map: Dict[str, List[str]] = defaultdict(list)
    for page in pages:
        props = page.get("properties") or {}
        db_url = get_property_value(props.get("DB_Url") or props.get("Url") or {})
        if db_url:
            db_url_map[str(db_url).strip()].append(page.get("id"))

    changed = 0
    checked = 0
    for page in pages:
        checked += 1
        issues: Set[str] = set()
        props = page.get("properties") or {}

        name = _get_title_value(page, "Name")
        author_ids = _get_relation_ids(page, "Author")
        db_url = get_property_value(props.get("DB_Url") or props.get("Url") or {})
        db_url = str(db_url).strip() if db_url else None

        if not name:
            issues.add(ISSUE_MISSING_NAME)
        if not author_ids:
            issues.add(ISSUE_MISSING_AUTHOR)

        prop_cover = _get_files_url(page, "Cover")
        icon_url = _get_icon_url(page)
        cover_url = _get_cover_url(page)

        has_any_cover = bool(prop_cover or icon_url or cover_url)
        if not has_any_cover:
            issues.add(ISSUE_MISSING_COVER)
        elif check_remote:
            urls = [x for x in [prop_cover, icon_url, cover_url] if x]
            if not all(_is_valid_image_url(url, check_remote=True) for url in urls):
                issues.add(ISSUE_BROKEN_COVER)

        if db_url and len(db_url_map.get(db_url) or []) > 1:
            issues.add(ISSUE_DUPLICATE_DB_URL)

        updates: Dict = {}
        _append_data_issue_update(page, sorted(issues), updates)

        if ISSUE_MISSING_COVER in issues:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Missing", "CoverCheckedAt", updates)
        elif ISSUE_BROKEN_COVER in issues:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Broken", "CoverCheckedAt", updates)
        else:
            _append_status_and_checked_at_if_changed(page, "CoverStatus", "Ok", "CoverCheckedAt", updates)

        if _update_page_properties(nh.client, page, updates):
            changed += 1
        if checked % 50 == 0:
            _log(f"[Book] progress {checked}/{len(pages)} changed={changed}")

    _log(f"[Book] checked={checked} changed={changed}")
    return checked, changed


def build_helper(kind: str) -> Optional[NotionHelper]:
    try:
        return NotionHelper(kind)
    except Exception as exc:
        _log(f"[{kind}] init failed: {str(exc)[:160]}")
        return None


def run(
    scope: str,
    check_remote: bool,
    limit: int = 0,
    enable_imdb_title_mismatch: bool = False,
    enable_person_name_mismatch: bool = False,
) -> None:
    _log(
        f"[Audit] scope={scope} check_remote={check_remote} limit={limit} "
        f"mismatch_check={enable_imdb_title_mismatch} person_name_mismatch={enable_person_name_mismatch}"
    )
    movie_helper = None
    book_helper = None

    if scope in {"all", "movie", "actor", "director"}:
        movie_helper = build_helper("movie")
    if scope in {"all", "book", "author"}:
        book_helper = build_helper("book")

    if scope in {"all", "movie"} and movie_helper and movie_helper.movie_database_id:
        audit_movie(
            movie_helper,
            check_remote=check_remote,
            limit=limit,
            enable_imdb_title_mismatch=enable_imdb_title_mismatch,
        )
    if scope in {"all", "actor"} and movie_helper:
        audit_actor(
            movie_helper,
            check_remote=check_remote,
            enable_person_name_mismatch=enable_person_name_mismatch,
            limit=limit,
        )
    if scope in {"all", "director"} and movie_helper:
        audit_director(
            movie_helper,
            check_remote=check_remote,
            enable_person_name_mismatch=enable_person_name_mismatch,
            limit=limit,
        )
    if scope in {"all", "book"} and book_helper and book_helper.book_database_id:
        audit_book(book_helper, check_remote=check_remote, limit=limit)
    if scope in {"all", "author"} and book_helper:
        audit_author(
            book_helper,
            check_remote=check_remote,
            enable_person_name_mismatch=enable_person_name_mismatch,
            limit=limit,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scope",
        nargs="?",
        default="all",
        choices=["all", "movie", "book", "actor", "director", "author"],
    )
    parser.add_argument(
        "--skip-url-check",
        action="store_true",
        help="只检查字段是否为空，不远程验证图片URL有效性",
    )
    parser.add_argument(
        "--enable-imdb-title-mismatch",
        action="store_true",
        help="启用 IMDb 标题一致性检测（可能产生误报，默认关闭）",
    )
    parser.add_argument(
        "--enable-person-name-mismatch",
        action="store_true",
        help="启用人物 Name 与 IMDb canonical 名称一致性检测（Actor/Director/Author）",
    )
    parser.add_argument("--limit", type=int, default=0, help="仅审计前N条，0表示不限制")
    args = parser.parse_args()

    run(
        scope=args.scope,
        check_remote=not args.skip_url_check,
        limit=args.limit,
        enable_imdb_title_mismatch=args.enable_imdb_title_mismatch,
        enable_person_name_mismatch=args.enable_person_name_mismatch,
    )


if __name__ == "__main__":
    main()
