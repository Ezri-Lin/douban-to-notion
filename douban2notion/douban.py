import argparse
import json
import os
import re
from bs4 import BeautifulSoup
import pendulum
from retrying import retry
import requests
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils
DOUBAN_API_HOST = os.getenv("DOUBAN_API_HOST", "frodo.douban.com")
DOUBAN_API_KEY = os.getenv("DOUBAN_API_KEY", "0ac44ae016490db2204ce0a042db2916")

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
from dotenv import load_dotenv
load_dotenv()
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

# 豆瓣中文标题 -> IMDB英文检索词（通过环境变量配置，避免硬编码样本数据）
DEFAULT_IMDB_TITLE_ALIAS_MAP = {}

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
        response = requests.get(url, headers=headers, params=params)

        if response.ok:
            response = response.json()
            interests = response.get("interests")
            if len(interests)==0:
                break
            results.extend(interests)
            print(f"total = {total}")
            print(f"size = {len(results)}")
            page += 1
            offset = page * 50
    return results



def insert_movie(douban_name,notion_helper):
    notion_movies = notion_helper.query_all(database_id=notion_helper.movie_database_id)
    notion_movie_dict = {}
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
        notion_movie_dict[db_url] = current_movie
        if current_movie.get("IMDB"):
            notion_movie_imdb_dict[current_movie.get("IMDB")] = current_movie
        for unique_key in _build_movie_unique_keys(current_movie.get("Name"), current_movie.get("MovieName"), current_movie.get("Year")):
            notion_movie_title_year_dict[unique_key] = current_movie
    results = []
    for i in movie_status.keys():
        results.extend(fetch_subjects(douban_name, "movie", i))
    for result in results:
        movie = {}
        if not result:
            print(result)
            continue
        subject = result.get("subject")
        douban_title = subject.get("title")  # 豆瓣标题

        # 验证必要字段
        if not douban_title or douban_title == "未知电影":
            print(f"跳过无效电影: {douban_title}")
            continue
        if not subject.get("year"):
            print(f"跳过无年份电影: {douban_title}")
            continue

        # 判断是否为中文电影
        countries = subject.get("countries", [])
        # card_subtitle 格式："年份 / 地区 / 类型 / 导演 / 演员"
        # 当 countries 为空时，从 card_subtitle 解析地区
        if not countries:
            card_subtitle = subject.get("card_subtitle", "")
            parts = card_subtitle.split(" / ")
            if len(parts) >= 2:
                countries = [c.strip() for c in parts[1].split(" ") if c.strip()]
        original_title = subject.get("original_title")  # 获取原名
        is_chinese = is_chinese_movie(douban_title, countries, original_title)

        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)

        movie["Date"] = create_time.int_timestamp
        movie["DB_Url"] = subject.get("url")
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

            douban_title = movie.get("_douban_title")
            is_chinese = movie.get("_is_chinese")
            original_title = movie.get("_original_title")

            # ── 获取 IMDB 信息 ──────────────────────────────────────
            subtype = subject.get("subtype", "movie")
            imdb_id = notion_movive.get("IMDB") or get_imdb(movie.get("DB_Url"))
            if imdb_id:
                existing_imdb_info = get_imdb_info(imdb_id)
                if existing_imdb_info and not _is_imdb_title_consistent(douban_title, existing_imdb_info.get("title")):
                    print(f"  现有IMDB({imdb_id})与标题不一致，重新检索: {douban_title}")
                    imdb_id = None
            if not imdb_id:
                found_by_search = False
                for search_title in _build_imdb_search_titles(douban_title):
                    imdb_id = search_imdb_by_title(search_title, movie.get("Year"), media_type=subtype)
                    if not imdb_id:
                        imdb_id = search_imdb_by_title(search_title, media_type=subtype)
                    if imdb_id:
                        found_by_search = True
                        break
                if not found_by_search:
                    print(f"  IMDB检索失败: {douban_title} ({movie.get('Year')}, {subtype})")
            imdb_info = None
            if imdb_id:
                imdb_info = get_imdb_info(imdb_id)
                movie["IMDB"] = imdb_id
                movie["IMDB_Url"] = f"https://www.imdb.com/title/{imdb_id}/"

            # ── 计算正确的 Name / MovieName / Cover ─────────────────
            clean_douban_title = _strip_season(douban_title)
            clean_original_title = _strip_season(original_title)
            if is_chinese:
                movie["Name"] = clean_douban_title
                # 华语片不设置 MovieName
            else:
                # 外文电影：Name=原名，MovieName=中文译名
                if clean_original_title:
                    movie["Name"] = _normalize_full_title(clean_original_title)
                elif imdb_info and imdb_info.get('title'):
                    movie["Name"] = _normalize_full_title(_strip_season(imdb_info['title']))
                elif _get_alias_title(clean_douban_title):
                    movie["Name"] = _normalize_full_title(_get_alias_title(clean_douban_title))
                else:
                    movie["Name"] = clean_douban_title
                movie["MovieName"] = clean_douban_title

            if imdb_info:
                if imdb_info.get('rating'):
                    movie["IMDBRating"] = imdb_info['rating']
                if imdb_info.get('poster'):
                    movie["Cover"] = imdb_info['poster']
                    movie["CoverSource"] = "IMDB"
                    movie["CoverStatus"] = "Ok"
                else:
                    movie["CoverStatus"] = "Missing"
                movie["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp
            else:
                movie["CoverStatus"] = "Missing"
                movie["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp

            # ── 判断是否有实质变化需要更新 ───────────────────────────
            current_name = notion_movive.get("Name")
            needs_update = (
                notion_movive.get("Date") != movie.get("Date")
                or notion_movive.get("Remark") != movie.get("Remark")
                or notion_movive.get("Status") != movie.get("Status")
                or notion_movive.get("Rating") != movie.get("Rating")
                or notion_movive.get("Actor") is None
                or notion_movive.get("Director") is None
                or not notion_movive.get("IMDB")
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
                    if cast_crew and cast_crew['actors']:
                        douban_actors_map = {idx: a.get("name") for idx, a in enumerate(subject.get("actors", []))}
                        actor_ids = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            person_info_data = get_imdb_person_info(actor['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(person_info_data.get('birthplace'))
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': actor['id'],
                                    'bio': person_info_data.get('bio'),
                                    'c_name': douban_actors_map.get(idx)
                                }
                            actor_name = douban_actors_map.get(idx) or actor['name']
                            actor_ids.append(notion_helper.get_relation_id(
                                actor_name, notion_helper.actor_database_id, USER_ICON_URL, {}, person_info
                            ))
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
                        douban_actors_map = {idx: a.get("name") for idx, a in enumerate(subject.get("actors", []))}
                        actor_ids = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            person_info_data = get_imdb_person_info(actor['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(person_info_data.get('birthplace'))
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': actor['id'],
                                    'bio': person_info_data.get('bio'),
                                    # 外文条目不按顺序映射中文名，避免 C-Name 错位
                                    'c_name': None
                                }
                            actor_ids.append(notion_helper.get_relation_id(
                                actor['name'], notion_helper.actor_database_id, USER_ICON_URL, {}, person_info
                            ))
                        movie["Actor"] = _ensure_actor_relations(
                            actor_ids, subject, notion_helper, allow_douban_fallback=False
                        )

                # ── Director ─────────────────────────────────────────
                if is_chinese:
                    # 中文片：如果有IMDB，优先每次按豆瓣中文名刷新关系；否则缺失时补齐
                    if cast_crew and cast_crew['directors']:
                        douban_directors_map = {idx: d.get("name") for idx, d in enumerate(subject.get("directors", []))}
                        director_ids = []
                        for idx, director in enumerate(cast_crew['directors']):
                            person_info_data = get_imdb_person_info(director['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(person_info_data.get('birthplace'))
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': director['id'],
                                    'bio': person_info_data.get('bio'),
                                    'c_name': douban_directors_map.get(idx)
                                }
                            director_name = douban_directors_map.get(idx) or director['name']
                            director_ids.append(notion_helper.get_relation_id(
                                director_name, notion_helper.director_database_id, USER_ICON_URL, {}, person_info
                            ))
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
                        douban_directors_map = {idx: d.get("name") for idx, d in enumerate(subject.get("directors", []))}
                        director_ids = []
                        for idx, director in enumerate(cast_crew['directors']):
                            person_info_data = get_imdb_person_info(director['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(person_info_data.get('birthplace'))
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': director['id'],
                                    'bio': person_info_data.get('bio'),
                                    # 外文条目不按顺序映射中文名，避免 C-Name 错位
                                    'c_name': None
                                }
                            director_ids.append(notion_helper.get_relation_id(
                                director['name'], notion_helper.director_database_id, USER_ICON_URL, {}, person_info
                            ))
                        movie["Director"] = director_ids

                # 清理临时字段
                movie.pop("_douban_title", None)
                movie.pop("_original_title", None)
                movie.pop("_is_chinese", None)

                properties = utils.get_properties(movie, movie_properties_type_dict)
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
                notion_movive.update({
                    "Remark": movie.get("Remark"),
                    "Status": movie.get("Status"),
                    "Date": movie.get("Date"),
                    "Rating": movie.get("Rating"),
                    "Actor": movie.get("Actor", notion_movive.get("Actor")),
                    "Director": movie.get("Director", notion_movive.get("Director")),
                    "IMDB": movie.get("IMDB", notion_movive.get("IMDB")),
                    "IMDB_Url": movie.get("IMDB_Url", notion_movive.get("IMDB_Url")),
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
            douban_title = movie.get("_douban_title")
            is_chinese = movie.get("_is_chinese")
            original_title = movie.get("_original_title")

            print(f"插入{douban_title} ({'中文片' if is_chinese else '外文片'})")

            # ── 获取 IMDB 信息 ──────────────────────────────────────
            subtype = subject.get("subtype", "movie")
            imdb_id = get_imdb(movie.get("DB_Url"))
            if not imdb_id:
                print(f"  豆瓣页面无IMDB信息，尝试搜索IMDB...")
                found_by_search = False
                for search_title in _build_imdb_search_titles(douban_title):
                    imdb_id = search_imdb_by_title(search_title, movie.get("Year"), media_type=subtype)
                    if not imdb_id:
                        imdb_id = search_imdb_by_title(search_title, media_type=subtype)
                    if imdb_id:
                        found_by_search = True
                        break
                if not found_by_search:
                    print(f"  IMDB检索失败: {douban_title} ({movie.get('Year')}, {subtype})")

            imdb_info = None
            if imdb_id:
                movie["IMDB"] = imdb_id
                movie["IMDB_Url"] = f"https://www.imdb.com/title/{imdb_id}/"
                imdb_info = get_imdb_info(imdb_id)

            # ── 设置 Name / MovieName ────────────────────────────────
            clean_douban_title = _strip_season(douban_title)
            clean_original_title = _strip_season(original_title)
            if is_chinese:
                movie["Name"] = clean_douban_title
                # 华语片不设置 MovieName
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

            # ── 封面：仅使用IMDB ────────────────────────────────────
            cover = None
            if imdb_info and imdb_info.get('poster'):
                cover = imdb_info['poster']
                movie["CoverSource"] = "IMDB"
                movie["CoverStatus"] = "Ok"
            if imdb_info and imdb_info.get('rating'):
                movie["IMDBRating"] = imdb_info['rating']
            if not cover:
                print(f"  IMDB封面获取失败，跳过封面")
                movie["CoverStatus"] = "Missing"
            movie["CoverCheckedAt"] = pendulum.now(tz=utils.tz).int_timestamp

            movie["Cover"] = cover
            movie["Medium"] = subject.get("type")

            # 清理临时字段
            movie.pop("_douban_title", None)
            movie.pop("_original_title", None)
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

                    douban_actors_map = {idx: a.get("name") for idx, a in enumerate(subject.get("actors", []))}
                    douban_directors_map = {idx: d.get("name") for idx, d in enumerate(subject.get("directors", []))}

                    if cast_crew['actors']:
                        actor_relations = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            person_info_data = get_imdb_person_info(actor['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(
                                    person_info_data.get('birthplace')
                                )
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': actor['id'],
                                    'bio': person_info_data.get('bio'),
                                    'c_name': douban_actors_map.get(idx)
                                }
                            actor_id = notion_helper.get_relation_id(
                                douban_actors_map.get(idx) or actor['name'],
                                notion_helper.actor_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            actor_relations.append(actor_id)
                        movie["Actor"] = _ensure_actor_relations(
                            actor_relations, subject, notion_helper, allow_douban_fallback=False
                        )

                    if cast_crew['directors']:
                        director_relations = []
                        for idx, director in enumerate(cast_crew['directors']):
                            person_info_data = get_imdb_person_info(director['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(
                                    person_info_data.get('birthplace')
                                )
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': director['id'],
                                    'bio': person_info_data.get('bio'),
                                    'c_name': douban_directors_map.get(idx)
                                }
                            director_id = notion_helper.get_relation_id(
                                douban_directors_map.get(idx) or director['name'],
                                notion_helper.director_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            director_relations.append(director_id)
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

                    # 创建豆瓣演员/导演名字映射（用于获取中文名）
                    douban_actors_map = {}
                    douban_directors_map = {}

                    if subject.get("actors"):
                        for idx, actor in enumerate(subject.get("actors", [])):
                            douban_actors_map[idx] = actor.get("name")

                    if subject.get("directors"):
                        for idx, director in enumerate(subject.get("directors", [])):
                            douban_directors_map[idx] = director.get("name")

                    # 添加演员（IMDB数据，包含详细信息和豆瓣中文名）
                    if cast_crew['actors']:
                        actor_relations = []
                        for idx, actor in enumerate(cast_crew['actors']):
                            # 获取演员详细信息
                            person_info_data = get_imdb_person_info(actor['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(
                                    person_info_data.get('birthplace')
                                )
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': actor['id'],
                                    'bio': person_info_data.get('bio'),
                                    # 外文条目不按顺序映射中文名，避免 C-Name 错位
                                    'c_name': None
                                }

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

                    # 添加导演（IMDB数据，包含详细信息和豆瓣中文名）
                    if cast_crew['directors']:
                        director_relations = []
                        for idx, director in enumerate(cast_crew['directors']):
                            # 获取导演详细信息
                            person_info_data = get_imdb_person_info(director['id'])
                            person_info = None
                            if person_info_data:
                                nation = get_person_nation_from_birthplace(
                                    person_info_data.get('birthplace')
                                )
                                person_info = {
                                    'photo': person_info_data.get('photo'),
                                    'photo_source': 'IMDB',
                                    'nation': nation,
                                    'imdb_id': director['id'],
                                    'bio': person_info_data.get('bio'),
                                    # 外文条目不按顺序映射中文名，避免 C-Name 错位
                                    'c_name': None
                                }

                            director_id = notion_helper.get_relation_id(
                                director['name'],  # IMDB英文原名
                                notion_helper.director_database_id,
                                USER_ICON_URL,
                                {},
                                person_info
                            )
                            director_relations.append(director_id)
                        movie["Director"] = director_relations
                else:
                    if not imdb_id:
                        print(f"  外文条目未获取到IMDB，暂回退豆瓣演职员（中文）")
                    # 没有IMDB信息，回退到豆瓣
                    print(f"  IMDB不可用，回退到豆瓣数据源")
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
                if "tv" in kind or "mini-series" in kind or "series" in kind:
                    score += 3
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


def _is_imdb_title_consistent(douban_title, imdb_title):
    """判断既有 IMDB 条目是否与豆瓣标题语义一致，避免沿用错误的旧IMDB。"""
    if not imdb_title:
        return False
    imdb_norm = _normalize_ascii_title(imdb_title)
    if not imdb_norm:
        return False

    for candidate in _build_imdb_search_titles(douban_title):
        candidate_norm = _normalize_ascii_title(candidate)
        if not candidate_norm:
            continue
        if candidate_norm in imdb_norm or imdb_norm in candidate_norm:
            return True

        # 宽松匹配：关键词重叠至少两个
        candidate_words = set(re.findall(r"[a-z0-9]+", candidate.lower()))
        imdb_words = set(re.findall(r"[a-z0-9]+", imdb_title.lower()))
        if len(candidate_words & imdb_words) >= 2:
            return True
    return False


def search_imdb_by_title(title, year=None, media_type="movie"):
    """通过电影/剧集名称在IMDB搜索
    media_type: "movie" 搜索电影(ttype=ft)，"tv" 搜索剧集(ttype=tv)
    """
    try:
        suggest_id = _search_imdb_suggest(title, year=year, media_type=media_type)
        if suggest_id:
            print(f"  通过IMDB suggest找到: {suggest_id} ({title})")
            return suggest_id

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

            # 查找第一个搜索结果
            result = soup.find('a', href=lambda x: x and '/title/tt' in x)
            if result:
                href = result.get('href')
                # 提取IMDB ID
                imdb_id_match = re.search(r'/(tt\d+)/', href)
                if imdb_id_match:
                    imdb_id = imdb_id_match.group(1)
                    print(f"  通过IMDB搜索找到: {imdb_id}")
                    return imdb_id
    except Exception as e:
        print(f"  IMDB搜索失败: {str(e)[:50]}")
    return None

def get_imdb_person_info(person_id):
    """从IMDB获取演员/导演详细信息"""
    if not person_id:
        return None

    result = {
        'name': None,
        'photo': None,
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
                    result['photo'] = photo_url

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

    return result if (result['name'] or result['photo']) else None

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

    return result

def get_imdb_info(imdb_id):
    """从IMDB获取电影信息（海报、原名、评分）"""
    if not imdb_id:
        return None

    result = {
        'poster': None,
        'title': None,
        'rating': None
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
                if result['poster']:
                    info_str.append("海报")
                print(f"  从IMDB获取成功 ({imdb_id}): {', '.join(info_str)}")

    except Exception as e:
        print(f"  从IMDB获取信息失败 ({imdb_id}): {str(e)[:50]}")

    return result if (result['poster'] or result['title'] or result['rating']) else None

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
    options = parser.parse_args()
    type = options.type
    notion_helper = NotionHelper(type)
    is_movie = True if type=="movie" else False
    douban_name = os.getenv("DOUBAN_NAME", None)
    if is_movie:
        insert_movie(douban_name,notion_helper)
    else:
        insert_book(douban_name,notion_helper)
if __name__ == "__main__":
    main()
