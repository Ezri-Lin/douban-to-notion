from douban2notion import douban


def test_extract_celebrity_urls_keeps_directors_and_actors_separate():
    html = """
    <section>
      <h4>导演</h4>
      <span class="attrs">
        <a href="/celebrity/1111111/">导演甲</a>
      </span>
      <h4>主演</h4>
      <span class="attrs">
        <a href="/celebrity/2222222/">演员乙</a>
      </span>
    </section>
    """

    urls = douban._extract_celebrity_urls_from_html(html)

    assert urls["directors"] == [
        {
            "name": "导演甲",
            "url": "https://movie.douban.com/celebrity/1111111/",
        }
    ]
    assert urls["actors"] == [
        {
            "name": "演员乙",
            "url": "https://movie.douban.com/celebrity/2222222/",
        }
    ]


def test_douban_person_payload_does_not_bind_unmatched_imdb_by_position(monkeypatch):
    douban_photo = "https://img1.doubanio.com/view/celebrity/raw/public/p111.jpg"
    wrong_positional_photo = "https://img1.doubanio.com/view/celebrity/raw/public/p222.jpg"

    monkeypatch.setattr(douban, "get_tmdb_person_photo_by_name", lambda name: None)
    monkeypatch.setattr(douban, "_scrape_douban_celebrity_photo", lambda url: douban_photo)

    payload = douban._build_douban_person_info_payload(
        "张三",
        [
            {
                "name": "Wrong Person",
                "id": "nm0000001",
                "photo": wrong_positional_photo,
                "photo_source": "TMDB",
            }
        ],
        0,
        "https://movie.douban.com/celebrity/1111111/",
    )

    assert payload["imdb_id"] is None
    assert payload["photo"] == douban_photo
    assert payload["photo"] != wrong_positional_photo
    assert payload["photo_source"] == "Douban"
    assert payload["c_name"] == "张三"
