"""Web 工具：简化网页/HTTP 查询工具。"""

from typing import Any

from langchain.tools import tool


@tool
def web_search(query: str) -> str:
    """使用搜索引擎检索网页。"""
    return "Web search 功能暂未实现，请配置搜索 API。"


@tool
def fetch_url(url: str) -> str:
    """抓取网页内容。"""
    return f"Fetch URL 功能暂未实现，target: {url}"


def build_web_tools() -> list[Any]:
    return [web_search, fetch_url]
