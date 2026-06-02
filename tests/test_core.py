"""
核心功能单元测试
"""

import os
import sys
import time
import threading
from unittest.mock import Mock, patch, MagicMock

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestCacheManager:
    """缓存管理器测试"""

    def test_singleton(self):
        """测试单例模式"""
        from douban2notion.cache_manager import CacheManager

        instance1 = CacheManager()
        instance2 = CacheManager()
        assert instance1 is instance2

    def test_basic_operations(self):
        """测试基本操作"""
        from douban2notion.cache_manager import CacheManager

        cache = CacheManager()

        # 测试set和get
        cache.set("test_cache", "key1", "value1")
        assert cache.get("test_cache", "key1") == "value1"

        # 测试has
        assert cache.has("test_cache", "key1") == True
        assert cache.has("test_cache", "key2") == False

        # 测试默认值
        assert cache.get("test_cache", "key2", "default") == "default"

        # 清理
        cache.clear("test_cache")

    def test_thread_safety(self):
        """测试线程安全"""
        from douban2notion.cache_manager import CacheManager

        cache = CacheManager()
        results = []

        def worker(thread_id):
            for i in range(100):
                cache.set("thread_test", f"key_{thread_id}_{i}", f"value_{i}")
            results.append(thread_id)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 验证所有值都已设置
        for i in range(10):
            for j in range(100):
                assert cache.get("thread_test", f"key_{i}_{j}") == f"value_{j}"

        cache.clear("thread_test")

    def test_stats(self):
        """测试统计功能"""
        from douban2notion.cache_manager import CacheManager

        cache = CacheManager()
        cache.set("stats_test", "key1", "value1")
        cache.set("stats_test", "key2", "value2")

        stats = cache.get_stats()
        assert "stats_test" in stats
        assert stats["stats_test"] == 2

        cache.clear("stats_test")


class TestRetryUtils:
    """重试工具测试"""

    def test_retry_on_exception(self):
        """测试异常重试装饰器"""
        from douban2notion.retry_utils import retry_on_exception

        call_count = 0

        @retry_on_exception(max_retries=3, delay=0.01, exceptions=(ValueError,))
        def failing_function():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("测试错误")
            return "成功"

        result = failing_function()
        assert result == "成功"
        assert call_count == 3

    def test_retry_on_exception_max_retries(self):
        """测试达到最大重试次数"""
        from douban2notion.retry_utils import retry_on_exception

        @retry_on_exception(max_retries=2, delay=0.01)
        def always_failing():
            raise ValueError("总是失败")

        with pytest.raises(ValueError):
            always_failing()

    def test_retryable_request(self):
        """测试可重试请求类"""
        from douban2notion.retry_utils import RetryableRequest

        requester = RetryableRequest(max_retries=2, delay=0.01, timeout=5)
        assert requester.max_retries == 2
        assert requester.timeout == 5


class TestPerformanceMonitor:
    """性能监控测试"""

    def test_timing_decorator(self):
        """测试计时装饰器"""
        from douban2notion.performance_monitor import timing, performance_stats

        # 清空统计
        performance_stats.clear()

        @timing
        def slow_function():
            time.sleep(0.1)
            return "done"

        result = slow_function()
        assert result == "done"

        stats = performance_stats.get_stats("slow_function")
        assert stats["count"] == 1
        assert stats["avg"] >= 0.1

    def test_timer_context_manager(self):
        """测试计时器上下文管理器"""
        from douban2notion.performance_monitor import Timer, performance_stats

        # 清空统计
        performance_stats.clear()

        with Timer("test_operation"):
            time.sleep(0.1)

        stats = performance_stats.get_stats("test_operation")
        assert stats["count"] == 1
        assert stats["avg"] >= 0.1

    def test_performance_stats(self):
        """测试性能统计"""
        from douban2notion.performance_monitor import PerformanceStats

        stats = PerformanceStats()
        stats.record("func1", 1.0)
        stats.record("func1", 2.0)
        stats.record("func2", 0.5)

        result = stats.get_stats("func1")
        assert result["count"] == 2
        assert result["total"] == 3.0
        assert result["avg"] == 1.5
        assert result["min"] == 1.0
        assert result["max"] == 2.0

        all_stats = stats.get_stats()
        assert "func1" in all_stats
        assert "func2" in all_stats


class TestConfigValidator:
    """配置验证测试"""

    def test_validate_config_success(self):
        """测试配置验证成功"""
        from douban2notion.config_validator import validate_config

        # 设置必要的环境变量
        os.environ["DOUBAN_NAME"] = "test_user"
        os.environ["AUTH_TOKEN"] = "test_token"
        os.environ["NOTION_TOKEN"] = "test_notion_token"

        try:
            config = validate_config()
            assert config["DOUBAN_NAME"] == "test_user"
            assert config["NOTION_TOKEN"] == "test_notion_token"
            assert "AUTH_TOKEN" not in config
        finally:
            # 清理环境变量
            del os.environ["DOUBAN_NAME"]
            del os.environ["AUTH_TOKEN"]
            del os.environ["NOTION_TOKEN"]

    def test_validate_config_missing_required(self):
        """测试缺少必要配置"""
        from douban2notion.config_validator import validate_config, ConfigValidationError

        # 确保环境变量不存在
        for key in ["DOUBAN_NAME", "AUTH_TOKEN", "NOTION_TOKEN"]:
            if key in os.environ:
                del os.environ[key]

        with pytest.raises(ConfigValidationError):
            validate_config()

    def test_validate_config_movie_type(self):
        """测试电影类型配置验证"""
        from douban2notion.config_validator import validate_config, ConfigValidationError
        from unittest.mock import patch

        # 保存原始环境变量
        original_env = {}
        for key in ["DOUBAN_NAME", "AUTH_TOKEN", "NOTION_TOKEN", "NOTION_MOVIE_URL"]:
            original_env[key] = os.environ.get(key)

        try:
            # 设置必要配置
            os.environ["DOUBAN_NAME"] = "test_user"
            os.environ["AUTH_TOKEN"] = "test_token"
            os.environ["NOTION_TOKEN"] = "test_notion_token"

            # 确保电影URL不存在
            if "NOTION_MOVIE_URL" in os.environ:
                del os.environ["NOTION_MOVIE_URL"]

            # Mock load_dotenv以避免加载.env文件
            with patch('douban2notion.config_validator.load_dotenv'):
                with pytest.raises(ConfigValidationError):
                    validate_config("movie")
        finally:
            # 恢复原始环境变量
            for key, value in original_env.items():
                if value is None:
                    if key in os.environ:
                        del os.environ[key]
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
