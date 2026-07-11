"""
main.py — CLI 入口

用法:
    python main.py <allure-results 路徑> [-o report.md] [--no-ai]

流程:
    load_failures → rule-based 預分類 → 剩下的批次送 Gemini → 合併 → Markdown 報告
"""

import argparse
import sys

from dotenv import load_dotenv

from analyzer import load_failures, pre_classify
from classifier import classify_failures
from report import generate_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Allure 失敗結果 AI 分類器")
    parser.add_argument("results_dir", help="allure-results 資料夾路徑")
    parser.add_argument("-o", "--output", default="report.md", help="輸出報告路徑(預設 report.md)")
    parser.add_argument("--no-ai", action="store_true", help="只跑 rule-based,不呼叫 Gemini(離線除錯用)")
    args = parser.parse_args()

    load_dotenv()

    # 1. 讀取失敗案例
    failures = load_failures(args.results_dir)
    print(f"[main] 讀到 {len(failures)} 個失敗案例")
    if not failures:
        report = generate_report([], source_dir=args.results_dir)
        _write(args.output, report)
        return 0

    # 2. rule-based 預分類
    classified: list[dict | None] = []
    need_ai: list[tuple[int, dict]] = []  # (原始索引, failure)
    for i, f in enumerate(failures):
        rule_result = pre_classify(f)
        if rule_result:
            classified.append({**f, **rule_result})
        else:
            classified.append(None)  # 佔位,等 Gemini 結果填回
            need_ai.append((i, f))

    n_rule = len(failures) - len(need_ai)
    print(f"[main] 規則分類 {n_rule} 個,{len(need_ai)} 個送 Gemini")

    # 3. Gemini 分類
    if need_ai:
        if args.no_ai:
            for i, f in need_ai:
                classified[i] = {
                    **f,
                    "category": "不確定",
                    "confidence": "low",
                    "reasoning": "[--no-ai 模式,未呼叫 Gemini]",
                }
        else:
            ai_inputs = [f for _, f in need_ai]
            ai_results = classify_failures(ai_inputs)
            for (i, f), r in zip(need_ai, ai_results):
                classified[i] = {**f, **r}

    # 4. 產報告
    report = generate_report(classified, source_dir=args.results_dir)
    _write(args.output, report)

    # 5. 終端摘要
    print(f"[main] 報告已寫入 {args.output}")
    summary: dict[str, int] = {}
    for c in classified:
        summary[c["category"]] = summary.get(c["category"], 0) + 1
    for cat, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"  {cat}: {n}")
    low = sum(1 for c in classified if c.get("confidence") == "low")
    if low:
        print(f"  ⚠️ 低信心 {low} 個,請開報告人工複查")
    return 0


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


if __name__ == "__main__":
    sys.exit(main())
