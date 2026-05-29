"""
性能监控模块
提供计时装饰器和性能统计功能
"""

import time
import logging
from functools import wraps
from typing import Dict, List, Optional
from collections import defaultdict
import threading

# 配置日志
logger = logging.getLogger(__name__)


class PerformanceStats:
    """性能统计类（单例模式）"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._stats: Dict[str, List[float]] = defaultdict(list)
                cls._instance._stats_lock = threading.Lock()
            return cls._instance

    def record(self, func_name: str, duration: float) -> None:
        """记录函数执行时间"""
        with self._stats_lock:
            self._stats[func_name].append(duration)

    def get_stats(self, func_name: Optional[str] = None) -> Dict:
        """获取统计信息"""
        with self._stats_lock:
            if func_name:
                times = self._stats.get(func_name, [])
                if not times:
                    return {}
                return {
                    "count": len(times),
                    "total": sum(times),
                    "avg": sum(times) / len(times),
                    "min": min(times),
                    "max": max(times),
                }
            else:
                result = {}
                for name, times in self._stats.items():
                    if times:
                        result[name] = {
                            "count": len(times),
                            "total": sum(times),
                            "avg": sum(times) / len(times),
                            "min": min(times),
                            "max": max(times),
                        }
                return result

    def get_summary(self) -> str:
        """获取统计摘要"""
        stats = self.get_stats()
        if not stats:
            return "暂无性能数据"

        lines = ["\n" + "="*60]
        lines.append("性能统计摘要")
        lines.append("="*60)
        lines.append(f"{'函数名':<40} {'调用次数':>8} {'总耗时':>10} {'平均耗时':>10}")
        lines.append("-"*60)

        # 按总耗时排序
        sorted_stats = sorted(
            stats.items(),
            key=lambda x: x[1]["total"],
            reverse=True
        )

        for name, data in sorted_stats:
            lines.append(
                f"{name:<40} {data['count']:>8} {data['total']:>10.2f}s {data['avg']:>10.2f}s"
            )

        lines.append("="*60)
        return "\n".join(lines)

    def clear(self) -> None:
        """清空统计数据"""
        with self._stats_lock:
            self._stats.clear()


# 全局性能统计实例
performance_stats = PerformanceStats()


def timing(func=None, *, logger_name: Optional[str] = None):
    """
    计时装饰器

    Args:
        func: 要装饰的函数
        logger_name: 日志记录器名称
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = f(*args, **kwargs)
            end_time = time.time()
            duration = end_time - start_time

            # 记录统计
            performance_stats.record(f.__name__, duration)

            # 记录日志
            log = logging.getLogger(logger_name or __name__)
            if duration > 1.0:  # 超过1秒的函数调用记录警告
                log.warning(f"{f.__name__} 耗时较长: {duration:.2f}秒")
            else:
                log.debug(f"{f.__name__} 耗时: {duration:.2f}秒")

            return result
        return wrapper

    if func is None:
        return decorator
    return decorator(func)


def timing_async(func=None, *, logger_name: Optional[str] = None):
    """
    异步函数计时装饰器

    Args:
        func: 要装饰的异步函数
        logger_name: 日志记录器名称
    """
    def decorator(f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            result = await f(*args, **kwargs)
            end_time = time.time()
            duration = end_time - start_time

            # 记录统计
            performance_stats.record(f.__name__, duration)

            # 记录日志
            log = logging.getLogger(logger_name or __name__)
            if duration > 1.0:
                log.warning(f"{f.__name__} 耗时较长: {duration:.2f}秒")
            else:
                log.debug(f"{f.__name__} 耗时: {duration:.2f}秒")

            return result
        return wrapper

    if func is None:
        return decorator
    return decorator(func)


class Timer:
    """
    上下文管理器计时器

    使用示例:
        with Timer("my_operation"):
            # 执行某些操作
            pass
    """

    def __init__(self, name: str, logger_name: Optional[str] = None):
        self.name = name
        self.logger_name = logger_name
        self.start_time = None
        self.end_time = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        duration = self.duration

        # 记录统计
        performance_stats.record(self.name, duration)

        # 记录日志
        log = logging.getLogger(self.logger_name or __name__)
        if duration > 1.0:
            log.warning(f"{self.name} 耗时较长: {duration:.2f}秒")
        else:
            log.debug(f"{self.name} 耗时: {duration:.2f}秒")

        return False

    @property
    def duration(self) -> float:
        """获取持续时间"""
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time is not None else time.time()
        return end - self.start_time


def print_performance_stats() -> None:
    """打印性能统计"""
    print(performance_stats.get_summary())


def reset_performance_stats() -> None:
    """重置性能统计"""
    performance_stats.clear()


# 便捷函数
def get_function_stats(func_name: str) -> Dict:
    """获取指定函数的性能统计"""
    return performance_stats.get_stats(func_name)


def get_all_stats() -> Dict:
    """获取所有函数的性能统计"""
    return performance_stats.get_stats()
