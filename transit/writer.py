"""导出器：合并分类结果 + 翻译结果 → 输出与输入同格式的 Mtools JSON。

输出结构: {原文: 译文}
- passthrough（ID/数字/代码）: 原样保留（值=键）
- translated（已有译文）: 保留原译文
- need_translate: 用翻译结果填充
"""
import json
import os


def export_file(data: dict, groups: dict, translations: dict, out_path: str) -> dict:
    """合并生成最终翻译文件。

    data: 原始 {原文: 译文}
    groups: analyze_file() 的三分类结果
    translations: {原文: 译文}（need_translate 的处理结果，可能缺漏）
    out_path: 输出路径
    返回最终 dict。
    """
    result = {}
    stats = {"passthrough": 0, "translated": 0, "translated_new": 0, "missing": 0}
    for k, v in data.items():
        if k in groups["passthrough"]:
            result[k] = v
            stats["passthrough"] += 1
        elif k in groups["translated"]:
            result[k] = v
            stats["translated"] += 1
        else:  # need_translate
            if k in translations and translations[k]:
                result[k] = translations[k]
                stats["translated_new"] += 1
            else:
                # 缺漏：保留原文，便于人工发现
                result[k] = v
                stats["missing"] += 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print(f"[export] 已写出 {out_path}（共 {len(result)} 条）")
    print(f"[export] 统计: 原样保留 {stats['passthrough']} | 沿用旧译 {stats['translated']} "
          f"| 新译 {stats['translated_new']} | 缺漏 {stats['missing']}")
    return result
