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
  在那個分頁點下去完全一樣。參考實作對接名為 `kimi-webbridge` 的 daemon（預設 endpoint
  `http://127.0.0.1:10086/command`）；只要能講同一套簡單的 `{action, args, session}` JSON 協定，
  把 `OE_BRIDGE_URL` 指過去即可。OpenEvidence 沒有公開 API，且 session cookie 是 httpOnly，這正是為什麼
  要用分頁內 fetch 而不是一般 HTTP client。
- **Python 3.10+** 以及 `requirements.txt` 裡的相依套件（`mcp`）。

### 設定（環境變數）

| 變數 | 預設值 | 用途 |
|---|---|---|
| `OE_BRIDGE_URL` | `http://127.0.0.1:10086/command` | browser-bridge daemon 的 endpoint。 |
| `OE_BRIDGE_BIN` | `~/.kimi-webbridge/bin/kimi-webbridge.exe` | daemon 未啟動時要自動拉起的執行檔。 |
| `OE_DATA_DIR` | `~/.openevidence` | query log 與 rate log 的寫入位置。 |

### 安裝

```bash
pip install -r requirements.txt

# 註冊到 MCP client，例如 Claude Code：
claude mcp add openevidence -- python /path/to/openevidence-tools/mcp/server.py

# 把 skills/oe-verify/ 放到你的 client 會掃描 skill 的位置。
```

在平常用的瀏覽器登入 openevidence.com，並確認 browser-bridge daemon 有在跑；server 會自動開啟或
重用一個 OpenEvidence 分頁。

---

## 負責任的使用方式

- **你自己的帳號、你自己的 session。** 這個工具自動化的是*你*已登入的瀏覽器分頁。請只用在你本來就有權
  使用的帳號上，並遵守 OpenEvidence 的服務條款。
- **尊重額度。** OpenEvidence 的帳號額度約為**每小時 100 個問題**，且只有新問題與追問會消耗額度
  （重新取回舊答案免費）。Server 內建禮貌性的 rate limiter（滑動視窗 + 併發上限），預設值刻意保守 ——
  請不要為了灌爆服務而調高它。
- **臨床使用前務必查核。** 在你要依賴某個 OE 答案之前，先跑 `/oe-verify`（或自行核對原始文獻）。
  這就是本 repo 的全部意義。
- **這不是醫療建議。** 這些工具協助臨床工作者評估證據，不能取代臨床判斷，其輸出也不構成醫療建議。

---

## 與上游專案的關係

本專案**不是** [htlin222/openevidence-mcp](https://github.com/htlin222/openevidence-mcp) 的 fork。
它有自己的 tool layer，因為查核流程依賴上游沒有暴露的欄位 —— `publication_types`、`impact_score`、
`recency_score`、`is_guideline`，以及每個片段的 `origin` provenance。

在此感謝 **[htlin222](https://github.com/htlin222)** 的兩個想法，本專案在其基礎上發展：browser-relay
傳輸的概念，以及 `origin` ROT-1 解碼的技巧（出自 `audit-oe-skill`）—— 後者正是 provenance 分級查核
得以成立的關鍵。

## 與 textbook-to-note 搭配

`/oe-verify` 的設計目標之一，是作為
[textbook-to-note](https://github.com/drpwchen/textbook-to-note) 筆記工作流的可選
**evidence-enrichment 階段**：當一則筆記的 management/treatment 段落需要最新的建議時，由
`ask_openevidence` 取得，再由 `/oe-verify` 把關後才寫進筆記。當然，單獨使用 —— 純粹問 OE 問題並查核
答案 —— 也完全沒問題。
