import argparse
from email import feedparser
import json
import os
import re
import pendulum
from retrying import retry
import requests
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils
DOUBAN_API_HOST = os.getenv("DOUBAN_API_HOST", "frodo.douban.com")
DOUBAN_API_KEY = os.getenv("DOUBAN_API_KEY", "0ac44ae016490db2204ce0a042db2916")

from douban2notion.config import movie_properties_type_dict,book_properties_type_dict, TAG_ICON_URL, USER_ICON_URL
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
            "page_id": i.get("id")
        }
    print(f"notion {len(notion_movie_dict)}")
    results = []
    for i in movie_status.keys():
        results.extend(fetch_subjects(douban_name, "movie", i))
    for result in results:
        movie = {}
        if not result:
            print(result)
            continue
        subject = result.get("subject")
        movie["Name"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        movie["Date"] = create_time.int_timestamp
        movie["Url"] = subject.get("url")
        movie["Status"] = movie_status.get(result.get("status"))
        movie["DoubanRating"] = subject.get("rating", {}).get("value", 0) if subject.get("rating") else 0
        # movie["豆瓣评分人数"] = subject.get("rating", {}).get("count", 0) if subject.get("rating") else 0
        # 验证必要字段
        if not subject.get("title") or subject.get("title") == "未知电影":
            print(f"跳过无效电影: {subject.get('title')}")
            continue
        if not subject.get("year"):
            print(f"跳过无年份电影: {subject.get('title')}")
            continue
        movie["Year"] = subject.get("year")
        if result.get("rating"):
            movie["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            movie["Remark"] = result.get("comment")
        if notion_movie_dict.get(movie.get("Url")):
            notion_movive = notion_movie_dict.get(movie.get("Url"))
            if (
                notion_movive.get("Date") != movie.get("Date")
                or notion_movive.get("Remark") != movie.get("Remark")
                or notion_movive.get("Status") != movie.get("Status")
                or notion_movive.get("Rating") != movie.get("Rating")
            ):
                properties = utils.get_properties(movie, movie_properties_type_dict)
                #notion_helper.get_date_relation(properties,create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties
            )

        else:
            print(f"插入{movie.get('Name')}")
            cover = subject.get("pic").get("normal")
            if not cover.endswith('.webp'):
                cover = cover.rsplit('.', 1)[0] + '.webp'
            movie["Cover"] = cover
            movie["Medium"] = subject.get("type").upper() if subject.get("type") else None
            if subject.get("genres"):
                movie["Category"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in subject.get("genres")
                ]
            if subject.get("actors"):
                l = []
                actors = subject.get("actors")[0:100]
                for actor in actors:
                    if actor.get("name"):
                        if "/" in actor.get("name"):
                            l.extend(actor.get("name").split("/"))
                        else:
                            l.append(actor.get("name"))  
                movie["Actors"] = l
                movie["Actor"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                    )
                    for x in subject.get("actors")[0:100]
                ]
            if subject.get("directors"):
                movie["Director"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                    )
                    for x in subject.get("directors")[0:100]
                ]
            properties = utils.get_properties(movie, movie_properties_type_dict)
            #notion_helper.get_date_relation(properties,create_time)
            parent = {
                "database_id": notion_helper.movie_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(cover)
            )


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
            "page_id": i.get("id")
        }
    print(f"notion {len(notion_book_dict)}")
    results = []
    for i in book_status.keys():
        results.extend(fetch_subjects(douban_name, "book", i))
    for result in results:
        print(result)
        book = {}
        subject = result.get("subject")
        book["Name"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        book["Date"] = create_time.int_timestamp
        book["Url"] = subject.get("url")
        book["Status"] = book_status.get(result.get("status"))
        book["DoubanRating"] = subject.get("rating", {}).get("value", 0) if subject.get("rating") else 0
        book["Raters"] = subject.get("rating", {}).get("count", 0) if subject.get("rating") else 0

        # 验证必要字段
        if not subject.get("title") or subject.get("title") == "未知电影":
            print(f"跳过无效书籍: {subject.get('title')}")
            continue
        # 从 pubdate 中提取年份
        pubdate = subject.get("pubdate", [])
        if pubdate and len(pubdate) > 0:
            # 提取第一个日期中的年份
            year = pubdate[0].split("-")[0] if "-" in pubdate[0] else pubdate[0].split(".")[0]
            book["Year"] = year
        if result.get("rating"):
            book["Rating"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["Remark"] = result.get("comment")
        if notion_book_dict.get(book.get("Url")):
            notion_movive = notion_book_dict.get(book.get("Url"))
            if (
                notion_movive.get("Date") != book.get("Date")
                or notion_movive.get("Remark") != book.get("Remark")
                or notion_movive.get("Status") != book.get("Status")
                or notion_movive.get("Rating") != book.get("Rating")
            ):
                properties = utils.get_properties(book, book_properties_type_dict)
                #notion_helper.get_date_relation(properties,create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties
            )

        else:
            print(f"插入{book.get('Name')}")
            cover = subject.get("pic").get("large")
            # 处理图片链接，确保使用 large 尺寸的图片
            if cover:
                # 替换图片链接中的尺寸参数
                cover = cover.replace("/l/", "/l/")  # 确保使用大图
                # 移除任何查询参数
                cover = cover.split("?")[0]
                # 确保链接以 .jpg 结尾
                if not cover.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    cover = cover + '.jpg'
            book["Cover"] = cover
            book["Intro"] = subject.get("intro")
            press = []
            for i in subject.get("press"):
                press.extend(i.split(","))
            book["Publisher"] = press
            book["类型"] = subject.get("type")
            if result.get("tags"):
                book["Category"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in result.get("tags")
                ]
            if subject.get("author"):
                book["Author"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.author_database_id, USER_ICON_URL
                    )
                    for x in subject.get("author")[0:100]
                ]
            properties = utils.get_properties(book, book_properties_type_dict)
            #notion_helper.get_date_relation(properties,create_time)
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
    # notion_helper = NotionHelper(type)
    # is_movie = True if type=="movie" else False
    # douban_name = os.getenv("DOUBAN_NAME", None)
    # if is_movie:
    #     insert_movie(douban_name,notion_helper)
    # else:
    #     insert_book(douban_name,notion_helper)
    try:
        notion_helper = NotionHelper(type)
        is_movie = True if type=="movie" else False
        douban_name = os.getenv("DOUBAN_NAME", None)
        if not douban_name:
            raise ValueError("DOUBAN_NAME environment variable is required")
            
        if is_movie:
            insert_movie(douban_name,notion_helper)
        else:
            insert_book(douban_name,notion_helper)
    except ValueError as e:
        print(f"错误: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"发生错误: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
