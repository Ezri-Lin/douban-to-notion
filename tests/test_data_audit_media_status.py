import httpx

from douban2notion import data_audit


def test_data_audit_file_urls_include_notion_hosted_files():
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

    assert data_audit._get_files_url(page, "Cover") == "https://s3.us-west-2.amazonaws.com/notion/cover.jpg"
    assert data_audit._get_icon_url(page) == "https://s3.us-west-2.amazonaws.com/notion/icon.jpg"
    assert data_audit._get_cover_url(page) == "https://s3.us-west-2.amazonaws.com/notion/page-cover.jpg"


def test_media_issue_uses_any_valid_url(monkeypatch):
    monkeypatch.setattr(
        data_audit,
        "_is_valid_image_url",
        lambda url, check_remote: url == "https://example.com/valid.jpg",
    )

    issue = data_audit._media_issue_for_urls(
        [
            "https://example.com/valid.jpg",
            "https://example.com/broken.jpg",
        ],
        check_remote=True,
        missing_issue=data_audit.ISSUE_MISSING_COVER,
        broken_issue=data_audit.ISSUE_BROKEN_COVER,
    )

    assert issue is None


def test_media_issue_is_broken_when_all_urls_fail(monkeypatch):
    monkeypatch.setattr(data_audit, "_is_valid_image_url", lambda url, check_remote: False)

    issue = data_audit._media_issue_for_urls(
        ["https://example.com/broken.jpg"],
        check_remote=True,
        missing_issue=data_audit.ISSUE_MISSING_COVER,
        broken_issue=data_audit.ISSUE_BROKEN_COVER,
    )

    assert issue == data_audit.ISSUE_BROKEN_COVER


class _FlakyPages:
    def __init__(self, failures_before_success):
        self.failures_before_success = failures_before_success
        self.calls = 0

    def update(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return {"id": kwargs.get("page_id")}


class _DummyClient:
    def __init__(self, pages):
        self.pages = pages


def test_update_page_properties_retries_transient_notion_disconnect(monkeypatch):
    pages = _FlakyPages(failures_before_success=1)
    client = _DummyClient(pages)
    monkeypatch.setattr(data_audit.time, "sleep", lambda seconds: None)

    updated = data_audit._update_page_properties(
        client,
        {"id": "page-id"},
        {"CoverStatus": {"select": {"name": "Ok"}}},
    )

    assert updated is True
    assert pages.calls == 2


def test_update_page_properties_does_not_abort_after_repeated_disconnects(monkeypatch):
    pages = _FlakyPages(failures_before_success=10)
    client = _DummyClient(pages)
    monkeypatch.setattr(data_audit.time, "sleep", lambda seconds: None)

    updated = data_audit._update_page_properties(
        client,
        {"id": "page-id"},
        {"CoverStatus": {"select": {"name": "Ok"}}},
    )

    assert updated is False
    assert pages.calls == data_audit.NOTION_UPDATE_MAX_ATTEMPTS
