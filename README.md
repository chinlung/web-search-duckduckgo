# DuckDuckGo Web 搜尋工具

這個專案提供一個 MCP (Model Context Protocol) 伺服器，允許你使用 DuckDuckGo 搜尋引擎搜尋網路，並可選擇性地獲取和摘要找到的 URL 內容。

## 功能特點

* **基本功能**
  * **網路搜尋**：使用 DuckDuckGo 搜尋網路
  * **結果提取**：從搜尋結果中提取標題、URL 和摘要
  * **內容獲取**：獲取搜尋結果中 URL 的內容，並使用 Jina API 轉換為 Markdown 格式
  * **並行獲取**：並行獲取多個 URL，提高處理速度
  * **錯誤處理**：優雅地處理搜尋和獲取過程中的超時和其他潛在錯誤
  * **可配置**：允許設定返回的最大搜尋結果數量
  * **MCP 兼容**：此伺服器設計為與任何 MCP 兼容的客戶端一起使用

* **快取管理功能**
  * **快取統計**：提供快取使用情況的統計資訊
  * **快取清除**：可以清除所有快取資料
  * **智能快取時間**：根據查詢類型自動設定不同的快取時間
    * 新聞類查詢：15分鐘
    * 技術文檔類查詢：24小時
    * 一般查詢：1小時

* **搜尋功能增強**
  * **相關查詢建議**：提供與搜尋關鍵字相關的建議查詢
  * **搜尋偏好設定**：允許自訂搜尋參數，如區域、安全搜尋等
  * **搜尋設定查看**：可以查看當前的所有設定

* **高級搜尋功能**
  * **進階搜尋**：支援過濾和排序功能
  * **結果過濾與排序**：可以按域名、關鍵字過濾和按標題、日期排序
  * **搜尋結果分析**：提供搜尋結果的分析和摘要

* **系統監控功能**
  * **資源監控**：監控系統資源使用情況，包括 CPU、記憶體、網路和磁碟使用情況
  * **健康檢查**：檢查系統各組件是否正常運行

## 使用方法

1. **前置條件**：
   * 安裝 Python 3.11 或更高版本
   * 使用 Poetry 安裝依賴：`poetry install`

2. **Claude Desktop 配置**
   * 如果你使用 Claude Desktop，可以將伺服器添加到 `claude_desktop_config.json` 文件中：
   ```json
   {
       "mcpServers": {
           "web-search-duckduckgo": {
               "command": "poetry",
               "args": [
                   "run",
                   "python",
                   "/path/to/web-search-duckduckgo/main.py"
               ]
           }
       }
   }
   ```

3. **工具**
   * 在你的 MCP 客戶端（例如 Claude）中，你可以使用以下工具：

   * **`search_and_fetch`**：搜尋網路並獲取 URL 內容
     * `query`：搜尋查詢字符串
     * `limit`：返回結果的最大數量（預設：3，最大：10）
     * `content_format`：內容格式（text, markdown, html）
     * `region`：搜尋區域（tw, us, hk, jp 等）

   * **`search`**：僅搜尋網路，不獲取頁面內容
     * `query`：搜尋查詢字符串
     * `limit`：返回結果的最大數量（預設：5，最大：10）
     * `region`：搜尋區域（tw, us, hk, jp 等）

   * **`fetch`**：獲取特定 URL 的內容
     * `url`：要獲取的 URL
     * `content_format`：內容格式（text, markdown, html）

   * **`suggest_related_queries`**：提供相關查詢建議
     * `query`：原始搜尋關鍵字
     * `count`：返回建議數量（預設：5）

   * **`get_cache_statistics`**：獲取快取統計資訊

   * **`clear_cache`**：清除所有快取資料

   * **`set_search_preferences`**：設定搜尋偏好
     * `region`：搜尋區域代碼（例如：tw, us, jp）
     * `safe_search`：是否啟用安全搜尋
     * `max_results`：最大結果數量
     * `cache_ttl`：快取存活時間（秒）

   * **`system_monitor`**：監控系統資源使用情況

   * **`health_check`**：檢查系統各組件是否正常運行

## 系統需求

* Python 3.11+
* 依賴套件 (透過 Poetry 管理)：
  * beautifulsoup4
  * httpx
  * mcp[cli]
  * python-dotenv
  * cachetools
  * psutil (用於系統監控功能)
  * requests

## 授權

本專案採用 MIT 授權。
