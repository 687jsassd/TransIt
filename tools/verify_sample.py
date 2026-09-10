"""一次性校验：示例样例的分支覆盖与重复键检查（CLI: python tools/verify_sample.py）"""
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "示例翻译文件", "ManualTransFile.json")
    sys.path.insert(0, ROOT)

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    keys = re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', raw, re.M)
    dup = [k for k, c in collections.Counter(keys).items() if c > 1]
    print(f"原始键数: {len(keys)} | 重复键: {dup if dup else '无'}")
    if dup:
        return 1

    data = json.loads(raw)
    print(f"条目数: {len(data)}")

    from transit.reader import analyze_file, classify_entry, PLACEHOLDER_RE

    groups = analyze_file(data)
    print("\n分类结果:")
    for name in ("passthrough", "translated", "need_translate"):
        print(f"  {name:16s} {len(groups[name])}")

    reasons = collections.Counter(classify_entry(k, v)[1] for k, v in data.items())
    print("\n触发的分类分支:")
    for r, c in sorted(reasons.items()):
        print(f"  {r:26s} {c}")

    expect = {"empty", "id_or_code", "code_assignment", "latin_no_cjk",
              "untranslated_jp", "has_translation", "translated_but_value_is_id"}
    missing = expect - set(reasons)
    print("\n未覆盖分支:", missing if missing else "无 —— classify_entry 全部分支已覆盖")

    text = "\n".join(data.keys())
    n_sg = len(re.findall(r"<[^>]+>", text))
    n_var = len(re.findall(r"\\[a-zA-Z]\[\d+\]", text))
    n_nl = len(re.findall(r"\\n", text))
    print(f"\n占位符统计: <SG..> {n_sg} | \\v[n] {n_var} | \\n {n_nl}")
    print(f"文件大小: {len(raw.encode('utf-8'))} 字节")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
