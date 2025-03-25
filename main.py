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
        formatted_query = quote(query)
        
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

@mcp.tool()
def get_cache_statistics():
    """
    獲取快取統計資訊。
    
    Returns:
        包含快取統計資訊的字典
    """
    return get_cache_stats()

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

@mcp.tool()
def get_current_settings():
    """
    獲取當前搜尋和系統設定。
    
    Returns:
        當前設定
    """
    try:
        return {
            "status": "success",
            "settings": {
                "search": {
                    "region": CONFIG["REGION"],
                    "safe_search": CONFIG["SAFE_SEARCH"],
                    "max_results": CONFIG["MAX_RESULTS"]
                },
                "cache": {
                    "max_size": CONFIG["CACHE_MAX_SIZE"],
                    "ttl": CONFIG["CACHE_TTL"]
                },
                "request": {
                    "max_concurrent": CONFIG["MAX_CONCURRENT_REQUESTS"],
                    "timeout": {
                        "jina": CONFIG["JINA_TIMEOUT"],
                        "raw_html": CONFIG["RAW_HTML_TIMEOUT"]
                    }
                },
                "content": {
                    "length_limit": CONFIG["CONTENT_LENGTH_LIMIT"]
                }
            }
        }
    except Exception as e:
        logger.error(f"獲取當前設定時發生錯誤: {str(e)}")
        return {
            "status": "error",
            "message": f"獲取設定時發生錯誤: {str(e)}"
        }

def filter_and_sort_results(results: List[Dict[str, Any]], 
                           filters: Dict[str, Any] = None, 
                           sort_by: str = None, 
                           reverse: bool = False) -> List[Dict[str, Any]]:
    """
    過濾和排序搜尋結果。
    
    Args:
        results: 原始搜尋結果列表
        filters: 過濾條件 (例如: {"domain": "example.com", "keywords": ["python", "tutorial"]})
        sort_by: 排序依據 (可選: "relevance", "date", "title")
        reverse: 是否反向排序
        
    Returns:
        過濾和排序後的結果列表
    """
    filtered_results = results.copy()
    
    # 應用過濾條件
    if filters:
        # 按域名過濾
        if "domain" in filters and filters["domain"]:
            domain = filters["domain"].lower()
            filtered_results = [r for r in filtered_results if domain in r.get("url", "").lower()]
            
        # 按關鍵字過濾 (標題和摘要中包含所有指定關鍵字)
        if "keywords" in filters and filters["keywords"]:
            keywords = [k.lower() for k in filters["keywords"]]
            filtered_results = [
                r for r in filtered_results 
                if all(
                    k in r.get("title", "").lower() or 
                    k in r.get("snippet", "").lower() 
                    for k in keywords
                )
            ]
            
        # 按排除關鍵字過濾 (標題和摘要中不包含任何指定關鍵字)
        if "exclude_keywords" in filters and filters["exclude_keywords"]:
            exclude_keywords = [k.lower() for k in filters["exclude_keywords"]]
            filtered_results = [
                r for r in filtered_results 
                if not any(
                    k in r.get("title", "").lower() or 
                    k in r.get("snippet", "").lower() 
                    for k in exclude_keywords
                )
            ]
    
    # 應用排序
    if sort_by:
        if sort_by == "relevance":
            # 預設已按相關性排序，不需額外操作
            pass
        elif sort_by == "title":
            filtered_results.sort(key=lambda x: x.get("title", "").lower(), reverse=reverse)
        elif sort_by == "date":
            # 嘗試從摘要中提取日期，如果無法提取則保持原排序
            import re
            from datetime import datetime
            
            def extract_date(result):
                # 嘗試從摘要中提取日期
                snippet = result.get("snippet", "")
                # 常見日期格式: YYYY-MM-DD, YYYY/MM/DD, DD/MM/YYYY, Month DD, YYYY
                date_patterns = [
                    r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',  # YYYY-MM-DD or YYYY/MM/DD
                    r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
                    r'([A-Z][a-z]+ \d{1,2}, \d{4})'    # Month DD, YYYY
                ]
                
                for pattern in date_patterns:
                    match = re.search(pattern, snippet)
                    if match:
                        try:
                            date_str = match.group(1)
                            # 嘗試解析日期，如果失敗則返回默認值
                            if '-' in date_str and date_str.count('-') == 2:
                                parts = date_str.split('-')
                                if len(parts[0]) == 4:  # YYYY-MM-DD
                                    return datetime.strptime(date_str, '%Y-%m-%d')
                                else:  # DD-MM-YYYY
                                    return datetime.strptime(date_str, '%d-%m-%Y')
                            elif '/' in date_str and date_str.count('/') == 2:
                                parts = date_str.split('/')
                                if len(parts[0]) == 4:  # YYYY/MM/DD
                                    return datetime.strptime(date_str, '%Y/%m/%d')
                                else:  # DD/MM/YYYY
                                    return datetime.strptime(date_str, '%d/%m/%Y')
                            elif ',' in date_str:  # Month DD, YYYY
                                return datetime.strptime(date_str, '%B %d, %Y')
                        except ValueError:
                            pass
                
                # 如果無法提取日期，返回一個非常早的日期作為默認值
                return datetime(1900, 1, 1)
            
            filtered_results.sort(key=extract_date, reverse=reverse)
    
    return filtered_results

@mcp.tool()
async def advanced_search(query: str, 
                         limit: int = 10, 
                         region: str = None, 
                         filters: Dict[str, Any] = None, 
                         sort_by: str = None, 
                         reverse_sort: bool = False):
    """
    執行高級搜尋，支持過濾和排序。
    
    Args:
        query: 搜尋關鍵字
        limit: 結果數量限制
        region: 搜尋區域代碼
        filters: 過濾條件 (例如: {"domain": "example.com", "keywords": ["python", "tutorial"]})
        sort_by: 排序依據 (可選: "relevance", "date", "title")
        reverse_sort: 是否反向排序
        
    Returns:
        過濾和排序後的搜尋結果
    """
    if not query or not isinstance(query, str):
        return {
            "status": "error",
            "message": "請提供有效的搜尋關鍵字"
        }
    
    # 使用默認區域如果未指定
    if not region:
        region = CONFIG["REGION"]
    
    try:
        # 先獲取基本搜尋結果
        search_response = await search_duckduckgo(
            query, 
            limit=max(limit * 2, 20),  # 獲取更多結果以便過濾
            region=region, 
            safe_search=CONFIG["SAFE_SEARCH"]
        )
        
        if search_response["status"] != "success":
            return search_response
        
        results = search_response["results"]
        
        # 應用過濾和排序
        filtered_results = filter_and_sort_results(
            results=results,
            filters=filters,
            sort_by=sort_by,
            reverse=reverse_sort
        )
        
        # 限制結果數量
        filtered_results = filtered_results[:limit]
        
        # 提供友好的提示，如果過濾後沒有結果
        if len(filtered_results) == 0 and filters:
            suggestions = []
            if "domain" in filters and filters["domain"]:
                suggestions.append(f"移除域名過濾條件 '{filters['domain']}'")
            if "keywords" in filters and filters["keywords"]:
                suggestions.append(f"移除或減少關鍵字過濾條件 {filters['keywords']}")
            if "exclude_keywords" in filters and filters["exclude_keywords"]:
                suggestions.append(f"移除或減少排除關鍵字條件 {filters['exclude_keywords']}")
            
            return {
                "status": "success",
                "query": query,
                "original_count": len(results),
                "filtered_count": 0,
                "results": [],
                "filters_applied": filters,
                "sort_by": sort_by,
                "reverse_sort": reverse_sort,
                "message": "未找到符合過濾條件的結果",
                "suggestions": suggestions
            }
        
        return {
            "status": "success",
            "query": query,
            "original_count": len(results),
            "filtered_count": len(filtered_results),
            "results": filtered_results,
            "filters_applied": filters,
            "sort_by": sort_by,
            "reverse_sort": reverse_sort
        }
    except Exception as e:
        logger.error(f"高級搜尋失敗: {str(e)}")
        return {
            "status": "error",
            "message": f"執行高級搜尋時發生錯誤: {str(e)}",
            "query": query
        }

def analyze_search_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    分析搜尋結果，提取關鍵資訊並生成摘要。
    
    Args:
        results: 搜尋結果列表
        
    Returns:
        分析結果
    """
    if not results:
        return {
            "status": "error",
            "message": "沒有可分析的搜尋結果"
        }
    
    try:
        # 提取所有域名
        domains = {}
        for result in results:
            url = result.get("url", "")
            if url:
                # 提取域名
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                domains[domain] = domains.get(domain, 0) + 1
        
        # 按出現頻率排序域名
        top_domains = sorted(domains.items(), key=lambda x: x[1], reverse=True)
        
        # 提取常見關鍵字 (簡單實現，可以用更複雜的 NLP 方法改進)
        import re
        from collections import Counter
        
        # 停用詞列表 (可以擴展)
        stop_words = set([
            "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
            "at", "from", "by", "about", "as", "in", "to", "for", "with", "on",
            "of", "is", "are", "was", "were", "be", "been", "being", "have", "has",
            "had", "do", "does", "did", "can", "could", "will", "would", "shall",
            "should", "may", "might", "must", "this", "that", "these", "those",
            "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them"
        ])
        
        # 合併所有標題和摘要
        all_text = " ".join([
            f"{result.get('title', '')} {result.get('snippet', '')}" 
            for result in results
        ]).lower()
        
        # 提取單詞
        words = re.findall(r'\b[a-z]{3,}\b', all_text)
        words = [word for word in words if word not in stop_words]
        
        # 計算頻率
        word_counts = Counter(words)
        top_keywords = word_counts.most_common(10)
        
        # 生成簡單摘要
        summary = "根據搜尋結果分析，"
        
        # 添加域名信息
        if top_domains:
            top_domains_str = ", ".join([f"{domain} ({count}次)" for domain, count in top_domains[:3]])
            summary += f"最常出現的網站是 {top_domains_str}。"
        
        # 添加關鍵字信息
        if top_keywords:
            top_keywords_str = ", ".join([f"{word} ({count}次)" for word, count in top_keywords[:5]])
            summary += f"主要關鍵字包括: {top_keywords_str}。"
        
        # 計算平均標題和摘要長度
        avg_title_len = sum(len(result.get("title", "")) for result in results) / len(results)
        avg_snippet_len = sum(len(result.get("snippet", "")) for result in results) / len(results)
        
        return {
            "status": "success",
            "result_count": len(results),
            "top_domains": top_domains[:5],
            "top_keywords": top_keywords,
            "avg_title_length": round(avg_title_len, 1),
            "avg_snippet_length": round(avg_snippet_len, 1),
            "summary": summary
        }
    except Exception as e:
        logger.error(f"分析搜尋結果時發生錯誤: {str(e)}")
        return {
            "status": "error",
            "message": f"分析搜尋結果時發生錯誤: {str(e)}"
        }

@mcp.tool()
async def summarize_search_results(query: str, limit: int = 10, region: str = None):
    """
    搜尋並提供結果摘要分析。
    
    Args:
        query: 搜尋關鍵字
        limit: 結果數量限制
        region: 搜尋區域代碼
        
    Returns:
        搜尋結果分析和摘要
    """
    if not query or not isinstance(query, str):
        return {
            "status": "error",
            "message": "請提供有效的搜尋關鍵字"
        }
    
    # 使用默認區域如果未指定
    if not region:
        region = CONFIG["REGION"]
    
    try:
        # 先獲取搜尋結果
        search_response = await search_duckduckgo(
            query, 
            limit=limit,
            region=region, 
            safe_search=CONFIG["SAFE_SEARCH"]
        )
        
        if search_response["status"] != "success":
            return search_response
        
        results = search_response["results"]
        
        # 分析結果
        analysis = analyze_search_results(results)
        
        return {
            "status": "success",
            "query": query,
            "result_count": len(results),
            "analysis": analysis,
            "results": results[:3]  # 只返回前三個結果作為示例
        }
    except Exception as e:
        logger.error(f"摘要搜尋結果失敗: {str(e)}")
        return {
            "status": "error",
            "message": f"摘要搜尋結果時發生錯誤: {str(e)}",
            "query": query
        }

def get_system_stats() -> Dict[str, Any]:
    """
    獲取系統資源使用情況。
    
    Returns:
        系統資源統計資訊
    """
    import os
    import time
    import platform
    import psutil
    from datetime import datetime, timedelta
    
    try:
        # 獲取基本系統信息
        system_info = {
            "platform": platform.system(),
            "platform_version": platform.version(),
            "python_version": platform.python_version(),
            "processor": platform.processor()
        }
        
        # 獲取記憶體使用情況
        memory = psutil.virtual_memory()
        memory_info = {
            "total": f"{memory.total / (1024 ** 3):.2f} GB",
            "available": f"{memory.available / (1024 ** 3):.2f} GB",
            "used": f"{memory.used / (1024 ** 3):.2f} GB",
            "percent": f"{memory.percent}%"
        }
        
        # 獲取 CPU 使用情況
        cpu_info = {
            "physical_cores": psutil.cpu_count(logical=False),
            "total_cores": psutil.cpu_count(logical=True),
            "current_usage_percent": f"{psutil.cpu_percent()}%",
            "per_core": [f"{x}%" for x in psutil.cpu_percent(percpu=True)]
        }
        
        # 獲取網路使用情況
        net_io = psutil.net_io_counters()
        net_info = {
            "bytes_sent": f"{net_io.bytes_sent / (1024 ** 2):.2f} MB",
            "bytes_received": f"{net_io.bytes_recv / (1024 ** 2):.2f} MB",
            "packets_sent": net_io.packets_sent,
            "packets_received": net_io.packets_recv
        }
        
        # 獲取磁碟使用情況
        disk = psutil.disk_usage('/')
        disk_info = {
            "total": f"{disk.total / (1024 ** 3):.2f} GB",
            "used": f"{disk.used / (1024 ** 3):.2f} GB",
            "free": f"{disk.free / (1024 ** 3):.2f} GB",
            "percent": f"{disk.percent}%"
        }
        
        # 獲取進程信息
        process = psutil.Process(os.getpid())
        process_info = {
            "pid": process.pid,
            "memory_usage": f"{process.memory_info().rss / (1024 ** 2):.2f} MB",
            "cpu_percent": f"{process.cpu_percent()}%",
            "threads": process.num_threads(),
            "running_time": str(timedelta(seconds=time.time() - process.create_time()))
        }
        
        return {
            "status": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "system": system_info,
            "memory": memory_info,
            "cpu": cpu_info,
            "network": net_info,
            "disk": disk_info,
            "process": process_info
        }
    except Exception as e:
        logger.error(f"獲取系統統計信息時發生錯誤: {str(e)}")
        return {
            "status": "error",
            "message": f"獲取系統統計信息時發生錯誤: {str(e)}"
        }

@mcp.tool()
def system_monitor():
    """
    監控系統資源使用情況。
    
    Returns:
        系統資源使用統計
    """
    return get_system_stats()

@mcp.tool()
def health_check():
    """
    執行系統健康檢查，檢查所有組件是否正常運行。
    
    Returns:
        健康檢查結果
    """
    try:
        health_status = {
            "status": "success",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "components": {}
        }
        
        # 檢查快取狀態
        cache_stats_result = get_cache_stats()
        health_status["components"]["cache"] = {
            "status": "healthy",
            "details": {
                "search_cache_size": len(search_cache),
                "url_cache_size": len(url_cache)
            }
        }
        
        # 檢查系統資源
        system_stats = get_system_stats()
        if system_stats["status"] == "success":
            memory_percent = float(system_stats["memory"]["percent"].replace("%", ""))
            cpu_percent = float(system_stats["cpu"]["current_usage_percent"].replace("%", ""))
            disk_percent = float(system_stats["disk"]["percent"].replace("%", ""))
            
            memory_status = "warning" if memory_percent > 80 else "healthy"
            cpu_status = "warning" if cpu_percent > 80 else "healthy"
            disk_status = "warning" if disk_percent > 80 else "healthy"
            
            health_status["components"]["system_resources"] = {
                "status": "warning" if any(s == "warning" for s in [memory_status, cpu_status, disk_status]) else "healthy",
                "details": {
                    "memory": {
                        "status": memory_status,
                        "usage": system_stats["memory"]["percent"]
                    },
                    "cpu": {
                        "status": cpu_status,
                        "usage": system_stats["cpu"]["current_usage_percent"]
                    },
                    "disk": {
                        "status": disk_status,
                        "usage": system_stats["disk"]["percent"]
                    }
                }
            }
        else:
            health_status["components"]["system_resources"] = {
                "status": "unknown",
                "details": {
                    "error": "無法獲取系統資源信息"
                }
            }
        
        # 檢查網路連接
        try:
            import httpx
            async def check_network():
                async with httpx.AsyncClient() as client:
                    response = await client.get("https://duckduckgo.com/", timeout=5.0, follow_redirects=True)
                    return response.status_code == 200
            
            import asyncio
            network_check_result = asyncio.run(check_network())
            
            health_status["components"]["network"] = {
                "status": "healthy" if network_check_result else "error",
                "details": {
                    "duckduckgo_reachable": network_check_result
                }
            }
        except Exception as e:
            health_status["components"]["network"] = {
                "status": "error",
                "details": {
                    "error": str(e)
                }
            }
        
        # 總體健康狀態
        component_statuses = [comp["status"] for comp in health_status["components"].values()]
        if any(status == "error" for status in component_statuses):
            health_status["overall"] = "error"
        elif any(status == "warning" for status in component_statuses):
            health_status["overall"] = "warning"
        else:
            health_status["overall"] = "healthy"
        
        return health_status
    except Exception as e:
        logger.error(f"健康檢查時發生錯誤: {str(e)}")
        return {
            "status": "error",
            "message": f"執行健康檢查時發生錯誤: {str(e)}"
        }

if __name__ == "__main__":
    # 必要套件: pip install mcp httpx beautifulsoup4 python-dotenv cachetools
    mcp.run(transport="stdio")