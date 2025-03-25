from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import httpx
from bs4 import BeautifulSoup
import asyncio
import logging
import time
import re
from urllib.parse import quote, urlparse
import os
from functools import wraps
from typing import List, Dict, Any, Optional
import json
from cachetools import TTLCache
from datetime import datetime

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("search_app.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 初始化 FastMCP 和載入環境變數
mcp = FastMCP("search")
load_dotenv()

# 載入配置
def load_config() -> Dict[str, Any]:
    """載入並返回應用程式配置"""
    return {
        "USER_AGENT": os.getenv("USER_AGENT", "search-app/1.0"),
        "DUCKDUCKGO_URL": os.getenv("DUCKDUCKGO_URL", "https://html.duckduckgo.com/html/"),
        "REQUEST_TIMEOUT": float(os.getenv("REQUEST_TIMEOUT", "30.0")),
        "CONTENT_LENGTH_LIMIT": int(os.getenv("CONTENT_LENGTH_LIMIT", "5000")),
        "CACHE_MAX_SIZE": int(os.getenv("CACHE_MAX_SIZE", "100")),
        "CACHE_TTL": int(os.getenv("CACHE_TTL", "3600")),
        "CACHE_TTL_NEWS": int(os.getenv("CACHE_TTL_NEWS", "900")),  # 15分鐘
        "CACHE_TTL_DOCS": int(os.getenv("CACHE_TTL_DOCS", "86400")),  # 24小時
        "USE_JINA_API": os.getenv("USE_JINA_API", "True").lower() == "true",
        "JINA_TIMEOUT": float(os.getenv("JINA_TIMEOUT", "15.0")),
        "RAW_HTML_TIMEOUT": float(os.getenv("RAW_HTML_TIMEOUT", "10.0")),
        "REGION": os.getenv("REGION", "tw"),
        "SAFE_SEARCH": os.getenv("SAFE_SEARCH", "True").lower() == "true",
        "MAX_RESULTS": int(os.getenv("MAX_RESULTS", "10")),
        "MAX_CONCURRENT_REQUESTS": int(os.getenv("MAX_CONCURRENT_REQUESTS", "5"))
    }

# 載入配置
CONFIG = load_config()

# 初始化快取
search_cache = TTLCache(maxsize=CONFIG["CACHE_MAX_SIZE"], ttl=CONFIG["CACHE_TTL"])
url_cache = TTLCache(maxsize=CONFIG["CACHE_MAX_SIZE"], ttl=CONFIG["CACHE_TTL"])
search_cache_times = {}

# 初始化並發控制
request_semaphore = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_REQUESTS", "5")))

# 快取統計
cache_stats = {
    "hits": 0,
    "misses": 0
}

# 快取統計函數
def get_cache_stats() -> Dict[str, Any]:
    """獲取快取統計資訊"""
    total_requests = cache_stats["hits"] + cache_stats["misses"]
    hit_rate = (cache_stats["hits"] / total_requests) * 100 if total_requests > 0 else 0
    
    return {
        "status": "success",
        "hits": cache_stats["hits"],
        "misses": cache_stats["misses"],
        "total_requests": total_requests,
        "hit_rate": f"{hit_rate:.2f}%",
        "search_cache_size": len(search_cache),
        "url_cache_size": len(url_cache),
        "cache_ttl": CONFIG["CACHE_TTL"]
    }

# 搜尋結果格式化函數
def format_snippet(snippet: str, max_length: int = 150) -> str:
    """格式化搜尋結果摘要，提高可讀性"""
    if not snippet:
        return ""
    
    # 移除多餘空白
    snippet = re.sub(r'\s+', ' ', snippet).strip()
    
    # 限制長度並添加省略號
    if len(snippet) > max_length:
        snippet = snippet[:max_length] + "..."
        
    return snippet

def format_url_for_display(url: str) -> str:
    """格式化URL以便於顯示"""
    try:
        parsed = urlparse(url)
        
        # 移除 www. 前綴
        domain = parsed.netloc
        if domain.startswith('www.'):
            domain = domain[4:]
            
        # 簡化路徑
        path = parsed.path
        if len(path) > 30:
            path_parts = path.split('/')
            if len(path_parts) > 3:
                path = '/'.join(path_parts[:3]) + "/..."
                
        return f"{domain}{path}"
    except Exception as e:
        logger.debug(f"URL 格式化失敗: {str(e)}")
        return url

# 安全地抓取指定 URL 的內容
async def fetch_url(url: str, content_format: str = "text", max_length: int = CONFIG["CONTENT_LENGTH_LIMIT"]) -> Dict[str, Any]:
    """
    安全地抓取指定 URL 的內容
    
    Args:
        url: 要抓取的網址
        content_format: 返回格式 (text, markdown, html)
        max_length: 最大內容長度
        
    Returns:
        dict: 包含狀態和內容的字典
    """
    # 檢查 URL 格式
    if not url or not isinstance(url, str):
        return {"status": "error", "message": "無效的網址格式"}
    
    # 確保 URL 包含協議
    if not url.startswith(('http://', 'https://')):
        url = f"https://{url}"
    
    # 檢查快取
    cache_key = f"{url}:{content_format}"
    if cache_key in url_cache:
        logger.info(f"使用快取內容: {url}")
        cache_stats["hits"] += 1
        return url_cache[cache_key]
    
    cache_stats["misses"] += 1
    
    # 使用並發控制
    async with request_semaphore:
        try:
            logger.info(f"抓取網頁內容: {url}")
            headers = {
                "User-Agent": CONFIG["USER_AGENT"],
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            
            # 使用 Jina API 或直接抓取
            use_jina = content_format == "markdown" and CONFIG["USE_JINA_API"]
            fetch_url = f"https://r.jina.ai/{url}" if use_jina else url
            
            async with httpx.AsyncClient(verify=True) as client:
                response = await client.get(
                    fetch_url, 
                    headers=headers, 
                    timeout=CONFIG["JINA_TIMEOUT"],
                    follow_redirects=True
                )
                response.raise_for_status()
                
                status_code = response.status_code
                
                # 根據請求的格式返回內容
                if use_jina:
                    # Jina API 已經將內容轉換為 Markdown
                    content = response.text
                    if max_length and len(content) > max_length:
                        content = content[:max_length] + "...\n\n[內容已截斷，點擊原始連結查看完整內容]"
                    
                    result = {
                        "status": "success",
                        "format": "markdown",
                        "url": url,
                        "content": content,
                        "code": status_code
                    }
                    
                    # 儲存到快取
                    url_cache[cache_key] = result
                    return result
                else:
                    # 解析 HTML
                    soup = BeautifulSoup(response.text, "html.parser")
                    
                    # 移除不需要的元素
                    for tag in soup(["script", "style", "nav", "footer", "iframe", "header", "aside"]):
                        tag.decompose()
                    
                    if content_format == "html":
                        # 提取主要內容區域
                        main_content = soup.find(["main", "article", "div", "body"])
                        html_content = str(main_content) if main_content else str(soup)
                        
                        if max_length and len(html_content) > max_length:
                            # 簡單截斷 HTML 可能會破壞結構，這裡僅作示意
                            html_content = html_content[:max_length] + "..."
                            
                        result = {
                            "status": "success",
                            "format": "html",
                            "url": url,
                            "content": html_content,
                            "code": status_code
                        }
                        
                        # 儲存到快取
                        url_cache[cache_key] = result
                        return result
                    else:
                        # 提取純文本
                        text = soup.get_text()
                        
                        # 清理文本
                        lines = (line.strip() for line in text.splitlines())
                        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                        text = '\n'.join(chunk for chunk in chunks if chunk)
                        
                        if max_length and len(text) > max_length:
                            text = text[:max_length] + "...\n\n[內容已截斷，點擊原始連結查看完整內容]"
                        
                        result = {
                            "status": "success",
                            "format": "text",
                            "url": url,
                            "content": text,
                            "code": status_code
                        }
                        
                        # 儲存到快取
                        url_cache[cache_key] = result
                        return result
                        
        except httpx.TimeoutException:
            logger.warning(f"請求超時: {url}")
            # 如果使用 Jina API 超時，嘗試直接抓取
            if use_jina:
                try:
                    logger.info(f"改為直接抓取網頁: {url}")
                    async with httpx.AsyncClient(verify=True) as client:
                        response = await client.get(
                            url, 
                            headers=headers, 
                            timeout=CONFIG["RAW_HTML_TIMEOUT"],
                            follow_redirects=True
                        )
                        response.raise_for_status()
                        
                        soup = BeautifulSoup(response.text, "html.parser")
                        for tag in soup(["script", "style"]):
                            tag.decompose()
                            
                        text = soup.get_text()
                        lines = (line.strip() for line in text.splitlines())
                        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                        text = '\n'.join(chunk for chunk in chunks if chunk)
                        
                        if max_length and len(text) > max_length:
                            text = text[:max_length] + "...\n\n[內容已截斷]"
                        
                        result = {
                            "status": "success",
                            "format": "text",
                            "url": url,
                            "content": text,
                            "code": response.status_code,
                            "note": "已切換到純文本模式，原始 Markdown 轉換失敗"
                        }
                        
                        # 儲存到快取
                        url_cache[cache_key] = result
                        return result
                except Exception as e:
                    logger.error(f"後備抓取失敗: {str(e)}")
                    return {
                        "status": "error",
                        "url": url,
                        "message": "無法讀取網頁內容，請稍後再試或直接點擊連結查看",
                        "error_type": "timeout"
                    }
            else:
                return {
                    "status": "error",
                    "url": url,
                    "message": "請求超時，網頁載入太慢或無法連線",
                    "error_type": "timeout"
                }
                
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            error_messages = {
                404: "網頁不存在或已被移除",
                403: "無法存取此網頁，可能需要登入或沒有權限",
                500: "網站伺服器出現錯誤，請稍後再試",
                503: "網站暫時無法使用，可能正在維護中"
            }
            message = error_messages.get(status_code, f"發生錯誤 (HTTP {status_code})")
            logger.error(f"HTTP 錯誤 {status_code}: {url}")
            return {
                "status": "error",
                "url": url,
                "message": message,
                "code": status_code,
                "error_type": "http"
            }
            
        except Exception as e:
            logger.error(f"抓取失敗 {url}: {str(e)}")
            return {
                "status": "error",
                "url": url,
                "message": "無法讀取網頁內容，請直接點擊連結查看",
                "error_type": "general"
            }

async def search_duckduckgo(query: str, limit: int = 5, region: str = "tw", safe_search: bool = True) -> Dict[str, Any]:
    """從 DuckDuckGo 獲取搜尋結果"""
    from datetime import datetime
    
    # 根據查詢類型調整快取時間
    ttl = CONFIG["CACHE_TTL"]
    if any(news_term in query.lower() for news_term in ["新聞", "最新", "今日", "news", "latest"]):
        ttl = CONFIG["CACHE_TTL_NEWS"]
        logger.info(f"使用新聞快取時間 ({ttl}秒): {query}")
    elif any(docs_term in query.lower() for docs_term in ["文檔", "教程", "指南", "docs", "tutorial", "guide"]):
        ttl = CONFIG["CACHE_TTL_DOCS"]
        logger.info(f"使用文檔快取時間 ({ttl}秒): {query}")
    
    # 檢查快取
    cache_key = f"{query}:{limit}:{region}:{safe_search}"
    if cache_key in search_cache:
        # 檢查快取是否過期
        cache_time = search_cache_times.get(cache_key, datetime.min)
        if (datetime.now() - cache_time).total_seconds() < ttl:
            logger.info(f"使用快取結果: {query}")
            cache_stats["hits"] += 1
            return {
                "status": "success",
                "source": "cache",
                "results": search_cache[cache_key]
            }
    
    cache_stats["misses"] += 1
        
    try:
        # 安全處理查詢字串
        # 直接使用原始查詢字串，讓 httpx 處理編碼
        formatted_query = query
        
        # 添加區域和安全搜尋參數
        params = {
            "q": formatted_query,
            "kl": region,  # 區域設定
        }
        
        if safe_search:
            params["kp"] = "1"
            
        # 建構 URL
        url = f"{CONFIG['DUCKDUCKGO_URL']}"
        
        # 設定標頭
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        
        # 發送請求
        async with request_semaphore:
            async with httpx.AsyncClient(verify=True) as client:
                response = await client.get(
                    url, 
                    params=params,
                    headers=headers, 
                    timeout=CONFIG["REQUEST_TIMEOUT"],
                    follow_redirects=True
                )
                response.raise_for_status()
                
                # 解析 HTML 回應
                soup = BeautifulSoup(response.text, "html.parser")
                result_elements = soup.select('.result__body')
                
                # 檢查是否有搜尋建議
                suggestion_elem = soup.select_one('.search__did-you-mean')
                suggestion = None
                if suggestion_elem:
                    suggestion_link = suggestion_elem.select_one('a')
                    if suggestion_link:
                        suggestion = suggestion_link.get_text().strip()
                
                # 提取結果(限制數量)
                results = []
                for result in result_elements[:limit]:
                    title_elem = result.select_one('.result__a')
                    url_elem = result.select_one('.result__url')
                    snippet_elem = result.select_one('.result__snippet')
                    
                    if title_elem and url_elem:
                        url_text = url_elem.get_text().strip()
                        # 確保 URL 格式正確
                        if not url_text.startswith(('http://', 'https://')):
                            url_text = f"https://{url_text}"
                            
                        # 格式化搜尋結果
                        title = title_elem.get_text().strip()
                        snippet = format_snippet(snippet_elem.get_text().strip() if snippet_elem else "")
                        display_url = format_url_for_display(url_text)
                            
                        result_dict = {
                            "title": title,
                            "url": url_text,
                            "display_url": display_url,
                            "snippet": snippet
                        }
                        results.append(result_dict)
                
                # 構建響應
                response_data = {
                    "results": results,
                    "suggestion": suggestion,
                    "total": len(results),
                    "query": query
                }
                
                # 儲存到快取
                search_cache[cache_key] = results
                search_cache_times[cache_key] = datetime.now()
                
                return {
                    "status": "success",
                    "source": "duckduckgo",
                    "results": results,
                    "suggestion": suggestion,
                    "total": len(results)
                }
                
    except httpx.TimeoutException:
        logger.warning(f"搜尋請求超時: {query}")
        return {
            "status": "error",
            "message": "搜尋請求超時，請稍後再試",
            "suggestion": "您可以縮短搜尋關鍵字或檢查網路連線",
            "error_type": "timeout"
        }
    except httpx.HTTPStatusError as e:
        logger.error(f"搜尋 HTTP 錯誤 {e.response.status_code}: {query}")
        return {
            "status": "error",
            "message": "搜尋服務暫時無法使用",
            "suggestion": "請稍後再試，或使用較簡單的搜尋關鍵字",
            "error_type": "http",
            "code": e.response.status_code
        }
    except Exception as e:
        logger.error(f"搜尋失敗: {str(e)}")
        return {
            "status": "error",
            "message": "搜尋過程發生錯誤",
            "suggestion": "請嘗試使用不同的關鍵字",
            "error_type": "general"
        }

# MCP 工具函數
@mcp.tool()
async def search_and_fetch(query: str, limit: int = 3, content_format: str = "text", region: str = "tw"):
    """
    搜尋網路並抓取結果頁面內容。

    Args:
        query: 搜尋關鍵字 
        limit: 返回結果數量 (預設: 3，最大: 10)
        content_format: 內容格式 (text, markdown, html)
        region: 搜尋區域 (tw, us, hk, jp 等)

    Returns:
        包含搜尋結果與頁面內容的字典
    """
    # 輸入驗證
    if not isinstance(query, str) or not query.strip():
        return {
            "status": "error",
            "message": "請輸入有效的搜尋關鍵字",
            "suggestion": "搜尋關鍵字不能為空"
        }
    
    if not isinstance(limit, int) or limit < 1:
        limit = 3
    
    # 限制結果數量
    limit = min(limit, 10)
    
    # 驗證內容格式
    valid_formats = ["text", "markdown", "html"]
    if content_format not in valid_formats:
        content_format = "text"
    
    # 區域設定驗證
    valid_regions = ["tw", "us", "hk", "cn", "jp", "global"]
    if region not in valid_regions:
        region = "tw"
    
    try:
        # 步驟 1: 獲取搜尋結果
        search_response = await search_duckduckgo(query, limit, region, CONFIG["SAFE_SEARCH"])
        
        # 檢查搜尋結果狀態
        if search_response["status"] != "success":
            return search_response
        
        results = search_response["results"]
        
        # 檢查搜尋結果是否為空
        if not results:
            suggestions = [
                f"{query} 教學",
                f"{query} 範例",
                f"如何使用 {query}"
            ]
            return {
                "status": "no_results",
                "message": f"沒有找到「{query}」的相關結果",
                "suggestions": suggestions,
                "alternative_queries": suggestions
            }
        
        # 步驟 2: 抓取每個結果的內容
        fetch_tasks = []
        for item in results:
            if "url" in item:
                fetch_tasks.append(fetch_url(item["url"], content_format))
        
        # 同時抓取所有內容
        contents = await asyncio.gather(*fetch_tasks)
        
        # 步驟 3: 組合搜尋結果與內容
        for i, (item, content) in enumerate(zip(results, contents)):
            results[i] = {**item, **content}
        
        # 構建最終響應
        response = {
            "status": "success",
            "query": query,
            "total": len(results),
            "format": content_format,
            "results": results,
            "suggestion": search_response.get("suggestion")
        }
        
        return response
    except Exception as e:
        logger.error(f"search_and_fetch 失敗: {str(e)}")
        return {
            "status": "error",
            "message": "搜尋過程發生錯誤，請稍後再試",
            "suggestion": "您可以嘗試使用不同的關鍵字或減少結果數量",
            "query": query
        }

@mcp.tool()
async def search(query: str, limit: int = 5, region: str = "tw"):
    """
    只搜尋網路，不抓取頁面內容。

    Args:
        query: 搜尋關鍵字
        limit: 返回結果數量 (預設: 5，最大: 10)
        region: 搜尋區域 (tw, us, hk, jp 等)

    Returns:
        搜尋結果列表
    """
    # 輸入驗證
    if not isinstance(query, str) or not query.strip():
        return {
            "status": "error",
            "message": "請輸入有效的搜尋關鍵字"
        }
    
    if not isinstance(limit, int) or limit < 1:
        limit = 5
    
    # 限制結果數量
    limit = min(limit, 10)
    
    # 區域設定驗證
    valid_regions = ["tw", "us", "hk", "cn", "jp", "global"]
    if region not in valid_regions:
        region = "tw"
    
    try:
        search_response = await search_duckduckgo(query, limit, region, CONFIG["SAFE_SEARCH"])
        return search_response
    except Exception as e:
        logger.error(f"search 失敗: {str(e)}")
        return {
            "status": "error",
            "message": "搜尋過程發生錯誤，請稍後再試",
            "query": query
        }

@mcp.tool()
async def fetch(url: str, content_format: str = "markdown"):
    """
    抓取指定網址的內容。

    Args:
        url: 要抓取的網址
        content_format: 內容格式 (text, markdown, html)

    Returns:
        網頁內容
    """
    # 輸入驗證
    if not isinstance(url, str) or not url.strip():
        return {
            "status": "error",
            "message": "請輸入有效的網址"
        }
    
    # 驗證內容格式
    valid_formats = ["text", "markdown", "html"]
    if content_format not in valid_formats:
        content_format = "markdown"
    
    try:
        response = await fetch_url(url, content_format)
        return response
    except Exception as e:
        logger.error(f"fetch 失敗: {str(e)}")
        return {
            "status": "error",
            "message": "無法抓取網頁內容，請稍後再試",
            "url": url
        }

# 以下工具被遮蔽，不提供給 MCP Server 使用
# suggest_related_queries 工具
'''
@mcp.tool()
async def suggest_related_queries(query: str, count: int = 5):
    """
    提供與搜尋關鍵字相關的建議查詢。

    Args:
        query: 原始搜尋關鍵字
        count: 返回建議數量 (預設: 5)

    Returns:
        相關建議列表
    """
    if not isinstance(query, str) or not query.strip():
        return {
            "status": "error",
            "message": "請輸入有效的搜尋關鍵字"
        }
    
    count = min(max(1, count), 10)
    
    try:
        # 這裡可以接入更複雜的建議引擎，但為簡單起見，使用預設規則
        suggestions = [
            f"{query} 教學",
            f"{query} 推薦",
            f"{query} 範例",
            f"如何使用 {query}",
            f"{query} 比較",
            f"{query} 優缺點",
            f"{query} 最新",
            f"免費 {query}",
            f"{query} 排行榜",
            f"{query} 問題"
        ]
        
        return {
            "status": "success",
            "query": query,
            "suggestions": suggestions[:count]
        }
    except Exception as e:
        logger.error(f"suggest_related_queries 失敗: {str(e)}")
        return {
            "status": "error",
            "message": "無法生成相關建議",
            "query": query
        }
'''

# get_cache_statistics 工具
'''
@mcp.tool()
def get_cache_statistics():
    """
    獲取快取統計資訊。
    
    Returns:
        包含快取統計資訊的字典
    """
    return get_cache_stats()
'''

# clear_cache 工具
'''
@mcp.tool()
def clear_cache():
    """
    清除所有快取資料。
    
    Returns:
        操作結果
    """
    try:
        search_cache.clear()
        url_cache.clear()
        cache_stats["hits"] = 0
        cache_stats["misses"] = 0
        
        return {
            "status": "success",
            "message": "已清除所有快取資料",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"清除快取失敗: {str(e)}")
        return {
            "status": "error",
            "message": f"清除快取時發生錯誤: {str(e)}"
        }
'''

# set_search_preferences 工具
'''
@mcp.tool()
def set_search_preferences(region: str = None, safe_search: bool = None, max_results: int = None, cache_ttl: int = None):
    """
    設定搜尋偏好參數。
    
    Args:
        region: 搜尋區域代碼 (例如: tw, us, jp)
        safe_search: 是否啟用安全搜尋
        max_results: 最大結果數量
        cache_ttl: 快取存活時間 (秒)
    
    Returns:
        更新後的設定
    """
    try:
        if region is not None:
            if isinstance(region, str) and len(region) <= 5:
                CONFIG["REGION"] = region.lower()
            else:
                return {"status": "error", "message": "區域代碼格式無效"}
                
        if safe_search is not None:
            CONFIG["SAFE_SEARCH"] = bool(safe_search)
            
        if max_results is not None:
            if isinstance(max_results, int) and 1 <= max_results <= 50:
                CONFIG["MAX_RESULTS"] = max_results
            else:
                return {"status": "error", "message": "最大結果數量必須在 1-50 之間"}
                
        if cache_ttl is not None:
            if isinstance(cache_ttl, int) and cache_ttl >= 0:
                CONFIG["CACHE_TTL"] = cache_ttl
                # 更新快取設定
                global search_cache, url_cache
                search_cache = TTLCache(maxsize=CONFIG["CACHE_MAX_SIZE"], ttl=CONFIG["CACHE_TTL"])
                url_cache = TTLCache(maxsize=CONFIG["CACHE_MAX_SIZE"], ttl=CONFIG["CACHE_TTL"])
                # 重置快取統計
                cache_stats["hits"] = 0
                cache_stats["misses"] = 0
            else:
                return {"status": "error", "message": "快取存活時間必須為非負整數"}
        
        return {
            "status": "success",
            "message": "搜尋偏好已更新",
            "current_settings": {
                "region": CONFIG["REGION"],
                "safe_search": CONFIG["SAFE_SEARCH"],
                "max_results": CONFIG["MAX_RESULTS"],
                "cache_ttl": CONFIG["CACHE_TTL"]
            }
        }
    except Exception as e:
        logger.error(f"設定搜尋偏好時發生錯誤: {str(e)}")
        return {
            "status": "error",
            "message": f"更新設定時發生錯誤: {str(e)}"
        }
'''

# 以下是 main 函數，啟動 MCP 服務器
if __name__ == "__main__":
    # 必要套件: pip install mcp httpx beautifulsoup4 python-dotenv cachetools
    mcp.run(transport="stdio")