
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

book_properties_type_dict = {
    "Name":TITLE,
    "Short Comment":RICH_TEXT,
    "ISBN":RICH_TEXT,
    "Url":URL,
    "Author":RELATION,
    "Rating":SELECT,
    "Cover":FILES,
    "Category":RELATION,
    "Status":STATUS,
    "Date":DATE,
    "Intro":RICH_TEXT,
    "Publisher":MULTI_SELECT,
    "Year":SELECT,
    "DoubanRating":NUMBER,
    "Raters":NUMBER
}

TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
BOOK_ICON_URL = "https://www.notion.so/icons/book_gray.svg"


movie_properties_type_dict = {
    "Name":TITLE,
    "Short Comment":RICH_TEXT,
    # "ISBN":RICH_TEXT,
    # "链接":URL,
    "Director":RELATION,
    "Actors":MULTI_SELECT,
    "Actor":RELATION,
    # "Sort":NUMBER,
    "Cover":FILES,
    "Category":RELATION,
    "Status":STATUS,
    "Medium":SELECT,
    "Rating":SELECT,
    # "阅读时长":NUMBER,
    # "阅读进度":NUMBER,
    # "阅读天数":NUMBER,
    "Date":DATE,
    "Intro":RICH_TEXT,
    "DoubanRating":NUMBER,
    "Year":SELECT,
    # "开始阅读时间":DATE,
    # "最后阅读时间":DATE,
    # "简介":RICH_TEXT,
    # "书架分类":SELECT,
    # "我的评分":SELECT,
    "Url":URL,
}
