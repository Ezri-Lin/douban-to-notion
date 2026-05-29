
import os

RICH_TEXT = "rich_text"
URL = "url"
RELATION = "relation"
NUMBER = "number"
DATE = "date"
FILES = "files"
STATUS = "status"
TITLE = "title"
SELECT = "select"
MULTI_SELECT = "multi_select"

# 并发配置
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
MAX_URL_WORKERS = int(os.getenv("URL_VALIDATION_WORKERS", "5"))
COVER_FETCH_WORKERS = int(os.getenv("COVER_FETCH_WORKERS", "5"))

book_properties_type_dict = {
    "Name":TITLE,
    "Remark":RICH_TEXT,
    "ISBN":RICH_TEXT,
    "ISBN_13":RICH_TEXT,
    "DB_Url":URL,
    "GD_Url":URL,
    "Author":RELATION,
    "Rating":SELECT,
    "Cover":FILES,
    "CoverStatus":SELECT,
    "CoverCheckedAt":DATE,
    "CoverSource":SELECT,
    "Category":RELATION,
    "Status":STATUS,
    "Date":DATE,
    "Intro":RICH_TEXT,
    "Publisher":MULTI_SELECT,
    "Year":SELECT,
    "DoubanRating":NUMBER,
    "Raters":NUMBER,
    "DataIssue": MULTI_SELECT,
}

TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
BOOK_ICON_URL = "https://www.notion.so/icons/book_gray.svg"

MAX_ACTORS_RELATION = 5
MAX_DIRECTORS_RELATION = 2
MAX_CATEGORIES_RELATION = 3
MAX_AUTHORS_RELATION = 3
MAX_PUBLISHERS_MULTI_SELECT = 3

# Actor/Director数据库字段定义
actor_properties_type_dict = {
    "Name": TITLE,  # 人物名字
    "C-Name": RICH_TEXT,  # 中文译名
    "Photo": FILES,  # 人物照片
    "PhotoStatus": SELECT,
    "PhotoCheckedAt": DATE,
    "PhotoSource": SELECT,
    "Nation": SELECT,  # 国籍
    "Bio": RICH_TEXT,  # 简介
    "IMDB": RICH_TEXT,  # IMDB编号
    "IMDB_Url": URL,
    "DataIssue": MULTI_SELECT,
}

director_properties_type_dict = {
    "Name": TITLE,
    "C-Name": RICH_TEXT,
    "Photo": FILES,
    "PhotoStatus": SELECT,
    "PhotoCheckedAt": DATE,
    "PhotoSource": SELECT,
    "Nation": SELECT,
    "Bio": RICH_TEXT,
    "IMDB": RICH_TEXT,
    "IMDB_Url": URL,
    "DataIssue": MULTI_SELECT,
}


movie_properties_type_dict = {
    "Name": TITLE,
    "MovieName": RICH_TEXT,
    "Remark": RICH_TEXT,
    "Director": RELATION,
    "Actor": RELATION,
    "Cover": FILES,
    "CoverStatus": SELECT,
    "CoverCheckedAt": DATE,
    "CoverSource": SELECT,
    "Category": RELATION,
    "Status": STATUS,
    "Medium": SELECT,
    "Rating": SELECT,
    "Date": DATE,
    "Intro": RICH_TEXT,
    "DoubanRating": NUMBER,
    "IMDBRating": NUMBER,
    "Year": SELECT,
    "Season": SELECT,
    "IMDB": RICH_TEXT,
    "IMDB_Url": URL,
    "DB_Url": URL,
    "DataIssue": MULTI_SELECT,
}
