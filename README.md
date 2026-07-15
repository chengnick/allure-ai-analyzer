# allure-ai-analyzer

> AI-powered failure triage for [Allure](https://allurereport.org/) test results —
> a **rule-based pre-filter** catches the obvious, an **LLM (Gemini)** classifies the rest,
> and anything low-confidence is flagged for human review.
>
> Point it at any `allure-results/` folder. No coupling to any specific test framework.

繁中:讀取 Allure 測試結果,自動把失敗案例分類成「環境問題 / 程式 bug / 測試腳本問題 / 不確定」。
明確的錯誤走規則(快、免費、穩定),模糊的交給 Gemini,低信心案例標記出來供人工複查。

---

## Quick start

```bash
pip install -r requirements.txt

# No API key needed — rule-based classification only
python main.py sample-results -o report.md --no-ai
```

With a Gemini API key (free tier works), the LLM classifies whatever the rules can't:

```bash
cp .env.example .env      # put your GEMINI_API_KEY inside
python main.py sample-results -o report.md
```

Then open `report.md`. Sample output:

| 分類 | 數量 | 佔比 |
|------|-----:|-----:|
| 🐛 程式bug | 2 | 33% |
| 🔧 測試腳本問題 | 2 | 33% |
| 🌐 環境問題 | 2 | 33% |

Each failure comes with a one-line reasoning, e.g.:

> `test_api[gamma-POST-getItemList]`
> 判斷依據:API 回傳 500 且訊息顯示 endpoint 不被支援,屬伺服器端行為錯誤。

---

## How it works

```
allure-results/*.json
        │
        ▼
  analyzer.py ── dedupe retries (historyId), keep failed/broken
        │
        ├── rule hit? ──► 環境問題 (high confidence, zero cost)
        │
        ▼ no rule hit
  classifier.py ── batched Gemini calls, structured JSON output
        │
        ▼
  report.py ──► report.md (grouped by category,
                low-confidence cases flagged for human review)
```

**Categories:** 環境問題 (environment) · 程式bug (product bug) · 測試腳本問題 (test-code issue) · 不確定 (uncertain)

---

## Design points

- **Rules first, LLM second** — unambiguous failures (`ConnectionError`, DNS, timeout, SSL…)
  are classified by regex for free. Only genuinely ambiguous cases spend tokens.
  Adding a rule is one line in `_ENV_PATTERNS`.
- **"Uncertain" is a first-class answer** — the prompt explicitly allows the LLM to say
  it doesn't know. Low-confidence results are collected into a dedicated
  "needs human review" section instead of being silently wrong.
- **Retry-aware dedupe** — results are deduplicated by Allure's `historyId`
  (unique per test × parameter combo), keeping only the latest attempt.
  A test that failed then passed on retry is *not* a failure.
  (`fullName` alone can't be used — it collapses parametrized variants.)
- **Defensive JSON parsing** — three fallback layers for the LLM response; a malformed
  batch degrades to "uncertain" instead of crashing the run.
- **REST, not SDK** — the Gemini call is a plain `requests.post` with retry/backoff.
  Two runtime dependencies total.

---

## Usage with a real project

```bash
# after any test run that produces Allure results, e.g. with pytest:
pytest --alluredir=allure-results
python /path/to/allure-ai-analyzer/main.py allure-results -o report.md
```

Works with any Allure-producing framework (pytest, TestNG, Playwright, …).

### CLI

```
python main.py <allure-results-dir> [-o report.md] [--no-ai]
```

| flag | 說明 |
|------|------|
| `-o` | 報告輸出路徑(預設 `report.md`) |
| `--no-ai` | 只跑規則層,不呼叫 Gemini(離線/CI 免金鑰模式,未命中規則者標「不確定」) |

---

## Notes

`sample-results/` contains synthetic Allure result files covering all four categories,
plus a retry pair and a passing test to demonstrate dedupe — so the demo is fully
self-contained and reproducible without any real test environment.

Built as part of an SDET portfolio; the classification prompt, rule set, and report
format are intentionally small enough to read in one sitting.
