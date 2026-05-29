"""
配置验证模块
启动时检查必要环境变量，避免运行时才发现配置错误
"""

import os
import sys
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv


# 必要配置项及其说明
REQUIRED_CONFIG = {
    "DOUBAN_NAME": "豆瓣用户名",
    "NOTION_TOKEN": "Notion API Token",
}

# 电影同步必要配置
MOVIE_REQUIRED_CONFIG = {
    "NOTION_MOVIE_URL": "Notion电影数据库URL",
}

# 图书同步必要配置
BOOK_REQUIRED_CONFIG = {
    "NOTION_BOOK_URL": "Notion图书数据库URL",
}

# 可选配置项及其默认值
OPTIONAL_CONFIG = {
    "DOUBAN_API_HOST": "frodo.douban.com",
    "DOUBAN_API_KEY": "0ac44ae016490db2204ce0a042db2916",
    "TMDB_API_KEY": "",
    "MAX_WORKERS": "10",
    "URL_VALIDATION_WORKERS": "5",
    "COVER_FETCH_WORKERS": "5",
}


class ConfigValidationError(Exception):
    """配置验证错误"""
    pass


def validate_config(sync_type: Optional[str] = None) -> Dict[str, str]:
    """
    验证配置

    Args:
        sync_type: 同步类型 ('movie' 或 'book')，如果指定则验证对应的必要配置

    Returns:
        配置字典

    Raises:
        ConfigValidationError: 如果必要配置缺失
    """
    # 加载环境变量
    load_dotenv()

    missing_configs = []
    config = {}

    # 检查必要配置
    for key, description in REQUIRED_CONFIG.items():
        value = os.getenv(key)
        if not value:
            missing_configs.append(f"{key} ({description})")
        else:
            config[key] = value

    # 检查同步类型特定的配置
    if sync_type == "movie":
        for key, description in MOVIE_REQUIRED_CONFIG.items():
            value = os.getenv(key)
            if not value:
                missing_configs.append(f"{key} ({description})")
            else:
                config[key] = value
    elif sync_type == "book":
        for key, description in BOOK_REQUIRED_CONFIG.items():
            value = os.getenv(key)
            if not value:
                missing_configs.append(f"{key} ({description})")
            else:
                config[key] = value

    # 如果有缺失的配置，抛出错误
    if missing_configs:
        error_msg = "缺少必要配置:\n"
        for config_name in missing_configs:
            error_msg += f"  - {config_name}\n"
        error_msg += "\n请在.env文件或环境变量中配置这些项。"
        raise ConfigValidationError(error_msg)

    # 加载可选配置
    for key, default_value in OPTIONAL_CONFIG.items():
        config[key] = os.getenv(key, default_value)

    return config


def validate_config_or_exit(sync_type: Optional[str] = None) -> Dict[str, str]:
    """
    验证配置，如果失败则退出程序

    Args:
        sync_type: 同步类型

    Returns:
        配置字典
    """
    try:
        return validate_config(sync_type)
    except ConfigValidationError as e:
        print(f"❌ 配置验证失败:\n{e}")
        sys.exit(1)


def check_optional_config() -> List[Tuple[str, str, str]]:
    """
    检查可选配置项

    Returns:
        配置项列表，每项包含 (key, value, description)
    """
    load_dotenv()

    results = []
    for key, default_value in OPTIONAL_CONFIG.items():
        value = os.getenv(key, default_value)
        description = OPTIONAL_CONFIG.get(key, "")
        results.append((key, value, description))

    return results


def print_config_summary(sync_type: Optional[str] = None) -> None:
    """
    打印配置摘要

    Args:
        sync_type: 同步类型
    """
    print("\n" + "="*50)
    print("配置摘要")
    print("="*50)

    # 必要配置
    print("\n必要配置:")
    for key, description in REQUIRED_CONFIG.items():
        value = os.getenv(key)
        status = "✓ 已配置" if value else "✗ 缺失"
        print(f"  {key}: {status}")

    # 同步类型特定配置
    if sync_type == "movie":
        print("\n电影同步配置:")
        for key, description in MOVIE_REQUIRED_CONFIG.items():
            value = os.getenv(key)
            status = "✓ 已配置" if value else "✗ 缺失"
            print(f"  {key}: {status}")
    elif sync_type == "book":
        print("\n图书同步配置:")
        for key, description in BOOK_REQUIRED_CONFIG.items():
            value = os.getenv(key)
            status = "✓ 已配置" if value else "✗ 缺失"
            print(f"  {key}: {status}")

    # 可选配置
    print("\n可选配置:")
    for key, default_value in OPTIONAL_CONFIG.items():
        value = os.getenv(key, default_value)
        print(f"  {key}: {value}")

    print("="*50 + "\n")


def main():
    """命令行配置验证"""
    import argparse

    parser = argparse.ArgumentParser(description="验证配置")
    parser.add_argument(
        "sync_type",
        nargs="?",
        choices=["movie", "book"],
        help="同步类型",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细配置信息",
    )
    args = parser.parse_args()

    try:
        config = validate_config(args.sync_type)
        print("✓ 配置验证通过")

        if args.verbose:
            print_config_summary(args.sync_type)

    except ConfigValidationError as e:
        print(f"❌ 配置验证失败:\n{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
