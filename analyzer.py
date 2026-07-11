"""
analyzer.py — 讀取 Allure 結果 + rule-based 預分類

職責:
1. 走訪 allure-results/*-result.json,撈出 failed / broken 的測試
2. 同一測試多次結果(retry)只保留最新一筆
3. 明顯的環境問題用規則直接分類,省下 Gemini token
"""

import json
import re
from pathlib import Path

TRACE_MAX_LINES = 30

# rule-based 預分類:trace 或 message 命中就直接標「環境問題」,不送 Gemini
# 只放「幾乎不可能誤判」的 pattern,寧缺勿濫
_ENV_PATTERNS = [
    (r"ConnectionError|ConnectionRefused|ConnectionReset", "連線錯誤"),
    (r"NewConnectionError|Max retries exceeded", "無法建立連線"),
    (r"NameResolutionError|getaddrinfo failed|Name or service not known", "DNS 解析失敗"),
    (r"ReadTimeout|ConnectTimeout|TimeoutError", "連線逾時"),
    (r"SSLError|CERTIFICATE_VERIFY_FAILED", "SSL/憑證錯誤"),
    (r"ProxyError", "Proxy 錯誤"),
    (r"LoginError.*缺帳密", "測試環境缺少登入憑證"),   # ← 加這行
]
_ENV_RULES = [(re.compile(p), label) for p, label in _ENV_PATTERNS]


def _truncate(text: str, max_lines: int = TRACE_MAX_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... (truncated, 共 {len(lines)} 行)"


def load_failures(results_dir: str | Path) -> list[dict]:
    """讀取 allure-results 資料夾,回傳失敗案例清單。

    每筆格式(對齊 classifier.py 的輸入):
        {"test_name", "status", "message", "trace"}
    同一測試(fullName)有多筆結果時,只保留 stop 時間最新的一筆(處理 retry)。
    """
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        raise FileNotFoundError(f"找不到資料夾: {results_dir}")

    latest: dict[str, dict] = {}  # 去重 key -> raw result(stop 最新)

    for path in results_dir.glob("*-result.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[analyzer] 跳過無法解析的檔案: {path.name}")
            continue

        # 去重 key 必須能區分參數化測試的不同參數組合:
        # historyId 是 Allure 對「測試+參數」算的唯一雜湊,首選;
        # 沒有 historyId 時退回 fullName+parameters(fullName 本身不含參數,不能單獨用)
        key = raw.get("historyId")
        if not key:
            params = json.dumps(raw.get("parameters", []),
                                sort_keys=True, ensure_ascii=False)
            key = f"{raw.get('fullName') or raw.get('name') or path.name}::{params}"
        if key not in latest or raw.get("stop", 0) > latest[key].get("stop", 0):
            latest[key] = raw

    failures = []
    for raw in latest.values():
        if raw.get("status") not in ("failed", "broken"):
            continue
        details = raw.get("statusDetails") or {}
        failures.append({
            "test_name": raw.get("name") or raw.get("fullName", "unknown"),
            "status": raw["status"],
            "message": (details.get("message") or "").strip(),
            "trace": _truncate((details.get("trace") or "").strip()),
        })

    # 穩定排序,報告與批次順序可重現
    failures.sort(key=lambda f: f["test_name"])
    return failures


def pre_classify(failure: dict) -> dict | None:
    """rule-based 預分類。命中回傳分類結果 dict,沒命中回傳 None(交給 Gemini)。"""
    haystack = f"{failure.get('message', '')}\n{failure.get('trace', '')}"
    for pattern, label in _ENV_RULES:
        if pattern.search(haystack):
            return {
                "category": "環境問題",
                "confidence": "high",
                "reasoning": f"[規則命中] {label}({pattern.pattern.split('|')[0]}...)",
            }
    return None


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "./allure-results"
    fs = load_failures(d)
    print(f"共 {len(fs)} 個失敗案例:")
    for f in fs:
        rule = pre_classify(f)
        tag = f"→ 規則分類: {rule['category']}" if rule else "→ 需 Gemini"
        print(f"  [{f['status']}] {f['test_name']} {tag}")
