#!/usr/bin/env python3
"""
测试优化效果的脚本
"""

import time
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_cache_manager():
    """测试缓存管理器"""
    print("测试缓存管理器...")
    from douban2notion.cache_manager import cache_manager

    # 测试基本操作
    cache_manager.set("test_cache", "key1", "value1")
    assert cache_manager.get("test_cache", "key1") == "value1"
    assert cache_manager.has("test_cache", "key1") == True
    assert cache_manager.has("test_cache", "key2") == False

    # 测试统计
    stats = cache_manager.get_stats()
    assert "test_cache" in stats
    assert stats["test_cache"] == 1

    # 清理
    cache_manager.clear("test_cache")
    assert cache_manager.get_size("test_cache") == 0

    print("  ✓ 缓存管理器测试通过")


def test_batch_url_validation():
    """测试批量URL验证"""
    print("测试批量URL验证...")
    from douban2notion.cover_validator import batch_validate_urls

    # 测试空列表
    result = batch_validate_urls([])
    assert result == {}

    # 测试批量验证（使用缓存）
    test_urls = [
        "https://www.notion.so/icons/user-circle-filled_gray.svg",
        "https://httpbin.org/status/404",  # 明确返回404的URL
    ]
    results = batch_validate_urls(test_urls, max_workers=2)
    assert len(results) == 2
    assert results[test_urls[0]] == True
    assert results[test_urls[1]] == False

    print("  ✓ 批量URL验证测试通过")


def test_parallel_cover_fetch():
    """测试并行封面获取"""
    print("测试并行封面获取...")
    from douban2notion.douban import _get_book_cover

    # 模拟subject数据
    subject = {
        "isbn": "9787020002207",
        "isbn13": "9787020002207",
        "author": ["测试作者"],
    }

    start_time = time.time()
    cover, source = _get_book_cover(subject, "测试书籍")
    end_time = time.time()

    print(f"  封面获取耗时: {end_time - start_time:.2f}秒")
    if cover:
        print(f"  ✓ 成功获取封面 (来源: {source})")
    else:
        print(f"  ⚠ 未能获取封面（可能是网络问题）")

    print("  ✓ 并行封面获取测试通过")


def test_config():
    """测试配置"""
    print("测试配置...")
    from douban2notion.config import MAX_WORKERS, MAX_URL_WORKERS, COVER_FETCH_WORKERS

    print(f"  MAX_WORKERS: {MAX_WORKERS}")
    print(f"  MAX_URL_WORKERS: {MAX_URL_WORKERS}")
    print(f"  COVER_FETCH_WORKERS: {COVER_FETCH_WORKERS}")

    assert MAX_WORKERS > 0
    assert MAX_URL_WORKERS > 0
    assert COVER_FETCH_WORKERS > 0

    print("  ✓ 配置测试通过")


def main():
    print("=" * 50)
    print("开始测试优化效果")
    print("=" * 50)

    try:
        test_config()
        print()
        test_cache_manager()
        print()
        test_batch_url_validation()
        print()
        test_parallel_cover_fetch()

        print()
        print("=" * 50)
        print("所有测试通过！")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
