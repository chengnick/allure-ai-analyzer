"""
report.py — 產出 Markdown 分析報告

輸入:classified 清單(analyzer 欄位 + category/confidence/reasoning)
輸出:一份 Markdown 字串。按分類分組,低信心案例單獨列出供人工複查。
"""

from datetime import datetime

# 報告中的分類排序(嚴重度直覺:先看 bug,再看腳本,環境最後,不確定壓底)
_CATEGORY_ORDER = ["程式bug", "測試腳本問題", "環境問題", "不確定"]

_CATEGORY_ICON = {
    "程式bug": "🐛",
    "測試腳本問題": "🔧",
    "環境問題": "🌐",
    "不確定": "❓",
}


def generate_report(classified: list[dict], source_dir: str = "") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(classified)

    lines = [
        "# Allure 失敗分析報告",
        "",
        f"- 產生時間:{now}",
        f"- 結果來源:`{source_dir}`" if source_dir else "",
        f"- 失敗案例數:{total}",
        "",
    ]

    if total == 0:
        lines.append("✅ 沒有失敗案例,無需分析。")
        return "\n".join(filter(None, lines))

    # --- 統計表 ---
    counts = {c: 0 for c in _CATEGORY_ORDER}
    low_confidence = []
    for item in classified:
        counts[item.get("category", "不確定")] = counts.get(item.get("category", "不確定"), 0) + 1
        if item.get("confidence") == "low":
            low_confidence.append(item)

    lines += [
        "## 統計",
        "",
        "| 分類 | 數量 | 佔比 |",
        "|------|-----:|-----:|",
    ]
    for cat in _CATEGORY_ORDER:
        n = counts.get(cat, 0)
        if n:
            lines.append(f"| {_CATEGORY_ICON[cat]} {cat} | {n} | {n / total:.0%} |")
    lines.append("")

    # --- 分組明細 ---
    for cat in _CATEGORY_ORDER:
        items = [c for c in classified if c.get("category") == cat]
        if not items:
            continue
        lines += [f"## {_CATEGORY_ICON[cat]} {cat}({len(items)})", ""]
        for item in items:
            conf_mark = "" if item.get("confidence") == "high" else " ⚠️低信心"
            lines += [
                f"### `{item['test_name']}`{conf_mark}",
                "",
                f"- 狀態:`{item['status']}`",
                f"- 判斷依據:{item.get('reasoning', '(無)')}",
            ]
            if item.get("message"):
                # 錯誤訊息放進 code block,截前 5 行避免報告爆炸
                msg_lines = item["message"].splitlines()[:5]
                lines += ["- 錯誤訊息:", "", "```", *msg_lines, "```"]
            lines.append("")

    # --- 人工複查清單 ---
    if low_confidence:
        lines += [
            "## ⚠️ 建議人工複查",
            "",
            "以下案例分類信心不足,請人工確認:",
            "",
        ]
        for item in low_confidence:
            lines.append(
                f"- `{item['test_name']}` → {item.get('category')}:{item.get('reasoning', '')}"
            )
        lines.append("")

    return "\n".join(lines)
