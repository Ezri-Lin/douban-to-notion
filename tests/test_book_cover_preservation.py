from douban2notion import douban


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
