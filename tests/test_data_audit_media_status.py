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
