import logging
import os
import re
import time
import requests

from notion_client import Client
from notion_client.errors import APIResponseError
from retrying import retry

from douban2notion.utils import (
    format_date,
    get_date,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_year,
    get_icon,
    get_rich_text,
    get_relation,
    get_title,
)

TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"
BOOKMARK_ICON_URL = "https://www.notion.so/icons/bookmark_gray.svg"


class NotionHelper:
    database_name_dict = {
        "MOVIE_DATABASE_NAME": "Movie",
        "BOOK_DATABASE_NAME": "书架",
        "DAY_DATABASE_NAME": "日",
        "WEEK_DATABASE_NAME": "周",
        "MONTH_DATABASE_NAME": "月",
        "YEAR_DATABASE_NAME": "年",
        "CATEGORY_DATABASE_NAME": "CATEGORY",
        "DIRECTOR_DATABASE_NAME": "Director",
        "AUTHOR_DATABASE_NAME": "作者",
        "ACTOR_DATABASE_NAME": "Actor",
    }
    database_id_dict = {}
    image_dict = {}
    __url_validity_cache = {}

    def __init__(self, type):
        is_movie = True if type == "movie" else False
        page_url = os.getenv("NOTION_MOVIE_URL") if is_movie else os.getenv("NOTION_BOOK_URL")
        notion_token = os.getenv("NOTION_TOKEN")
        if not notion_token:
            if is_movie:
                notion_token = os.getenv("MOVIE_NOTION_TOKEN")
            else:
                notion_token = os.getenv("BOOK_NOTION_TOKEN")
        self.client = Client(auth=notion_token, log_level=logging.ERROR)
        self.__cache = {}
        self.__db_schema_cache = {}
        self.page_id = self.extract_page_id(page_url)
        self.search_database(self.page_id)
        for key in self.database_name_dict.keys():
            if os.getenv(key) != None and os.getenv(key) != "":
                self.database_name_dict[key] = os.getenv(key)
        self.book_database_id = self.database_id_dict.get(
            self.database_name_dict.get("BOOK_DATABASE_NAME")
        )
        self.movie_database_id = self.database_id_dict.get(
            self.database_name_dict.get("MOVIE_DATABASE_NAME")
        )
        self.day_database_id = self.database_id_dict.get(
            self.database_name_dict.get("DAY_DATABASE_NAME")
        )
        self.week_database_id = self.database_id_dict.get(
            self.database_name_dict.get("WEEK_DATABASE_NAME")
        )
        self.month_database_id = self.database_id_dict.get(
            self.database_name_dict.get("MONTH_DATABASE_NAME")
        )
        self.year_database_id = self.database_id_dict.get(
            self.database_name_dict.get("YEAR_DATABASE_NAME")
        )
        self.category_database_id = self.database_id_dict.get(
            self.database_name_dict.get("CATEGORY_DATABASE_NAME")
        )
        self.director_database_id = self.database_id_dict.get(
            self.database_name_dict.get("DIRECTOR_DATABASE_NAME")
        )
        self.author_database_id = self.database_id_dict.get(
            self.database_name_dict.get("AUTHOR_DATABASE_NAME")
        )
        self.actor_database_id = self.database_id_dict.get(
            self.database_name_dict.get("ACTOR_DATABASE_NAME")
        )
        if self.day_database_id:
            self.write_database_id(self.day_database_id)

    def write_database_id(self, database_id):
        env_file = os.getenv('GITHUB_ENV')
        with open(env_file, "a") as file:
            file.write(f"DATABASE_ID={database_id}\n")

    def extract_page_id(self, notion_url):
        match = re.search(
            r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            notion_url,
        )
        if match:
            return match.group(0)
        else:
            raise Exception(f"获取NotionID失败，请检查输入的Url是否正确")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def search_database(self, block_id):
        children = self.client.blocks.children.list(block_id=block_id)["results"]
        for child in children:
            if child["type"] == "child_database":
                self.database_id_dict[
                    child.get("child_database").get("title")
                ] = child.get("id")
            elif child["type"] == "embed" and child.get("embed").get("url"):
                if child.get("embed").get("url").startswith("https://heatmap.malinkang.com/"):
                    self.heatmap_block_id = child.get("id")
            if "has_children" in child and child["has_children"]:
                self.search_database(child["id"])

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_heatmap(self, block_id, url):
        return self.client.blocks.update(block_id=block_id, embed={"url": url})

    def get_database_schema(self, database_id):
        if database_id in self.__db_schema_cache:
            return self.__db_schema_cache[database_id]
        schema = self.client.databases.retrieve(database_id=database_id)
        self.__db_schema_cache[database_id] = schema
        return schema

    def has_select_option(self, database_id, property_name, option_name):
        if not option_name:
            return False
        schema = self.get_database_schema(database_id)
        properties = schema.get("properties", {})
        prop = properties.get(property_name)
        if not prop or prop.get("type") != "select":
            return False
        options = prop.get("select", {}).get("options", [])
        option_names = {x.get("name") for x in options}
        return option_name in option_names

    def ensure_select_option(self, database_id, property_name, option_name):
        """确保 select 字段存在指定选项；失败时返回 False，不抛错。"""
        if not option_name:
            return False
        schema = self.get_database_schema(database_id)
        properties = schema.get("properties", {})
        prop = properties.get(property_name)
        if not prop or prop.get("type") != "select":
            return False

        options = prop.get("select", {}).get("options", [])
        if any(x.get("name") == option_name for x in options):
            return True

        try:
            new_options = [{"name": x.get("name"), "color": x.get("color", "default")} for x in options]
            new_options.append({"name": option_name, "color": "default"})
            self.client.databases.update(
                database_id=database_id,
                properties={
                    property_name: {
                        "select": {
                            "options": new_options
                        }
                    }
                },
            )
            self.__db_schema_cache.pop(database_id, None)
            return True
        except APIResponseError as e:
            print(f"  无法为 {property_name} 添加选项 {option_name}: {e}")
            return False

    def get_week_relation_id(self, date):
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(week, self.week_database_id, TARGET_ICON_URL, properties)

    def get_month_relation_id(self, date):
        month = date.strftime("%Y年%-m月")
        start, end = get_first_and_last_day_of_month(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(month, self.month_database_id, TARGET_ICON_URL, properties)

    def get_year_relation_id(self, date):
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(year, self.year_database_id, TARGET_ICON_URL, properties)

    def get_day_relation_id(self, date):
        new_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day = new_date.strftime("%Y年%m月%d日")
        properties = {"日期": get_date(format_date(date))}
        properties["年"] = get_relation([self.get_year_relation_id(new_date)])
        properties["月"] = get_relation([self.get_month_relation_id(new_date)])
        properties["周"] = get_relation([self.get_week_relation_id(new_date)])
        return self.get_relation_id(day, self.day_database_id, TARGET_ICON_URL, properties)

    def _is_valid_image_url(self, url):
        if not url:
            return False
        if url in self.__url_validity_cache:
            return self.__url_validity_cache[url]
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
        self.__url_validity_cache[url] = ok
        return ok

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_relation_id(self, name, id, icon, properties=None, person_info=None):
        """获取或创建关系实体的ID（Actor/Director/Category 等）"""
        if properties is None:
            properties = {}
        key = f"{id}{name}"
        if key in self.__cache:
            return self.__cache.get(key)
        imdb_id = (person_info or {}).get("imdb_id")
        db_properties = (self.get_database_schema(id).get("properties") or {})

        # Prefer stable identity matching by IMDB id to avoid same-name collisions.
        response = {"results": []}
        if imdb_id and "IMDB" in db_properties:
            imdb_filter = {"property": "IMDB", "rich_text": {"equals": imdb_id}}
            response = self.client.databases.query(database_id=id, filter=imdb_filter)
        if len(response.get("results")) == 0:
            name_filter = {"property": "Name", "title": {"equals": name}}
            response = self.client.databases.query(database_id=id, filter=name_filter)
        if len(response.get("results")) == 0:
            parent = {"database_id": id, "type": "database_id"}
            properties["Name"] = get_title(name)

            if person_info:
                now_ts = int(time.time())
                photo_url = person_info.get('photo')
                photo_ok = self._is_valid_image_url(photo_url) if photo_url else False
                photo_source = person_info.get('photo_source')
                if photo_source == "TMDB":
                    photo_source = "IMDB"
                # Temporarily disable C-Name/Nation auto-fill; use Notion AI/manual curation.
                if photo_ok and "Photo" in db_properties:
                    properties["Photo"] = {
                        "files": [{"type": "external", "name": "Photo", "external": {"url": photo_url}}]
                    }
                if "PhotoStatus" in db_properties:
                    properties["PhotoStatus"] = {"select": {"name": "Ok" if photo_ok else "Missing"}}
                if "PhotoCheckedAt" in db_properties:
                    properties["PhotoCheckedAt"] = {
                        "date": {
                            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts)),
                            "time_zone": "Asia/Shanghai",
                        }
                    }
                if photo_source and "PhotoSource" in db_properties:
                    properties["PhotoSource"] = {"select": {"name": photo_source}}
                if person_info.get('imdb_id') and "IMDB" in db_properties:
                    properties["IMDB"] = get_rich_text(person_info['imdb_id'])
                if person_info.get('imdb_id') and "IMDB_Url" in db_properties:
                    properties["IMDB_Url"] = {"url": f"https://www.imdb.com/name/{person_info['imdb_id']}/"}
                if person_info.get('bio') and "Bio" in db_properties:
                    properties["Bio"] = get_rich_text(person_info['bio'])

            page_icon = get_icon(icon) if icon else None
            page_cover = None
            if person_info and photo_ok:
                page_icon = get_icon(photo_url)
                page_cover = get_icon(photo_url)

            create_params = {"parent": parent, "properties": properties}
            if page_icon:
                create_params["icon"] = page_icon
            if page_cover:
                create_params["cover"] = page_cover

            page_id = self.client.pages.create(**create_params).get("id")
        else:
            page_id = response.get("results")[0].get("id")
            if person_info:
                self._update_person_page_if_needed(page_id, person_info)
        self.__cache[key] = page_id
        return page_id

    def _update_person_page_if_needed(self, page_id, person_info):
        page = self.client.pages.retrieve(page_id=page_id)
        page_properties = page.get("properties", {})
        update_properties = {}
        update_page_payload = {"page_id": page_id}
        now_ts = int(time.time())
        photo_source = person_info.get("photo_source")
        if photo_source == "TMDB":
            photo_source = "IMDB"

        photo = person_info.get("photo")
        photo_ok = self._is_valid_image_url(photo) if photo else False
        if photo and "Photo" in page_properties:
            current_photo = page_properties["Photo"].get("files", [])
            current_photo_url = None
            if current_photo:
                current_photo_url = (current_photo[0].get("external") or {}).get("url")
            if photo_ok and ((not current_photo_url) or (not self._is_valid_image_url(current_photo_url))):
                update_properties["Photo"] = {
                    "files": [{"type": "external", "name": "Photo", "external": {"url": photo}}]
                }
        if "PhotoStatus" in page_properties:
            current_status = (page_properties["PhotoStatus"].get("select") or {}).get("name")
            new_status = "Ok" if photo_ok else "Missing"
            if current_status != new_status:
                update_properties["PhotoStatus"] = {"select": {"name": new_status}}
        if "PhotoCheckedAt" in page_properties:
            update_properties["PhotoCheckedAt"] = {
                "date": {
                    "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts)),
                    "time_zone": "Asia/Shanghai",
                }
            }
        if photo_source and "PhotoSource" in page_properties:
            current_source = (page_properties["PhotoSource"].get("select") or {}).get("name")
            if current_source != photo_source:
                update_properties["PhotoSource"] = {"select": {"name": photo_source}}

        if photo_ok:
            icon_url = ((page.get("icon") or {}).get("external") or {}).get("url")
            cover_url = ((page.get("cover") or {}).get("external") or {}).get("url")
            if (not icon_url) or (not self._is_valid_image_url(icon_url)):
                update_page_payload["icon"] = get_icon(photo)
            if (not cover_url) or (not self._is_valid_image_url(cover_url)):
                update_page_payload["cover"] = get_icon(photo)

        imdb_id = person_info.get("imdb_id")
        if imdb_id and "IMDB" in page_properties:
            current_imdb = page_properties["IMDB"].get("rich_text", [])
            current_imdb = current_imdb[0].get("plain_text") if current_imdb else None
            if not current_imdb:
                update_properties["IMDB"] = get_rich_text(imdb_id)
        if imdb_id and "IMDB_Url" in page_properties:
            current_imdb_url = page_properties["IMDB_Url"].get("url")
            imdb_url = f"https://www.imdb.com/name/{imdb_id}/"
            if current_imdb_url != imdb_url:
                update_properties["IMDB_Url"] = {"url": imdb_url}

        bio = person_info.get("bio")
        if bio and "Bio" in page_properties:
            current_bio = page_properties["Bio"].get("rich_text", [])
            current_bio = current_bio[0].get("plain_text") if current_bio else None
            if not current_bio:
                update_properties["Bio"] = get_rich_text(bio)

        if update_properties:
            update_page_payload["properties"] = update_properties
            self.client.pages.update(**update_page_payload)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_page(self, page_id, properties, icon=None):
        update_data = {"page_id": page_id, "properties": properties}
        if icon:
            update_data["icon"] = icon
        return self.client.pages.update(**update_data)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page(self, parent, properties, icon):
        try:
            if icon:
                return self.client.pages.create(parent=parent, properties=properties, icon=icon)
            else:
                return self.client.pages.create(parent=parent, properties=properties)
        except Exception as e:
            print(f"创建页面失败: {str(e)}")
            print(f"Parent: {parent}")
            print(f"Properties: {list(properties.keys())}")
            raise e

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query(self, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v}
        return self.client.databases.query(**kwargs)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_block_children(self, id):
        response = self.client.blocks.children.list(id)
        return response.get("results")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks(self, block_id, children):
        return self.client.blocks.children.append(block_id=block_id, children=children)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks_after(self, block_id, children, after):
        return self.client.blocks.children.append(
            block_id=block_id, children=children, after=after
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def delete_block(self, block_id):
        return self.client.blocks.delete(block_id=block_id)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all(self, database_id):
        """获取database中所有的数据"""
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    def get_date_relation(self, properties, date):
        pass
