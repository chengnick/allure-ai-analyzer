"""
classifier.py — Gemini 失敗分類器
用 Gemini REST API 直接呼叫(不用 SDK,沿用你 Turso 的教訓:REST 最穩)。

用法:
    from classifier import classify_failures
    results = classify_failures(failures)  # failures: list[dict]

每個 failure dict 需要:
    {
        "test_name": "test_login[]",
        "status": "failed" | "broken",
        "message": "...",   # statusDetails.message
        "trace": "...",     # statusDetails.trace(已截斷)
    }
"""

import json
import os
import re
import time

import requests

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_TRACE_LINES = 30
BATCH_SIZE = 10  # 一次送幾個失敗案例給 Gemini

VALID_CATEGORIES = {"環境問題", "程式bug", "測試腳本問題", "不確定"}

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是資深 QA 工程師,負責分析 API 自動化測試的失敗原因。

分類定義(只能用這四個,不可自創):
- 環境問題:網路、DNS、timeout、憑證、服務未啟動、測試環境資料異常等,與程式碼和測試腳本無關
- 程式bug:受測系統(API)行為錯誤,例如回傳錯誤的 status code、錯誤的資料內容、違反 API 規格
- 測試腳本問題:測試程式本身寫錯,例如 assertion 預期值錯誤、測資過期、selector/欄位名稱寫錯、腳本邏輯錯誤
- 不確定:資訊不足以判斷時,誠實選這個,不要硬猜

判斷原則:
1. `broken` 狀態(exception 中斷)偏向環境問題或腳本問題;`failed` 狀態(assertion 失敗)偏向程式bug 或腳本問題,但都不是絕對
2. 若 trace 顯示錯誤發生在測試程式碼的準備階段(setup/fixture),偏向腳本或環境
3. 若 API 有正常回應但內容不符預期,需判斷是 API 錯(程式bug)還是預期值錯(腳本問題);無法判斷時選「不確定」並在 reasoning 說明需要人工確認什麼

輸出規則:
- 只輸出 JSON array,不要 markdown code fence,不要任何前後說明文字
- 每個元素格式:
  {"id": <與輸入相同的 id>, "category": "<四選一>", "confidence": "high" | "low", "reasoning": "<一句話,繁體中文>"}
- 每個輸入案例都必須有對應輸出,不可遺漏
"""

CASE_TEMPLATE = """--- 案例 {id} ---
測試名稱: {test_name}
Allure 狀態: {status}
錯誤訊息:
{message}

Stack trace(截斷):
{trace}
"""


def _truncate_trace(trace: str, max_lines: int = MAX_TRACE_LINES) -> str:
    if not trace:
        return "(無)"
    lines = trace.splitlines()
    if len(lines) <= max_lines:
        return trace
    return "\n".join(lines[:max_lines]) + f"\n... (truncated, 共 {len(lines)} 行)"


def _build_user_prompt(batch: list[dict]) -> str:
    parts = ["以下是本批次的失敗案例,請逐一分類:\n"]
    for i, f in enumerate(batch):
        parts.append(
            CASE_TEMPLATE.format(
                id=i,
                test_name=f.get("test_name", "(unknown)"),
                status=f.get("status", "(unknown)"),
                message=(f.get("message") or "(無)").strip()[:2000],
                trace=_truncate_trace(f.get("trace") or ""),
            )
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Gemini REST call
# ---------------------------------------------------------------------------


def _call_gemini(user_prompt: str, api_key: str, max_retries: int = 3) -> str:
    """回傳 Gemini 的純文字回應。429/5xx 指數退避重試,其他失敗直接 raise。"""
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.1,          # 分類任務,壓低隨機性
            "response_mime_type": "application/json",  # 要求 JSON 輸出
        },
    }
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            GEMINI_URL,
            params={"key": api_key},
            json=payload,
            timeout=60,
        )
        if resp.status_code in (429, 500, 502, 503) and attempt < max_retries:
            wait = 2 ** attempt  # 2, 4, 8 秒
            print(
                f"[classifier] HTTP {resp.status_code},{wait}s 後重試 ({attempt}/{max_retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"Gemini 回應結構異常: {json.dumps(data)[:500]}") from e
    raise RuntimeError("Gemini API 重試次數用盡")


# ---------------------------------------------------------------------------
# JSON 解析(防禦性)
# ---------------------------------------------------------------------------


def _parse_response(text: str, batch_size: int) -> list[dict]:
    """
    解析 Gemini 回傳的 JSON。三層防禦:
    1. 直接 json.loads
    2. 剝掉可能殘留的 ```json fence 再 parse
    3. regex 撈出最外層的 [ ... ] 再 parse
    全失敗 → 整批標「不確定」,不讓程式掛掉。
    """
    candidates = [text]

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    candidates.append(stripped)

    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))

    parsed = None
    for c in candidates:
        try:
            parsed = json.loads(c)
            break
        except (json.JSONDecodeError, TypeError):
            continue

    if not isinstance(parsed, list):
        return [_fallback_item(i, "Gemini 回應無法解析為 JSON") for i in range(batch_size)]

    # 建 id -> item 索引,補齊遺漏的案例
    by_id = {}
    for item in parsed:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            by_id[item["id"]] = _sanitize_item(item)

    return [
        by_id.get(i, _fallback_item(i, "Gemini 回應中遺漏此案例"))
        for i in range(batch_size)
    ]


def _sanitize_item(item: dict) -> dict:
    category = item.get("category", "不確定")
    if category not in VALID_CATEGORIES:
        category = "不確定"
    confidence = item.get("confidence", "low")
    if confidence not in ("high", "low"):
        confidence = "low"
    return {
        "id": item["id"],
        "category": category,
        "confidence": confidence,
        "reasoning": str(item.get("reasoning", "")).strip() or "(無說明)",
    }


def _fallback_item(idx: int, reason: str) -> dict:
    return {"id": idx, "category": "不確定", "confidence": "low", "reasoning": reason}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_failures(failures: list[dict], api_key: str | None = None) -> list[dict]:
    """
    輸入 failure dicts,回傳同順序的分類結果:
        [{"category": ..., "confidence": ..., "reasoning": ...}, ...]
    內部自動分批(BATCH_SIZE),單批失敗不影響其他批次。
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("找不到 GEMINI_API_KEY,請確認 .env 已載入")

    results: list[dict] = []
    for start in range(0, len(failures), BATCH_SIZE):
        batch = failures[start: start + BATCH_SIZE]
        prompt = _build_user_prompt(batch)
        try:
            raw = _call_gemini(prompt, api_key)
            items = _parse_response(raw, len(batch))
        except (requests.RequestException, RuntimeError) as e:
            items = [
                _fallback_item(i, f"API 呼叫失敗: {e.__class__.__name__}")
                for i in range(len(batch))
            ]
        # 去掉內部用的 id,保持與輸入同順序
        for item in items:
            item.pop("id", None)
        results.extend(items)

    return results


if __name__ == "__main__":
    # 冒煙測試:python classifier.py(需要 GEMINI_API_KEY)
    from dotenv import load_dotenv

    load_dotenv()

    sample = [
        {
            "test_name": "test_login[tenantA]",
            "status": "broken",
            "message": "requests.exceptions.ConnectionError: HTTPSConnectionPool(host='api.tenant-a.example', port=443)",
            "trace": "Traceback (most recent call last):\n  File \"auth.py\", line 42, in login\n    resp = session.post(url, ...)\nrequests.exceptions.ConnectionError: ...",
        },
        {
            "test_name": "test_get_balance[tenantB]",
            "status": "failed",
            "message": "AssertionError: assert 200 == 500",
            "trace": "  File \"test_wallet.py\", line 18, in test_get_balance\n    assert resp.status_code == 200\nAssertionError",
        },
    ]
    for f, r in zip(sample, classify_failures(sample)):
        print(
            f"{f['test_name']}: {r['category']} ({r['confidence']}) — {r['reasoning']}")
