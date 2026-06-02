import httpx
from types import SimpleNamespace

from douban2notion import cover_validator


class FakePages:
    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return {"id": kwargs.get("page_id")}


class FakeClient:
    def __init__(self):
        self.pages = FakePages()
        self.options = SimpleNamespace(auth="secret-token")


class FakeNotionHelper:
    def __init__(self):
        self.client = FakeClient()


class FlakyPages(FakePages):
    def __init__(self, failures_before_success):
        super().__init__()
        self.failures_before_success = failures_before_success

    def update(self, **kwargs):
        if len(self.updates) < self.failures_before_success:
            self.updates.append({"failed": kwargs})
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return super().update(**kwargs)


def test_file_urls_include_notion_hosted_files():
    page = {
        "properties": {
            "Cover": {
                "type": "files",
                "files": [
                    {
                        "type": "file",
                        "file": {"url": "https://s3.us-west-2.amazonaws.com/notion/cover.jpg"},
                    }
                ],
            }
        },
        "icon": {
            "type": "file",
            "file": {"url": "https://s3.us-west-2.amazonaws.com/notion/icon.jpg"},
        },
        "cover": {
            "type": "file",
            "file": {"url": "https://s3.us-west-2.amazonaws.com/notion/page-cover.jpg"},
        },
    }

    assert cover_validator.get_files_url(page, "Cover") == "https://s3.us-west-2.amazonaws.com/notion/cover.jpg"
    assert cover_validator.get_icon_url(page) == "https://s3.us-west-2.amazonaws.com/notion/icon.jpg"
    assert cover_validator.get_cover_url(page) == "https://s3.us-west-2.amazonaws.com/notion/page-cover.jpg"


def test_book_cover_repair_copies_single_valid_slot_to_missing_slots(monkeypatch):
    nh = FakeNotionHelper()
    valid_cover = "https://example.com/valid-cover.jpg"
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "Cover": {"type": "files", "files": [{"type": "external", "external": {"url": valid_cover}}]},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
            "DataIssue": {"type": "multi_select", "multi_select": [{"name": "BrokenCover"}]},
        },
        "icon": None,
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {valid_cover: True})
    monkeypatch.setattr(cover_validator, "_download_image", lambda url: b"image-bytes")
    monkeypatch.setattr(cover_validator, "_notion_upload_binary", lambda token, img_data, filename: "upload-id")

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is True
    media_updates = [
        update for update in nh.client.pages.updates
        if update.get("icon") or update.get("cover")
    ]
    assert media_updates
    assert media_updates[0]["icon"] == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert media_updates[0]["cover"] == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert "properties" not in media_updates[0]


def test_cover_repair_retries_transient_notion_update(monkeypatch):
    nh = FakeNotionHelper()
    nh.client.pages = FlakyPages(failures_before_success=1)
    valid_cover = "https://example.com/valid-cover.jpg"
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "Cover": {"type": "files", "files": [{"type": "external", "external": {"url": valid_cover}}]},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
            "DataIssue": {"type": "multi_select", "multi_select": [{"name": "BrokenCover"}]},
        },
        "icon": None,
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {valid_cover: True})
    monkeypatch.setattr(cover_validator, "_download_image", lambda url: b"image-bytes")
    monkeypatch.setattr(cover_validator, "_notion_upload_binary", lambda token, img_data, filename: "upload-id")
    monkeypatch.setattr(cover_validator.time, "sleep", lambda seconds: None)

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is True
    assert any(update.get("icon") for update in nh.client.pages.updates)


def test_existing_valid_slot_blocks_external_replacement_when_copy_fails(monkeypatch):
    nh = FakeNotionHelper()
    valid_cover = "https://example.com/valid-cover.jpg"
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "ISBN": {"type": "rich_text", "rich_text": []},
            "Cover": {"type": "files", "files": [{"type": "external", "external": {"url": valid_cover}}]},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
            "DataIssue": {"type": "multi_select", "multi_select": [{"name": "BrokenCover"}]},
        },
        "icon": None,
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {valid_cover: True})
    monkeypatch.setattr(cover_validator, "_download_image", lambda url: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("external replacement should not run when an existing slot is valid")

    monkeypatch.setattr(cover_validator, "_get_douban_book_cover_via_upload", fail_if_called)
    monkeypatch.setattr(cover_validator, "_get_book_cover_parallel", fail_if_called)

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is False
    assert nh.client.pages.updates == []


def test_existing_unverified_book_cover_is_not_replaced_by_external_sources(monkeypatch):
    nh = FakeNotionHelper()
    existing_cover = "https://example.com/existing-cover.jpg"
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "ISBN": {"type": "rich_text", "rich_text": []},
            "Author": {"type": "relation", "relation": []},
            "Cover": {"type": "files", "files": [{"type": "external", "external": {"url": existing_cover}}]},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
            "DataIssue": {"type": "multi_select", "multi_select": [{"name": "BrokenCover"}]},
        },
        "icon": None,
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {existing_cover: False})

    def fail_if_called(*args, **kwargs):
        raise AssertionError("external replacement should not run when existing media cannot be verified")

    monkeypatch.setattr(cover_validator, "_get_douban_book_cover_via_upload", fail_if_called)
    monkeypatch.setattr(cover_validator, "_get_book_cover_parallel", fail_if_called)

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is False
    assert all(not update.get("icon") and not update.get("cover") for update in nh.client.pages.updates)
    assert all(
        update.get("properties", {}).get("Cover", {}) == {}
        for update in nh.client.pages.updates
    )


def test_book_cover_repair_marks_missing_when_no_existing_or_external_cover(monkeypatch):
    nh = FakeNotionHelper()
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "ISBN": {"type": "rich_text", "rich_text": []},
            "Author": {"type": "relation", "relation": []},
            "Cover": {"type": "files", "files": []},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
        },
        "icon": None,
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {})
    monkeypatch.setattr(cover_validator, "_get_douban_book_cover_via_upload", lambda nh, page: (None, None))
    monkeypatch.setattr(cover_validator, "_get_book_cover_parallel", lambda *args: (None, None))

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is False
    status_updates = [
        update for update in nh.client.pages.updates
        if update.get("properties", {}).get("CoverStatus")
    ]
    assert status_updates[-1]["properties"]["CoverStatus"] == {"select": {"name": "Missing"}}


def test_book_external_fallback_uploads_instead_of_writing_external_url(monkeypatch):
    nh = FakeNotionHelper()
    page = {
        "id": "book-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试书"}]},
            "ISBN": {"type": "rich_text", "rich_text": []},
            "Author": {"type": "relation", "relation": []},
            "Cover": {"type": "files", "files": []},
            "CoverStatus": {"type": "select", "select": {"name": "Broken"}},
            "CoverCheckedAt": {"type": "date", "date": None},
            "CoverSource": {"type": "select", "select": {"name": "Manual"}},
        },
        "icon": None,
        "cover": None,
    }
    external_cover = "https://covers.openlibrary.org/b/id/1-L.jpg"
    captured = {}

    def fake_patch(url, json, headers, timeout):
        captured["json"] = json
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {})
    monkeypatch.setattr(cover_validator, "_get_douban_book_cover_via_upload", lambda nh, page: (None, None))
    monkeypatch.setattr(cover_validator, "_get_book_cover_parallel", lambda *args: (external_cover, "OpenLibrary"))
    monkeypatch.setattr(cover_validator, "_download_image", lambda url: b"image-bytes")
    monkeypatch.setattr(cover_validator, "_notion_upload_binary", lambda token, img_data, filename: "upload-id")
    monkeypatch.setattr(cover_validator.requests, "patch", fake_patch)

    checked, fixed = cover_validator._validate_single_book_cover(nh, page)

    assert checked is True
    assert fixed is True
    assert captured["json"]["properties"]["Cover"]["files"][0] == {
        "type": "file_upload",
        "file_upload": {"id": "upload-id"},
        "name": "Cover",
    }
    assert captured["json"]["properties"]["CoverSource"] == {"select": {"name": "OpenLibrary"}}
    assert not nh.client.pages.updates


def test_person_photo_repair_copies_valid_icon_to_photo_and_cover(monkeypatch):
    nh = FakeNotionHelper()
    valid_icon = "https://example.com/valid-person.jpg"
    page = {
        "id": "person-page",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "测试人"}]},
            "Photo": {"type": "files", "files": []},
            "PhotoStatus": {"type": "select", "select": {"name": "Missing"}},
            "PhotoCheckedAt": {"type": "date", "date": None},
            "PhotoSource": {"type": "select", "select": {"name": "Manual"}},
            "DataIssue": {"type": "multi_select", "multi_select": [{"name": "MissingPhoto"}]},
        },
        "icon": {"type": "external", "external": {"url": valid_icon}},
        "cover": None,
    }

    monkeypatch.setattr(cover_validator, "batch_validate_urls", lambda urls, max_workers=3: {valid_icon: True})
    monkeypatch.setattr(cover_validator, "_download_image", lambda url: b"image-bytes")
    monkeypatch.setattr(cover_validator, "_notion_upload_binary", lambda token, img_data, filename: "upload-id")

    checked, fixed = cover_validator._validate_single_person_photo(
        nh,
        page,
        has_photo_property=True,
        has_imdb_property=False,
        imdb_enabled=False,
    )

    assert checked is True
    assert fixed is True
    media_updates = [
        update for update in nh.client.pages.updates
        if update.get("icon") or update.get("cover")
    ]
    assert media_updates
    assert media_updates[0]["cover"] == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert media_updates[0]["properties"]["Photo"]["files"][0] == {
        "type": "file_upload",
        "file_upload": {"id": "upload-id"},
        "name": "Photo",
    }


def test_douban_upload_sets_property_as_file_upload(monkeypatch):
    captured = {}

    def fake_patch(url, json, headers, timeout):
        captured["json"] = json
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(cover_validator.requests, "patch", fake_patch)

    ok = cover_validator._notion_set_cover_upload(
        "secret-token",
        "page-id",
        "upload-id",
        "https://img1.doubanio.com/view/subject/raw/public/p1.jpg",
        "Cover",
        "CoverStatus",
        "CoverCheckedAt",
        "CoverSource",
    )

    assert ok is True
    assert captured["json"]["icon"] == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert captured["json"]["cover"] == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert captured["json"]["properties"]["Cover"]["files"][0] == {
        "type": "file_upload",
        "file_upload": {"id": "upload-id"},
        "name": "Cover",
    }
