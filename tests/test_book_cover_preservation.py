from types import SimpleNamespace

from douban2notion import douban


class FakeBookHelper:
    def __init__(self):
        self.client = SimpleNamespace(options=SimpleNamespace(auth="secret-token"))


def test_preserve_existing_book_cover_when_status_is_ok():
    book = {
        "Cover": "https://example.com/new-cover.jpg",
        "CoverStatus": "Ok",
        "CoverSource": "Douban",
    }
    existing_book = {
        "Cover": "https://example.com/manual-cover.jpg",
        "CoverStatus": "Ok",
        "CoverSource": "Manual",
    }

    preserved = douban._preserve_existing_book_cover_if_valid(book, existing_book)

    assert preserved is True
    assert book["Cover"] == existing_book["Cover"]
    assert book["CoverStatus"] == "Ok"
    assert book["CoverSource"] == "Manual"


def test_do_not_preserve_invalid_existing_book_cover(monkeypatch):
    monkeypatch.setattr(douban, "_is_valid_image_url", lambda url: False)
    book = {
        "Cover": "https://example.com/new-cover.jpg",
        "CoverStatus": "Ok",
        "CoverSource": "Douban",
    }
    existing_book = {
        "Cover": "https://example.com/old-broken-cover.jpg",
        "CoverStatus": "Broken",
        "CoverSource": "Manual",
    }

    preserved = douban._preserve_existing_book_cover_if_valid(book, existing_book)

    assert preserved is False
    assert book["Cover"] == "https://example.com/new-cover.jpg"
    assert book["CoverSource"] == "Douban"


def test_uploaded_book_cover_replaces_external_payload(monkeypatch):
    helper = FakeBookHelper()
    cover_url = "https://img1.doubanio.com/view/subject/raw/public/p1.jpg"
    book = {"Name": "测试书", "Cover": cover_url}
    properties = {
        "Cover": {"files": [{"type": "external", "name": "Cover", "external": {"url": cover_url}}]},
        "CoverStatus": {"select": {"name": "Ok"}},
    }

    monkeypatch.setattr(douban, "_download_image_for_notion_upload", lambda url: b"image-bytes")
    monkeypatch.setattr(douban, "_notion_upload_binary", lambda token, img_data, filename: "upload-id")

    icon, cover = douban._apply_uploaded_book_cover_to_properties(properties, helper, book)

    assert icon == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert cover == {"type": "file_upload", "file_upload": {"id": "upload-id"}}
    assert properties["Cover"]["files"][0] == {
        "type": "file_upload",
        "file_upload": {"id": "upload-id"},
        "name": "Cover",
    }
    assert "external" not in properties["Cover"]["files"][0]


def test_failed_book_cover_upload_drops_external_payload(monkeypatch):
    helper = FakeBookHelper()
    cover_url = "https://img1.doubanio.com/view/subject/raw/public/p1.jpg"
    book = {"Name": "测试书", "Cover": cover_url}
    properties = {
        "Cover": {"files": [{"type": "external", "name": "Cover", "external": {"url": cover_url}}]},
        "CoverStatus": {"select": {"name": "Ok"}},
    }

    monkeypatch.setattr(douban, "_download_image_for_notion_upload", lambda url: None)

    icon, cover = douban._apply_uploaded_book_cover_to_properties(properties, helper, book)

    assert icon is None
    assert cover is None
    assert "Cover" not in properties
    assert properties["CoverStatus"] == {"select": {"name": "Broken"}}
