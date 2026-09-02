# openevidence-tools

*[English version → README.md](README.md)*

兩個互相搭配的工具，讓 [OpenEvidence](https://www.openevidence.com) 成為**可信賴**的證據來源：

1. **一個 MCP server**，把 OpenEvidence 以四個 tool 的形式接給任何 MCP client（Claude Code 等）：
   `ask_openevidence`、`list_recent_oe_queries`、`fetch_oe_result`、`ask_oe_followup`。
2. **`/oe-verify` skill**，逐一把 OE 回答裡的**每一條 citation** 對回原始文獻查核，並找出 OE 漏掉的證據。

**為什麼兩個都需要？** OpenEvidence 回答臨床問題很快，但它本質上是 RAG pipeline（向量檢索 + LLM
合成），即使論述本身正確，citation 仍可能標錯。最典型的失效模式是 **transitive citation（轉引）**：
OE 檢索到某篇 **review 全文**的一個片段，而那段話其實是該 review 在引用*別人的* trial 數據，OE 卻把
這個數字算在 review 頭上。論述是真的，出處是錯的。**OE 的回答未經查核前，絕不可直接吸收** —— 這正是
`/oe-verify` 存在的理由。

---

## MCP server 提供的 tools

| Tool | 用途 |
|---|---|
| `ask_openevidence(question, preserve_citations=False)` | 送出新的臨床問題，回傳 evidence-based 答案 + citations；自動記錄 article_id 以便復原。 |
| `list_recent_oe_queries(limit=20)` | 列出近期送出的問題（ts、article_id、question），用來在中斷後找回 article_id。 |
| `fetch_oe_result(article_id, preserve_citations=False)` | 用 ID 重新取回先前的答案。這是 GET，**不消耗**提問額度。 |
| `ask_oe_followup(parent_article_id, followup_question, preserve_citations=False)` | 在同一個對話串裡追問；OE 會自動把前一輪 Q&A 展開成 context。 |

### `preserve_citations=True` —— `/oe-verify` 所依賴的 provenance 通道

設成 `True` 時，回答會保留內文的 `[N]` 標記，並在結尾附上 `## Citation Data` JSON 區塊。每一筆都帶著
查核所需的 metadata：`title`、`doi`、`pmid`、`journal`、`authors`、`publication_types`、
`impact_score`、`recency_days`、`is_guideline`、`url`，外加兩個 provenance 欄位：

- **`origin`** —— 該 citation 片段是從哪個 RAG corpus 檢索出來的。OE 以 ROT-1 混淆傳送（每個 byte +1，
  所以 `_` 會變成反引號）；server 會解回來，例如
  `` mbodfu`gvmmufyu`tdsbqfe`w3 `` → `lancet_fulltext_scraped_v2`。
- **`origin_risk`** —— 由 `origin` 推導出的轉引風險分級：

  | 風險級 | Corpora | 意義 |
  |---|---|---|
  | **HIGH** | `*_fulltext_scraped_*`（lancet、nejm、aan…） | 爬取的 **review 全文** —— 被引用的那句話常常是 review 在引*別人*的 trial。**這是 OE 最主要的失效模式。** |
  | **MEDIUM** | `wiley_cdsr_fulltext`（Cochrane）、`wiley_research_fulltext`、`guidelines_fulltext_*`、`media_annotated_gemini` | 出版社全文／AI 標註圖表 —— 是真實全文片段，但仍不保證是該論文自己的發現。 |
  | **LOW** | `pubmed_abstracts_*` | Abstract 層級，誤植空間小。 |
  | **UNKNOWN** | 無 span metadata | 無法得知出處 —— 一律視為 MEDIUM，**絕不**當成 LOW。 |

`/oe-verify` 用 `origin_risk` 決定 attribution check 該往哪裡打：HIGH/MEDIUM 片段又支撐**量化**論述
（effect size、HR/OR/RR、CI、%、p 值、n）就是危險區，每一筆都要追查那個數字究竟是不是**該論文自己的**發現。

> 同一篇論文可能合理地佔用多個 `[N]` —— OE 是**每個檢索片段**給一個 index，不是每篇論文一個
> （實測：10 個 index 對應 7 篇論文）。Server 以位置對應把標記映射到 citation 物件（`raw_text` 中第 i 個
> `[N]` 出現 == document order 中第 i 個 citation 物件）；一旦數量對不上，就**明確降級標示**而不是用猜的。

---

## `/oe-verify` skill

對單一則 OE 回答做三層查核（`skills/oe-verify/SKILL.md`）：

- **Step 0.5 — 存在性 gate**：在任何語意比對之前，先把每個 DOI 丟去 CrossRef 查。DOI 解不出來的直接判
  `FABRICATED`，不浪費 agent token。
- **Layer 1 — 逐條 citation 正確性**（平行、便宜的 agents）：到 Semantic Scholar / PubMed 查出該論文，
  比對 OE 的論述與原文實際說法；遇到 HIGH/MEDIUM provenance 的量化論述，執行專門抓轉引的 attribution check。
- **Layer 2 — 完整性**（一個能力較強的 agent）：以最新的 systematic review / meta-analysis 為錨點，
  雙向檢查 OE **漏掉**什麼，並加上對照你自身臨床情境的 applicability 檢查。
- **Layer 3 — 內部矛盾掃描**：OE 自己引的那些論文彼此有沒有互相打臉？僅在 ≥2 篇論文對**同一 outcome**
  提出量化論述時才啟動。

輸出是一份精簡的 Markdown 報告：逐條 citation 判定表（✅ 正確、⚠️ 誇大、❌ 曲解、🚫 捏造、
↗️ 轉引、🔄 二手未追溯、❓ 無法查證）加上證據全景摘要。

---

## 需求

- **一個 OpenEvidence 帳號**（OpenEvidence 面向臨床工作者）。登入由你自己在平常用的瀏覽器完成。
- **一個本機 browser-bridge daemon。** 這個 server 不爬蟲、也不偽造任何東西：它把每一次 OpenEvidence
  API 呼叫，當成 page-context `fetch()` 跑在**你自己那個已登入的 openevidence.com 分頁裡**，透過一支
  監聽 `127.0.0.1` 的小型本機 daemon 驅動。所以每個 request 都走**你自己的帳號與 session** —— 跟你親手
  在那個分頁點下去完全一樣。參考實作對接的是
  **[Kimi WebBridge](https://www.kimi.com/features/webbridge)** —— Moonshot Kimi 桌面版內建的
  瀏覽器橋接（預設 endpoint `http://127.0.0.1:10086/command`）。**注意有同名的第三方專案，其中部分
  會開一個未認證的 10086 port，本 repo 指的是官方那個。** 只要能講同一套簡單的
  `{action, args, session}` JSON 協定，把 `OE_BRIDGE_URL` 指過去即可。OpenEvidence 沒有公開 API，
  且 session cookie 是 httpOnly，這正是為什麼要用分頁內 fetch 而不是一般 HTTP client。
- **Python 3.10+** 以及 `requirements.txt` 裡的相依套件（`mcp`，已鎖 `<2`，理由見下）。
- **Windows、macOS、Linux 皆可。** 非 Windows 有一個限制：那些平台上 daemon 沒有可獨立執行的
  binary（Kimi 桌面版本身就是 daemon），所以**請先自己把 bridge 開起來再用這個 server** —— 自動拉起
  只在 Windows、或你把 `OE_BRIDGE_BIN` 指向一支吃 `start` 參數的執行檔時才有效。瀏覽器自動啟動三個
  平台都可用。

> **`mcp` 必須鎖在 2.0 以下。** mcp 2.0 移除了 `mcp.server.fastmcp`，沒鎖版本的全新安裝會裝到 2.x，
> `server.py` 在 import 當下就死於 `ModuleNotFoundError` —— 而 MCP client 只會顯示
> `Failed to connect — Connection closed`，害你往完全錯的方向找。`requirements.txt` 已經鎖好；
> 手動安裝的話請自己也鎖。

> **🔒 把要害網站鎖在 bridge 碰不到的地方。** WebBridge 操作的是你**已經登入的真實**瀏覽器，
> 而走 `chrome.debugger` API 的那類 bridge **一次就握有你所有網站的登入身分**——這正是它好用的原因。
> 如果你同時讓 AI agent 開這個瀏覽器做事，建議把網銀、券商、信箱擋在它碰不到的地方，這樣被 prompt
> injection 牽著走的 agent 也進不去。我為此做了一個小工具：
> **[kimi-webbridge-lockdown](https://github.com/drpwchen/kimi-webbridge-lockdown)**（擴充設定裡的
> 逐站「網站存取權」擋不住 debugger 型 bridge，要從瀏覽器政策層下手；這工具幫你設好，有引導、可還原）。

### 設定（環境變數）

| 變數 | 預設值 | 用途 |
|---|---|---|
| `OE_BRIDGE_URL` | `http://127.0.0.1:10086/command` | browser-bridge daemon 的 endpoint。 |
| `OE_BRIDGE_BIN` | Windows 為 `~/.kimi-webbridge/bin/kimi-webbridge.exe`；**其他平台不設** | daemon 未啟動時要自動拉起的執行檔。不設＝不自動拉起，請自己開 bridge。 |
| `OE_BROWSER_EXE` | 依平台自動偵測（Edge/Chrome/Chromium） | bridge extension 未連線（＝瀏覽器沒開）時要自動啟動的瀏覽器。 |
| `OE_DATA_DIR` | `~/.openevidence` | query log、rate log 與送出間隔狀態的寫入位置。 |
| `OE_SUBMIT_SPACING_SEC` | `700`（約 11.7 分鐘） | 兩次送出問題之間的最小實際間隔。要調低，請先用你自己帳號的實測數據 —— 見下方說明。 |

### 安裝

```bash
pip install -r requirements.txt

# 註冊到 MCP client，例如 Claude Code：
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py

# 把 skills/oe-verify/ 放到你的 client 會掃描 skill 的位置。
```

在平常用的瀏覽器登入 openevidence.com 一次即可。之後 server 會自我修復：daemon 沒跑會自動拉起、
瀏覽器沒開會自動啟動（該瀏覽器要裝好 bridge extension），並自動開啟或重用一個 OpenEvidence 分頁。

---

## 負責任的使用方式

- **你自己的帳號、你自己的 session。** 這個工具自動化的是*你*已登入的瀏覽器分頁。請只用在你本來就有權
  使用的帳號上，並遵守 OpenEvidence 的服務條款。
- **尊重額度 —— 那是「間隔」，不是「配額桶」。** 2026-09 網站改版後，OpenEvidence 不再以「每小時 N 題」
  節流，而是要求兩次送出之間有一段很長的**最小間隔**，而且被節流掉的請求似乎會*重新啟動*這段間隔，
  不是免費的。單一帳號實測（2026-09-02）：間隔 ≥11 分鐘的送出全部成功（11、13、14、16 分鐘，4/4）；
  間隔 6 分鐘失敗，幾分鐘內連發的批次也全部失敗。因此 server 會把送出序列化，並依
  `OE_SUBMIT_SPACING_SEC`（預設 700 秒）配速，且把上次嘗試寫進 `OE_DATA_DIR/submit_state.json`，
  **所以重開 server 不會把計時歸零**。只有新問題與追問會被配速，重新取回舊答案免費。
- **收到 429 千萬不要馬上重試。** OE 自己回的 `Expected available in 0 seconds` 永遠是 0，不能用。
  照它重試不會恢復，只會延長鎖定。所以 server 改成回報「下次什麼時候可以再送」而不重試 ——
  你自己手動操作時也請照做。
- **臨床使用前務必查核。** 在你要依賴某個 OE 答案之前，先跑 `/oe-verify`（或自行核對原始文獻）。
  這就是本 repo 的全部意義。
- **這不是醫療建議。** 這些工具協助臨床工作者評估證據，不能取代臨床判斷，其輸出也不構成醫療建議。

---

## 當 OpenEvidence 改版時

這裡沒有公開 API —— server 打的是網站自己在打的私有 endpoint，所以網站改版就可能整條路徑壞掉。
**2026-08-26 那次改版**（新 sidebar、Discover、Tools、voice mode）就是一次全滅。五處 drift 都已在
0.3.0 修好；值得寫下來，因為同樣形狀的問題還會再出現：

| 症狀 | 真正原因 | 修法 |
|---|---|---|
| 送出永遠 `429 throttled` | 節流窗口被拉長到約 10 分鐘，**而且失敗的請求也會續命**。舊碼收到 429 後只等 2 秒就重試（OE 自己回的 `Expected available in 0 seconds` 永遠是 0，不可信），等於自己把帳號鎖死 | 改成跨程序的最小送出間隔；收到 429 直接回報下次可送的時間，不重試 |
| payload 被拒 | `variant_configuration_file` 由 `minimal` 改為 `prod`；`component_config_version` 欄位已移除 | 對齊網站現行 payload |
| 答案開頭混進一段 JSON | 生成步驟元件由 `Thinking` 改名 `InlineGenerationStep`，舊碼只跳過前者 | 兩個都跳過 |
| 引用清單整個消失 | `structured_article.citations` 欄位被拿掉 | 改由 per-span citation 物件推導；舊欄位保留給改版前的文章 |
| 有表格或圖片的答案，引用退化成無 PMID、無 provenance | 表格／圖片元件在 `raw_text` 展開成 `<mdtable>`／`<visual>` 區塊，裡面的 `[N]` 數量對不上元件 span 的 citation 物件數（實測一篇文章 7 個 marker 對 3 個物件），撞破引用比對的 1:1 前提，整篇答案因此退化 | 兩邊都排除元件區塊後再比對；只出現在元件裡的編號明確標成 metadata unavailable，不猜 |

`<follow-up-question>`／`<follow-up-topic>`／`<visual>` 也是改版新增的包裝標籤，舊的
「Would you like to explore」句子比對已經抓不到，尾巴的 chrome 因此漏進正文。兩種形式現在都會清掉。

之後如果又壞了，最快的路就是這次用的方法：從自己瀏覽器的 network 面板看**網站本身**送了什麼，
再跟這個 server 送的東西對照。

---

## 該給的致謝 — htlin222

這個專案一開始是獨立長出來的：我想把 OpenEvidence 的答案接進自己的筆記流程，第一版是用自己寫的
瀏覽器自動化去操作網站。那一版被 bot detection 擋死，我在找出路的過程中才看到
**[Hsieh-Ting Lin (htlin222)](https://github.com/htlin222)** 的作品。這個 repo 現在做得好的地方，
很多來自他近期的更新，值得把來源講清楚。

**[audit-oe-skill](https://github.com/htlin222/audit-oe-skill)**（MIT）貢獻了整個 repo 的核心洞見：
OpenEvidence 的 `origin` 欄位是 ROT-1 混淆過的、它標示這段 chunk 是從哪個 RAG corpus 取出來的，
而 corpus 本身就能**預測**這條 citation 被錯置的機率。transitive citation（綜述引用了別人的數字、
OE 卻把功勞算給綜述）這個 failure mode 是他命名的；我們採用的驗證架構也是他的：每條 citation 一個
平行的便宜 agent、對綜述全文中的量化宣稱做溯源、再用一個較強的 agent 檢查 OE 自己引的文獻彼此是否
矛盾，並分成 CRITICAL / WARNING / NOTE 三級。我們的 `origin_risk` 分層就是他的 risk table；我們的
Layer 1 與 Layer 3 就是他的 Phase 3 與 Phase 4。讀我們的驗證報告時，裡面大多數的提問，其實是他的提問。

**[openevidence-mcp](https://github.com/htlin222/openevidence-mcp)**（Apache-2.0）貢獻了 transport
的想法，讓我們脫困：OpenEvidence 沒有公開 API、session cookie 又是 httpOnly，所以不要去爬它 ——
直接讓請求跑在臨床醫師自己那個已登入的瀏覽器分頁裡。實作是我們自己寫的（驅動的 daemon 也不同），
但方向是他指的。

我們自己寫了一層 tool layer 而不是 fork，理由很實際：verifier 需要 `publication_types`、
`impact_score`、`recency_score`、`is_guideline` 與 per-chunk `origin` 直接從 MCP contract 回傳，
而且我們驅動的是通用型 browser daemon，不是專屬 extension。我們另外加了他的 skill 沒有的
completeness layer（OE **漏掉**了什麼？）、CrossRef 存在性 gate，以及我們這種架構才需要的
marker↔citation index 對應。這些是加在他的地基上，不是取代它。

`skills/oe-verify/` 衍生自 `audit-oe-skill`，MIT © 2026 Hsieh-Ting Lin，詳見 [NOTICE](NOTICE)。
謝謝你，Hsieh-Ting。

## 與 textbook-to-note 搭配

`/oe-verify` 的設計目標之一，是作為
[textbook-to-note](https://github.com/drpwchen/textbook-to-note) 筆記工作流的可選
**evidence-enrichment 階段**：當一則筆記的 management/treatment 段落需要最新的建議時，由
`ask_openevidence` 取得，再由 `/oe-verify` 把關後才寫進筆記。當然，單獨使用 —— 純粹問 OE 問題並查核
答案 —— 也完全沒問題。
