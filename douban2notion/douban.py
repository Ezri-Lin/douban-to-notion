import argparse
from email import feedparser
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

def is_chinese_movie(title, countries=None):
    """判断是否为中文电影"""
    # 方法1：优先检查国家/地区（最可靠）
    if countries:
        chinese_regions = ['中国大陆', '中国香港', '中国台湾', '中国', '香港', '台湾', 'China', 'Hong Kong', 'Taiwan']
        # 检查是否有中国相关地区
        has_chinese_region = any(region in str(countries) for region in chinese_regions)
        # 检查是否有非中国地区
        foreign_regions = ['美国', '英国', '日本', '韩国', '法国', '德国', 'USA', 'UK', 'Japan', 'Korea', 'France', 'Germany']
        has_foreign_region = any(region in str(countries) for region in foreign_regions)

        # 如果只有中国地区，或者中国是主要制片地区，判定为中文片
        if has_chinese_region and not has_foreign_region:
            return True
        # 如果有外国地区且没有中国地区，判定为外文片
        if has_foreign_region and not has_chinese_region:
            return False

    # 方法2：检查标题是否主要是中文字符（作为辅助判断）
    if title:
        chinese_chars = sum(1 for char in title if '\u4e00' <= char <= '\u9fff')
        total_chars = len([c for c in title if c.strip() and not c.isspace()])
        if total_chars > 0 and chinese_chars / total_chars > 0.7:
            return True

    return False
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
    for i in notion_movies:
        movie = {}
        for key, value in i.get("properties").items():
            movie[key] = utils.get_property_value(value)
        notion_movie_dict[movie.get("Url")] = {
            "Remark": movie.get("Remark"),
            "Status": movie.get("Status"),
            "Date": movie.get("Date"),
            "Rating": movie.get("Rating"),
            "Actor": movie.get("Actor"),
            "IMDB": movie.get("IMDB"),
            "page_id": i.get("id")
        }
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
        is_chinese = is_chinese_movie(douban_title, countries)

        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)

        movie["Date"] = create_time.int_timestamp
        movie["Url"] = subject.get("url")
        movie["Status"] = movie_status.get(result.get("status"))
        movie["DoubanRating"] = subject.get("rating", {}).get("value", 0) if subject.get("rating") else 0
        movie["Year"] = subject.get("year")

        if result.get("rating"):
            movie["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            movie["Remark"] = result.get("comment")

        # 存储原始信息，稍后根据语言设置Name和MovieName
        movie["_douban_title"] = douban_title
        movie["_is_chinese"] = is_chinese
        if notion_movie_dict.get(movie.get("Url")):
            notion_movive = notion_movie_dict.get(movie.get("Url"))
            if (
                notion_movive.get("Date") != movie.get("Date")
                or notion_movive.get("Remark") != movie.get("Remark")
                or notion_movive.get("Status") != movie.get("Status")
                or notion_movive.get("Rating") != movie.get("Rating")
                or not notion_movive.get("Actor")
                or not notion_movive.get("IMDB")
            ):
                douban_title = movie.get("_douban_title")
                is_chinese = movie.get("_is_chinese")

                # 如果缺少Actor，根据语言选择数据源
                if not notion_movive.get("Actor"):
                    if is_chinese:
                        # 中文电影：从豆瓣获取
                        if subject.get("actors"):
                            actors = subject.get("actors")[0:MAX_ACTORS_RELATION]
                            movie["Actor"] = [
                                notion_helper.get_relation_id(
                                    x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                                )
                                for x in actors
                            ]
                    else:
                        # 外文电影：尝试从IMDB获取
                        imdb_id = notion_movive.get("IMDB") or get_imdb(movie.get("Url"))
                        if imdb_id:
                            print(f"  从IMDB获取演员信息")
                            cast_crew = get_imdb_cast_and_crew(imdb_id)
                            if cast_crew['actors']:
                                movie["Actor"] = [
                                    notion_helper.get_relation_id(
                                        actor['name'],
                                        notion_helper.actor_database_id,
                                        USER_ICON_URL
                                    )
                                    for actor in cast_crew['actors']
                                ]

                # 如果没有Director，同样根据语言选择数据源
                if not notion_movive.get("Director"):
                    if is_chinese:
                        if subject.get("directors"):
                            movie["Director"] = [
                                notion_helper.get_relation_id(
                                    x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                                )
                                for x in subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                            ]
                    else:
                        imdb_id = notion_movive.get("IMDB") or get_imdb(movie.get("Url"))
                        if imdb_id:
                            print(f"  从IMDB获取导演信息")
                            cast_crew = get_imdb_cast_and_crew(imdb_id)
                            if cast_crew['directors']:
                                movie["Director"] = [
                                    notion_helper.get_relation_id(
                                        director['name'],
                                        notion_helper.director_database_id,
                                        USER_ICON_URL
                                    )
                                    for director in cast_crew['directors']
                                ]

                # 如果没有IMDB信息，尝试获取
                if not notion_movive.get("IMDB"):
                    douban_title = movie.get("_douban_title")
                    is_chinese = movie.get("_is_chinese")

                    imdb_id = get_imdb(movie.get("Url"))
                    if not imdb_id:
                        imdb_id = search_imdb_by_title(douban_title, movie.get("Year"))

                    if imdb_id:
                        movie["IMDB"] = imdb_id
                        # 获取IMDB详细信息
                        imdb_info = get_imdb_info(imdb_id)
                        if imdb_info:
                            if is_chinese:
                                # 中文电影：Name=中文原名，MovieName=IMDB英文名
                                movie["Name"] = douban_title
                                if imdb_info.get('title'):
                                    movie["MovieName"] = imdb_info['title']
                            else:
                                # 外文电影：Name=IMDB原名，MovieName=豆瓣中文翻译名
                                if imdb_info.get('title'):
                                    movie["Name"] = imdb_info['title']
                                movie["MovieName"] = douban_title

                            if imdb_info.get('rating'):
                                movie["IMDBRating"] = imdb_info['rating']
                            if imdb_info.get('poster'):
                                movie["Cover"] = imdb_info['poster']
                    else:
                        # 没有IMDB信息，使用豆瓣标题
                        movie["Name"] = douban_title

                # 清理临时字段
                movie.pop("_douban_title", None)
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

        else:
            douban_title = movie.get("_douban_title")
            is_chinese = movie.get("_is_chinese")

            print(f"插入{douban_title} ({'中文片' if is_chinese else '外文片'})")

            # 获取IMDB信息
            imdb_id = get_imdb(movie.get("Url"))

            # 如果豆瓣页面没有IMDB信息，尝试通过电影名搜索
            if not imdb_id:
                print(f"  豆瓣页面无IMDB信息，尝试搜索IMDB...")
                imdb_id = search_imdb_by_title(douban_title, movie.get("Year"))

            cover = None
            if imdb_id:
                movie["IMDB"] = imdb_id
                # 获取IMDB详细信息（原名、评分、海报）
                imdb_info = get_imdb_info(imdb_id)
                if imdb_info:
                    if is_chinese:
                        # 中文电影：Name=中文原名，MovieName=IMDB英文名
                        movie["Name"] = douban_title
                        if imdb_info.get('title'):
                            movie["MovieName"] = imdb_info['title']
                            print(f"  英文名: {imdb_info['title']}")
                    else:
                        # 外文电影：Name=IMDB原名，MovieName=豆瓣中文翻译名
                        if imdb_info.get('title'):
                            movie["Name"] = imdb_info['title']
                            print(f"  IMDB原名: {imdb_info['title']}")
                        movie["MovieName"] = douban_title
                        print(f"  中文译名: {douban_title}")

                    if imdb_info.get('rating'):
                        movie["IMDBRating"] = imdb_info['rating']
                    if imdb_info.get('poster'):
                        cover = imdb_info['poster']
            else:
                # 没有IMDB信息，使用豆瓣标题作为Name
                movie["Name"] = douban_title

            # 清理临时字段
            movie.pop("_douban_title", None)
            movie.pop("_is_chinese", None)

            # 如果IMDB获取失败，回退到豆瓣封面
            if not cover:
                print(f"  IMDB封面获取失败，使用豆瓣封面")
                cover = subject.get("pic").get("normal")
                if cover and not cover.endswith('.webp'):
                    cover = cover.rsplit('.', 1)[0] + '.webp'

            movie["Cover"] = cover
            movie["Medium"] = subject.get("type")

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
                # 中文电影：从豆瓣获取Actor/Director
                print(f"  使用豆瓣数据源获取演员/导演")

                # 添加演员
                if subject.get("actors"):
                    actors = subject.get("actors")[0:MAX_ACTORS_RELATION]
                    movie["Actor"] = [
                        notion_helper.get_relation_id(
                            x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                        )
                        for x in actors
                    ]

                # 添加导演
                if subject.get("directors"):
                    movie["Director"] = [
                        notion_helper.get_relation_id(
                            x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                        )
                        for x in subject.get("directors")[0:MAX_DIRECTORS_RELATION]
                    ]
            else:
                # 外文电影：从IMDB获取Actor/Director
                if imdb_id:
                    print(f"  使用IMDB数据源获取演员/导演")
                    cast_crew = get_imdb_cast_and_crew(imdb_id)

                    # 添加演员（IMDB数据）
                    if cast_crew['actors']:
                        movie["Actor"] = [
                            notion_helper.get_relation_id(
                                actor['name'],
                                notion_helper.actor_database_id,
                                USER_ICON_URL
                            )
                            for actor in cast_crew['actors']
                        ]

                    # 添加导演（IMDB数据）
                    if cast_crew['directors']:
                        movie["Director"] = [
                            notion_helper.get_relation_id(
                                director['name'],
                                notion_helper.director_database_id,
                                USER_ICON_URL
                            )
                            for director in cast_crew['directors']
                        ]
                else:
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
            parent = {
                "database_id": notion_helper.movie_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover)
            )

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

def search_imdb_by_title(title, year=None):
    """通过电影名称在IMDB搜索"""
    try:
        # 构建搜索URL
        search_query = title
        if year:
            search_query += f" {year}"

        search_url = f"https://www.imdb.com/find?q={requests.utils.quote(search_query)}&s=tt&ttype=ft"
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
        'bio': None
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
                        if 'image' in data and not result['photo']:
                            result['photo'] = data['image']
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

    except Exception as e:
        print(f"  获取人物信息失败 ({person_id}): {str(e)[:50]}")

    return result if (result['name'] or result['photo']) else None

def get_imdb_cast_and_crew(imdb_id):
    """从IMDB获取完整的演员和导演列表"""
    result = {
        'actors': [],
        'directors': []
    }

    try:
        url = f"https://www.imdb.com/title/{imdb_id}/fullcredits"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, features="lxml")

            # 查找所有演员/导演链接
            all_person_links = soup.find_all('a', href=lambda x: x and '/name/nm' in x)

            seen_actors = set()
            seen_directors = set()

            for link in all_person_links:
                name = link.get_text().strip()
                if not name or len(name) < 2:
                    continue

                person_id_match = re.search(r'/name/(nm\d+)', link.get('href', ''))
                if not person_id_match:
                    continue

                person_id = person_id_match.group(1)

                # 通过上下文判断是导演还是演员
                parent_text = ''
                parent = link.find_parent(['div', 'td', 'h4'])
                if parent:
                    parent_text = parent.get_text().lower()

                # 导演通常在页面前面，且有特定标识
                if 'direct' in parent_text and person_id not in seen_directors and len(result['directors']) < 2:
                    result['directors'].append({
                        'name': name,
                        'id': person_id
                    })
                    seen_directors.add(person_id)
                # 演员
                elif person_id not in seen_actors and person_id not in seen_directors and len(result['actors']) < 5:
                    # 跳过一些明显不是演员的
                    if any(keyword in name.lower() for keyword in ['uncredited', 'archive', 'footage']):
                        continue
                    result['actors'].append({
                        'name': name,
                        'id': person_id
                    })
                    seen_actors.add(person_id)

                # 如果已经收集够了，提前退出
                if len(result['actors']) >= 5 and len(result['directors']) >= 2:
                    break

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
                result['poster'] = poster_url

            # 从JSON-LD结构化数据中获取信息
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        # 获取原名
                        if 'name' in data and not result['title']:
                            result['title'] = data['name']
                        # 获取评分
                        if 'aggregateRating' in data:
                            rating_value = data['aggregateRating'].get('ratingValue')
                            if rating_value:
                                result['rating'] = float(rating_value)
                        # 获取海报
                        if 'image' in data and not result['poster']:
                            result['poster'] = data['image']
                except:
                    continue

            # 如果JSON-LD没有找到，尝试其他方法获取标题和评分
            if not result['title']:
                title_tag = soup.find('h1')
                if title_tag:
                    result['title'] = title_tag.get_text().strip()

            if not result['rating']:
                # 尝试查找评分
                rating_tag = soup.find('span', {'class': lambda x: x and 'rating' in str(x).lower()})
                if rating_tag:
                    try:
                        result['rating'] = float(rating_tag.get_text().strip())
                    except:
                        pass

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

def get_imdb_poster(imdb_id):
    """从IMDB获取电影海报（兼容旧接口）"""
    info = get_imdb_info(imdb_id)
    return info['poster'] if info else None

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

def insert_book(douban_name,notion_helper):
    notion_books = notion_helper.query_all(database_id=notion_helper.book_database_id)
    notion_book_dict = {}
    for i in notion_books:
        book = {}
        for key, value in i.get("properties").items():
            book[key] = utils.get_property_value(value)
        notion_book_dict[book.get("Url")] = {
            "Remark": book.get("Remark"),
            "Status": book.get("Status"),
            "Date": book.get("Date"),
            "Rating": book.get("Rating"),
            "Cover": book.get("Cover"),
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
        book["Name"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        book["Date"] = create_time.int_timestamp
        book["Url"] = subject.get("url")
        book["Status"] = book_status.get(result.get("status"))

        # 尝试从Goodreads获取封面
        isbn = subject.get("isbn")
        authors = subject.get("author", [])
        author_name = authors[0] if authors else None
        cover = get_goodreads_cover(book.get("Name"), author=author_name, isbn=isbn)

        # 如果Goodreads获取失败，回退到豆瓣封面
        if not cover:
            cover = subject.get("pic").get("large")
            if cover and not cover.endswith('.webp'):
                cover = cover.rsplit('.', 1)[0] + '.webp'

        book["Cover"] = cover
        if result.get("rating"):
            book["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["Remark"] = result.get("comment")
        if notion_book_dict.get(book.get("Url")):
            notion_movive = notion_book_dict.get(book.get("Url"))
            if (
                notion_movive.get("Cover") is None
                or notion_movive.get("Cover") != book.get("Cover")
                or notion_movive.get("Date") != book.get("Date")
                or notion_movive.get("Remark") != book.get("Remark")
                or notion_movive.get("Status") != book.get("Status")
                or notion_movive.get("Rating") != book.get("Rating")
            ):
                print(f"更新{book.get('Name')}")
                properties = utils.get_properties(book, book_properties_type_dict)
                notion_helper.get_date_relation(properties,create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties
            )

        else:
            print(f"插入{book.get('Name')}")
            book["Intro"] = subject.get("intro")

            # 获取ISBN和作者信息
            isbn = subject.get("isbn")
            authors = subject.get("author", [])
            author_name = authors[0] if authors else None

            # 优先尝试从Goodreads获取封面
            cover = get_goodreads_cover(book.get("Name"), author=author_name, isbn=isbn)

            # 如果Goodreads获取失败，回退到豆瓣封面
            if not cover:
                print(f"  Goodreads封面获取失败，使用豆瓣封面")
                cover = subject.get("pic").get("large")
                if cover and not cover.endswith('.webp'):
                    cover = cover.rsplit('.', 1)[0] + '.webp'

            book["Cover"] = cover

            press = []
            for i in subject.get("press"):
                press.extend(i.split(","))
            book["Publisher"] = press[0:MAX_PUBLISHERS_MULTI_SELECT]
            if result.get("tags"):
                book["Category"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in result.get("tags")[0:MAX_CATEGORIES_RELATION]
                ]
            if subject.get("author"):
                book["Author"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.author_database_id, USER_ICON_URL
                    )
                    for x in subject.get("author")[0:MAX_AUTHORS_RELATION]
                ]
            if subject.get("rating"):
                book["DoubanRating"] = subject.get("rating").get("value", 0)
                book["Raters"] = subject.get("rating").get("count", 0)
            if subject.get("pubdate"):
                for date_str in subject.get("pubdate"):
                    year_match = re.search(r'\d{4}', date_str)
                    if year_match:
                        book["Year"] = year_match.group()
                        break
            properties = utils.get_properties(book, book_properties_type_dict)
            notion_helper.get_date_relation(properties,create_time)
            parent = {
                "database_id": notion_helper.book_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover)
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
