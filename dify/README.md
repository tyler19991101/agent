# Dify Secretary Setup

這個專案的 Python runtime 已經假設 Dify app 會做一件事：收到使用者目標後，回傳一個符合固定 JSON contract 的規劃結果。  
你要在 Dify Studio 建立一個 `Chatflow` app，因為官方文件指出 Chatflow 支援對話、memory、變數與多節點編排，適合這種秘書代理場景。  
參考：
- [Create Application](https://docs.dify.ai/versions/3-0-x/en/user-guide/application-orchestrate/creating-an-application)
- [Chatflow / Workflow concepts](https://docs.dify.ai/en/guides/workflow/node/start)
- [API Access / develop with APIs](https://docs.dify.ai/ja/use-dify/publish/developing-with-apis)

## 1. 在 Dify 建立 app

1. 進入 Dify Studio。
2. 建立新應用，類型選 `Chatflow`。
3. 名稱建議填：`LINE Secretary Agent`。
4. Model 選你要的模型。
   - 若你要沿用前面思路，可選 Gemini 1.5 Pro / 1.5 Flash。
   - 若你要更穩定的 JSON，可優先選較擅長結構輸出的模型。

## 2. Chatflow 最小節點

v1 先做最小可用版本，節點只要：

`Start -> LLM -> End`

設定原則：
- `Start` 使用內建的 `sys.query` 作為使用者輸入。
- `LLM` 節點開啟 conversation memory。
- `End` 直接輸出 LLM 文字結果。

## 3. 在 Dify 開啟工具

你這個秘書代理在 Dify 端至少要能：
- 搜尋網頁
- 閱讀網頁內容
- 讀 YouTube 字幕

所以請在 Dify workspace 的 Tools / Plugins 裡啟用你環境可用的等價工具：
- Google Search 或其他 search tool
- Web Scraper / Firecrawl / Browser 類工具
- YouTube Transcript 類工具

注意：工具名稱會隨插件版本和部署方式不同而不同，但能力面要對齊上面三類。

## 4. 在 LLM 節點貼上 prompt

把 [`SYSTEM_PROMPT.md`](./SYSTEM_PROMPT.md) 的內容貼進 LLM 節點的 system prompt。

這份 prompt 的目的是：
- 讓 Dify 永遠只輸出 JSON
- 對齊你本地 Python runtime 在 [`secretary_agent/dify_client.py`](../secretary_agent/dify_client.py) 的解析邏輯
- 把旅遊規劃、比價、補資料、確認節點都約束成固定欄位

## 5. 發布並取得 API Key

1. 在 Dify app 右上角 Publish。
2. 左側進 `API Access`。
3. 建立一組 API Key。
4. 把這個值填進本地 `.env.bot` 的 `DIFY_API_KEY`。

## 6. 本地環境檔

複製 `.env.bot.example` 成 `.env.bot`，至少填：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `DIFY_API_KEY`

如果你不是用 Dify SaaS，改掉：
- `DIFY_BASE_URL`

## 7. 驗證 Dify 是否回傳正確格式

先跑：

```bash
python3 scripts/smoke_test_dify.py "我要到泰國旅遊，預算四萬，幫我排四天三夜"
```

如果成功，腳本會印出解析後的 JSON 與重點欄位。
如果失敗，通常是：
- Dify app 沒發布
- API Key 錯
- prompt 沒貼對
- LLM 沒有穩定輸出 JSON

## 8. 最後啟動 LINE bot

```bash
./run_agentbot.sh
```

如果 Dify 還沒配好就直接啟動，LINE bot 會收到任務，但 worker 在呼叫 Dify 時會失敗。
