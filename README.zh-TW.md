# openevidence-tools

*[English version → README.md](README.md)*

兩個互相搭配的工具，讓 [OpenEvidence](https://www.openevidence.com) 成為**可信賴**的證據來源：

1. **一個 MCP server**，把 OpenEvidence 以四個 tool 的形式接給任何 MCP client（Claude Code 等）：
   `ask_openevidence`、`list_recent_oe_queries`、`fetch_oe_result`、`ask_oe_followup`。
2. **`/oe-verify` skill**（*選配，建議裝*），逐一把 OE 回答裡的**每一條 citation** 對回原始文獻查核，並找出 OE 漏掉的證據。
   沒有它 MCP server 照樣能用；安裝只是把一個資料夾複製進你的 skill 目錄，見
   [選配：`/oe-verify` skill](#選配oe-verify-skill)。

**為什麼兩個都需要？** OpenEvidence 回答臨床問題很快，但它本質上是 RAG pipeline（向量檢索 + LLM
合成），即使論述本身正確，citation 仍可能標錯。最典型的失效模式是 **transitive citation（轉引）**：
OE 檢索到某篇 **review 全文**的一個片段，而那段話其實是該 review 在引用*別人的* trial 數據，OE 卻把
這個數字算在 review 頭上。論述是真的，出處是錯的。**OE 的回答未經查核前，絕不可直接吸收** —— 這正是
`/oe-verify` 存在的理由。

## 快速開始

需要 OpenEvidence 帳號、Python 3.10+，以及 Edge、Chrome 或 Chromium（細節見[需求](#需求)）。
工具本身免費（MIT）、不需 API key；OpenEvidence 自己的條款與額度照舊。

```bash
pip install -r requirements.txt
python mcp/server.py --login          # 在它打開的瀏覽器視窗裡登入 OpenEvidence 一次
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py
```

接著對 Claude Code 問一個臨床問題，它會呼叫 `ask_openevidence`，回傳含 citation 的答案。
想先確認 transport 能動、又不碰 OpenEvidence，可以對本機的假伺服器跑 smoke test：
`python tests/smoke_cdp.py`。

> 🧋 省了你時間的話，[請我喝杯飲料](https://drpwchen.com/support/)。

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

*選配——MCP server 不需要它也能運作。安裝方式見 [選配：`/oe-verify` skill](#選配oe-verify-skill)。*

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

- **一個 OpenEvidence 帳號**（OpenEvidence 面向臨床工作者）。登入由你自己完成，只要一次，
  在 server 幫你打開的那個瀏覽器視窗裡登入。
- **裝好 Edge、Chrome 或 Chromium**——**Windows、macOS、Linux 皆可**。這個 server 不爬蟲、也不偽造
  任何東西：它把每一次 OpenEvidence API 呼叫，當成 page-context `fetch()` 跑在**一個已登入的
  openevidence.com 分頁裡**，所以每個 request 都走**你自己的帳號與 session**——跟你親手在那個分頁
  點下去完全一樣。OpenEvidence 沒有公開 API，且 session cookie 是 httpOnly，這正是為什麼要用分頁內
  fetch 而不是一般 HTTP client。

  預設（transport `cdp`）會用**獨立的 profile**（`~/.openevidence/browser-profile`）**另開一份你
  的瀏覽器**，透過 Playwright 走 Chrome DevTools Protocol（`127.0.0.1:9222`）驅動它。需要時自動啟動、
  用完不關；不下載任何東西，也不碰你平常用的瀏覽器 profile。不需要擴充功能，也不需要第三方 daemon。
- **Python 3.10+** 以及 `requirements.txt` 裡的相依套件（`mcp`，已鎖 `<2`，理由見下；以及
  `playwright`）。只需要 Python 套件本身：**不用跑 `playwright install`**，它接的是你已經裝好的瀏覽器。

> **`mcp` 必須鎖在 2.0 以下。** mcp 2.0 移除了 `mcp.server.fastmcp`，沒鎖版本的全新安裝會裝到 2.x，
> `server.py` 在 import 當下就死於 `ModuleNotFoundError` —— 而 MCP client 只會顯示
> `Failed to connect — Connection closed`，害你往完全錯的方向找。`requirements.txt` 已經鎖好；
> 手動安裝的話請自己也鎖。

> **🔒 那個專用瀏覽器只登入 OpenEvidence。** 它開著的時候，你這台電腦上的任何程式都能透過除錯埠
> 操作它（埠只聽 `127.0.0.1`，不對網路開放）。這正是它要用獨立 profile 的原因：專門給 OpenEvidence
> 用，網銀、信箱或其他任何帳號都不要在那裡登入。

<details>
<summary>舊 transport：Kimi WebBridge（<code>OE_TRANSPORT=http</code>）</summary>

0.3.x 以前透過 **[Kimi WebBridge](https://www.kimi.com/features/webbridge)**（瀏覽器擴充功能＋
`http://127.0.0.1:10086/command` 的本機 daemon）驅動你*平常用的*瀏覽器。這條路仍可用：設
`OE_TRANSPORT=http`，或把 `OE_BRIDGE_URL` 指向任何講同一套 `{action, args, session}` JSON 協定的
daemon（光是設了 `OE_BRIDGE_URL` 就會選這條，所以既有設定照常運作）。注意有同名的第三方專案，其中
部分會開一個未認證的 10086 port，這裡指的是官方那個。daemon 只在 Windows（或 `OE_BRIDGE_BIN` 指向
一支吃 `start` 參數的執行檔時）會自動拉起；macOS／Linux 請自己先開 bridge。debugger 型 bridge
一次就握有你所有網站的登入身分——如果你用它，建議用
**[kimi-webbridge-lockdown](https://github.com/drpwchen/kimi-webbridge-lockdown)** 把網銀、券商、
信箱擋在它碰不到的地方。

</details>

### 設定（環境變數）

| 變數 | 預設值 | 用途 |
|---|---|---|
| `OE_TRANSPORT` | `cdp`（有設 `OE_BRIDGE_URL` 時為 `http`） | `cdp`＝走 DevTools 的專用瀏覽器；`http`＝舊的 Kimi WebBridge daemon。 |
| `OE_BROWSER_EXE` | 依平台自動偵測（Edge/Chrome/Chromium） | 瀏覽器沒開時要啟動的瀏覽器。 |
| `OE_CDP_PORT` | `9222` | 專用瀏覽器的 DevTools 埠。9222 已被別的東西占用就改掉。這個埠上已經有瀏覽器在回應的話，server 直接用它、不另開。 |
| `OE_CDP_PROFILE` | `<OE_DATA_DIR>/browser-profile` | 專用瀏覽器的 profile 目錄——你的 OpenEvidence 登入狀態存在這裡。 |
| `OE_BROWSER_ARGS` | *（空）* | 啟動專用瀏覽器時額外加的參數，例如在 container 裡加 `--no-sandbox`。瀏覽器起不來時，它自己的輸出在 `<OE_DATA_DIR>/browser-launch.log`，錯誤訊息也會引用。 |
| `OE_BRIDGE_URL` | `http://127.0.0.1:10086/command` | 僅 `http` transport：browser-bridge daemon 的 endpoint。 |
| `OE_BRIDGE_BIN` | Windows 為 `~/.kimi-webbridge/bin/kimi-webbridge.exe`；**其他平台不設** | 僅 `http` transport：要自動拉起的 daemon 執行檔。不設＝不自動拉起。 |
| `OE_DATA_DIR` | `~/.openevidence` | query log、rate log 與送出間隔狀態的寫入位置。 |
| `OE_SUBMIT_SPACING_SEC` | `700`（約 11.7 分鐘） | 兩次送出問題之間的最小實際間隔。要調低，請先用你自己帳號的實測數據 —— 見下方說明。 |

### 安裝

```bash
pip install -r requirements.txt

# 在它打開的專用瀏覽器裡登入 OpenEvidence 一次：
python mcp/server.py --login

# 註冊到 MCP client，例如 Claude Code：
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py

# 選配（建議）：安裝 /oe-verify skill（見下一節）。
```

登入狀態存在專用 profile 裡；只有 OpenEvidence 把你登出時才需要再跑一次 `--login`。之後 server 會
自我修復：瀏覽器沒開會自動啟動、瀏覽器重開過會自動重連，並自動開啟或重用一個 OpenEvidence 分頁。
出現 `Not authenticated` 就是 session 過期了——再跑一次 `--login`。

不碰 OpenEvidence、先確認你這台機器上 transport 能動（會開一個用完即丟的瀏覽器 profile，對本機的
替身 server 跑完整流程，再關掉）：

```bash
python tests/smoke_cdp.py              # 沒有螢幕的 Linux：xvfb-run -a python tests/smoke_cdp.py
python -m unittest discover -s tests   # 單元測試，不需要瀏覽器
```

這兩項都在 CI 的 Windows、macOS、Linux 上跑。

### 選配：`/oe-verify` skill

透過 MCP server *使用* OpenEvidence 不需要這個 skill，但要放心依賴一則答案就需要它（理由見
[上方](#oe-verify-skill)）。它就是一個資料夾 `skills/oe-verify/`；安裝＝把這個資料夾複製到你的
client 載入 skill 的位置。在你裝好之前，`--login` 每次都會提醒一次。

Claude Code，裝成個人 skill（所有專案都能用）：

```bash
# macOS / Linux
mkdir -p ~/.claude/skills && cp -r skills/oe-verify ~/.claude/skills/
```

```powershell
# Windows（PowerShell）
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
Copy-Item -Recurse skills\oe-verify "$HOME\.claude\skills\"
```

只想在單一專案用，就改複製到 `<你的專案>/.claude/skills/`。裝完開一個新的 Claude Code session，
之後在 `ask_openevidence` 回答後跑 `/oe-verify`。複製過去的版本不會自己更新：`git pull` 之後要再複製一次
（或改用 symlink 而不是複製）。其他 MCP client：把資料夾放到該 client 掃描 skill 的位置。

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
直接讓請求跑在臨床醫師自己那個已登入的瀏覽器分頁裡。實作是我們自己寫的（現在改用 DevTools Protocol 驅動專用瀏覽器，
不靠擴充功能），但方向是他指的。

我們自己寫了一層 tool layer 而不是 fork，理由很實際：verifier 需要 `publication_types`、
`impact_score`、`recency_score`、`is_guideline` 與 per-chunk `origin` 直接從 MCP contract 回傳，
而且我們是透過通用管道驅動瀏覽器，不是專屬 extension。我們另外加了他的 skill 沒有的
completeness layer（OE **漏掉**了什麼？）、CrossRef 存在性 gate，以及我們這種架構才需要的
marker↔citation index 對應。這些是加在他的地基上，不是取代它。

`skills/oe-verify/` 衍生自 `audit-oe-skill`，MIT © 2026 Hsieh-Ting Lin，詳見 [NOTICE](NOTICE)。
謝謝你，Hsieh-Ting。

## 限制

- OpenEvidence 沒有公開 API。這個 server 用的是網站自己的私有端點，網站一改版就可能壞（見上）。
- 送出問題有配速（預設約 11.7 分鐘一題），這是為了守住 OpenEvidence 的最小間隔，本工具無法加快。
- 需要在專用瀏覽器裡保有已登入的 OpenEvidence session；過期就要重跑 `--login`。
- `/oe-verify` 能降低 citation 錯誤，但不能取代自己讀原始文獻，輸出也不是醫療建議。

## 與 textbook-to-note 搭配

`/oe-verify` 的設計目標之一，是作為
[textbook-to-note](https://github.com/drpwchen/textbook-to-note) 筆記工作流的可選
**evidence-enrichment 階段**：當一則筆記的 management/treatment 段落需要最新的建議時，由
`ask_openevidence` 取得，再由 `/oe-verify` 把關後才寫進筆記。當然，單獨使用 —— 純粹問 OE 問題並查核
答案 —— 也完全沒問題。

---

## Support 支持

覺得這個工具有幫助嗎？歡迎[請我喝飲料](https://drpwchen.com/support/) 🧋
If this tool helped you, you can [buy me a drink](https://drpwchen.com/en/support/).
