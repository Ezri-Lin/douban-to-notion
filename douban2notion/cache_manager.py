"""
统一的缓存管理器
用于管理所有模块的缓存，提供线程安全的缓存访问
"""

import threading
from typing import Any, Dict, Optional


class CacheManager:
    """统一的缓存管理器（单例模式）"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._caches: Dict[str, Dict[str, Any]] = {}
                cls._instance._cache_locks: Dict[str, threading.Lock] = {}
            return cls._instance

    def _get_cache_lock(self, cache_name: str) -> threading.Lock:
        """获取指定缓存的锁（懒加载）"""
        if cache_name not in self._cache_locks:
            with self._lock:
                if cache_name not in self._cache_locks:
                    self._cache_locks[cache_name] = threading.Lock()
        return self._cache_locks[cache_name]

    def get_cache(self, cache_name: str) -> Dict[str, Any]:
        """获取指定名称的缓存字典"""
        if cache_name not in self._caches:
            with self._lock:
                if cache_name not in self._caches:
                    self._caches[cache_name] = {}
        return self._caches[cache_name]

    def get(self, cache_name: str, key: str, default: Any = None) -> Any:
        """从缓存中获取值"""
        cache = self.get_cache(cache_name)
        # 无锁读取（dict读操作在CPython中是原子的）
        return cache.get(key, default)

    def set(self, cache_name: str, key: str, value: Any) -> None:
        """设置缓存值（线程安全）"""
        cache = self.get_cache(cache_name)
        cache_lock = self._get_cache_lock(cache_name)
        with cache_lock:
            cache[key] = value

    def has(self, cache_name: str, key: str) -> bool:
        """检查缓存中是否存在指定键"""
        cache = self.get_cache(cache_name)
        return key in cache

    def clear(self, cache_name: str) -> None:
        """清空指定缓存"""
        if cache_name in self._caches:
            cache_lock = self._get_cache_lock(cache_name)
            with cache_lock:
                self._caches[cache_name].clear()

    def clear_all(self) -> None:
        """清空所有缓存"""
        with self._lock:
            for cache_name in list(self._caches.keys()):
                self._caches[cache_name].clear()

    def get_stats(self) -> Dict[str, int]:
        """获取所有缓存的统计信息"""
        with self._lock:
            return {
                cache_name: len(cache)
                for cache_name, cache in self._caches.items()
            }

    def get_size(self, cache_name: str) -> int:
        """获取指定缓存的大小"""
        cache = self.get_cache(cache_name)
        return len(cache)


# 全局缓存管理器实例
cache_manager = CacheManager()


# 便捷函数
def get_url_validation_cache() -> Dict[str, bool]:
    """获取URL验证缓存"""
    return cache_manager.get_cache("url_validation")


def get_imdb_info_cache() -> Dict[str, Any]:
    """获取IMDB信息缓存"""
    return cache_manager.get_cache("imdb_info")


def get_cover_url_cache() -> Dict[str, bool]:
    """获取封面URL验证缓存"""
    return cache_manager.get_cache("cover_url_validation")


def get_author_name_cache() -> Dict[str, Optional[str]]:
    """获取作者名称缓存"""
    return cache_manager.get_cache("author_name")


def get_relation_name_cache() -> Dict[str, Optional[str]]:
    """获取关系名称缓存"""
    return cache_manager.get_cache("relation_name")


def get_douban_subject_cache() -> Dict[str, Any]:
    """获取豆瓣条目缓存"""
    return cache_manager.get_cache("douban_subject")


def print_cache_stats() -> None:
    """打印缓存统计信息"""
    stats = cache_manager.get_stats()
    print("\n缓存统计:")
    for cache_name, size in stats.items():
        print(f"  {cache_name}: {size} 条记录")
