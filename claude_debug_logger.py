#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Claude 通信除錯日誌模組
用於記錄 Claude 的請求和回應，以及與 DuckDuckGo 的通信內容
"""

import logging
import json
import os
from datetime import datetime

# 確保日誌目錄存在
LOG_DIR = "debug_logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# 設定 Claude 通信日誌
claude_logger = logging.getLogger("claude_debug")
claude_logger.setLevel(logging.DEBUG)

# 創建文件處理器，每天一個日誌文件
log_file = os.path.join(LOG_DIR, f"claude_debug_{datetime.now().strftime('%Y%m%d')}.log")
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)

# 設定日誌格式
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# 添加處理器到日誌記錄器
claude_logger.addHandler(file_handler)

# 同時輸出到控制台
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
claude_logger.addHandler(console_handler)

def log_claude_request(tool_name: str, params: dict):
    """
    記錄 Claude 發送的請求
    
    Args:
        tool_name: 工具名稱
        params: 請求參數
    """
    request_id = datetime.now().strftime('%Y%m%d%H%M%S%f')
    log_data = {
        "request_id": request_id,
        "timestamp": datetime.now().isoformat(),
        "tool": tool_name,
        "params": params
    }
    claude_logger.info(f"[REQUEST] {json.dumps(log_data, ensure_ascii=False)}")
    return request_id

def log_duckduckgo_request(request_id: str, url: str, params: dict, headers: dict):
    """
    記錄發送給 DuckDuckGo 的請求
    
    Args:
        request_id: 請求 ID
        url: 請求 URL
        params: 請求參數
        headers: 請求標頭
    """
    # 不記錄完整的 User-Agent
    safe_headers = headers.copy()
    if 'User-Agent' in safe_headers:
        safe_headers['User-Agent'] = '[REDACTED]'
        
    log_data = {
        "request_id": request_id,
        "timestamp": datetime.now().isoformat(),
        "url": url,
        "params": params,
        "headers": safe_headers
    }
    claude_logger.info(f"[DUCK_REQUEST] {json.dumps(log_data, ensure_ascii=False)}")

def log_duckduckgo_response(request_id: str, status_code: int, response_length: int, results_count: int, error=None):
    """
    記錄從 DuckDuckGo 收到的回應
    
    Args:
        request_id: 請求 ID
        status_code: HTTP 狀態碼
        response_length: 回應長度
        results_count: 結果數量
        error: 錯誤信息（如果有）
    """
    log_data = {
        "request_id": request_id,
        "timestamp": datetime.now().isoformat(),
        "status_code": status_code,
        "response_length": response_length,
        "results_count": results_count
    }
    
    if error:
        log_data["error"] = str(error)
        claude_logger.error(f"[DUCK_RESPONSE] {json.dumps(log_data, ensure_ascii=False)}")
    else:
        claude_logger.info(f"[DUCK_RESPONSE] {json.dumps(log_data, ensure_ascii=False)}")

def log_claude_response(request_id: str, response_data: dict):
    """
    記錄回傳給 Claude 的回應
    
    Args:
        request_id: 請求 ID
        response_data: 回應數據
    """
    # 基本日誌資訊
    log_data = {
        "request_id": request_id,
        "timestamp": datetime.now().isoformat(),
        "response_status": response_data.get("status", "unknown"),
        "results_count": len(response_data.get("results", [])) if "results" in response_data else 0,
        "has_suggestion": "suggestion" in response_data and response_data["suggestion"] is not None,
        # 添加完整的回應內容
        "full_response": response_data
    }
    claude_logger.info(f"[RESPONSE] {json.dumps(log_data, ensure_ascii=False)}")

def save_html_content(request_id: str, query: str, html_content: str):
    """
    保存 HTML 內容到文件
    
    Args:
        request_id: 請求 ID
        query: 搜索查詢
        html_content: HTML 內容
    """
    safe_query = query.replace(' ', '_').replace('/', '_').replace('\\', '_')
    filename = os.path.join(LOG_DIR, f"html_{request_id}_{safe_query}.html")
    
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_content)
        claude_logger.info(f"[HTML_SAVED] 請求 ID: {request_id}, 文件: {filename}")
    except Exception as e:
        claude_logger.error(f"[HTML_ERROR] 保存 HTML 失敗: {str(e)}")
