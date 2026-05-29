"""
重试工具模块
提供统一的重试机制和错误处理
"""

import time
import logging
from functools import wraps
from typing import Callable, Optional, Tuple, Type, Union

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError

# 配置日志
logger = logging.getLogger(__name__)


def retry_on_exception(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (RequestException,),
    on_retry: Optional[Callable] = None,
):
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 初始延迟时间（秒）
        backoff: 延迟倍数（指数退避）
        exceptions: 需要重试的异常类型
        on_retry: 重试时的回调函数
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 第{attempt + 1}次尝试失败: {str(e)[:100]}, "
                            f"{current_delay:.1f}秒后重试..."
                        )
                        if on_retry:
                            on_retry(attempt + 1, e)
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"{func.__name__} 在{max_retries + 1}次尝试后仍然失败: {str(e)[:100]}"
                        )
                        raise last_exception

            raise last_exception
        return wrapper
    return decorator


def retry_on_http_error(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    retry_on_status: Tuple[int, ...] = (429, 500, 502, 503, 504),
):
    """
    HTTP请求重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 初始延迟时间（秒）
        backoff: 延迟倍数
        retry_on_status: 需要重试的HTTP状态码
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    response = func(*args, **kwargs)
                    if response.status_code in retry_on_status:
                        if attempt < max_retries:
                            logger.warning(
                                f"{func.__name__} 返回状态码{response.status_code}, "
                                f"{current_delay:.1f}秒后重试..."
                            )
                            time.sleep(current_delay)
                            current_delay *= backoff
                            continue
                    return response
                except (Timeout, ConnectionError) as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 连接失败: {str(e)[:100]}, "
                            f"{current_delay:.1f}秒后重试..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"{func.__name__} 在{max_retries + 1}次尝试后仍然失败: {str(e)[:100]}"
                        )
                        raise last_exception

            return response
        return wrapper
    return decorator


class RetryableRequest:
    """可重试的HTTP请求类"""

    def __init__(
        self,
        max_retries: int = 3,
        delay: float = 1.0,
        backoff: float = 2.0,
        timeout: int = 10,
    ):
        self.max_retries = max_retries
        self.delay = delay
        self.backoff = backoff
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, url: str, **kwargs) -> requests.Response:
        """发送GET请求（带重试）"""
        kwargs.setdefault('timeout', self.timeout)
        return self._request('GET', url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """发送POST请求（带重试）"""
        kwargs.setdefault('timeout', self.timeout)
        return self._request('POST', url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送请求（带重试）"""
        last_exception = None
        current_delay = self.delay

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code == 429:  # Too Many Requests
                    if attempt < self.max_retries:
                        retry_after = int(response.headers.get('Retry-After', current_delay))
                        logger.warning(
                            f"请求被限流，{retry_after}秒后重试..."
                        )
                        time.sleep(retry_after)
                        current_delay *= self.backoff
                        continue
                return response
            except (Timeout, ConnectionError, RequestException) as e:
                last_exception = e
                if attempt < self.max_retries:
                    logger.warning(
                        f"请求失败 ({method} {url}): {str(e)[:100]}, "
                        f"{current_delay:.1f}秒后重试..."
                    )
                    time.sleep(current_delay)
                    current_delay *= self.backoff
                else:
                    logger.error(
                        f"请求在{self.max_retries + 1}次尝试后仍然失败: {str(e)[:100]}"
                    )
                    raise last_exception

        return response


# 全局可重试请求实例
retryable_request = RetryableRequest()


def safe_request(
    url: str,
    method: str = 'GET',
    max_retries: int = 3,
    timeout: int = 10,
    **kwargs
) -> Optional[requests.Response]:
    """
    安全的HTTP请求（带重试和错误处理）

    Args:
        url: 请求URL
        method: 请求方法
        max_retries: 最大重试次数
        timeout: 超时时间
        **kwargs: 其他请求参数

    Returns:
        Response对象或None（如果失败）
    """
    try:
        requester = RetryableRequest(max_retries=max_retries, timeout=timeout)
        if method.upper() == 'GET':
            return requester.get(url, **kwargs)
        elif method.upper() == 'POST':
            return requester.post(url, **kwargs)
        else:
            logger.error(f"不支持的请求方法: {method}")
            return None
    except Exception as e:
        logger.error(f"请求失败 ({method} {url}): {str(e)[:100]}")
        return None
