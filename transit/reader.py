"""读取器：解析 Mtools 式翻译文件（JSON 键值对），分类条目。

分类规则：
- SKIP_PASSTHROUGH: 纯数字/ID/代码类（如 "10", "EV001", "MAP001"），原样保留
- SKIP_TRANSLATED: 值 != 键 且非占位符（已被翻译过），保留现有译文
- NEED_TRANSLATION: 需要翻译的文本（日文/其他语言）
- 特殊：键内含 <SG...> 等系统占位符 → 必须翻译但保留占位符原样
"""
import hashlib
import json
import os
import re

# 纯 ID/数字/代码：全部由 ASCII 数字、字母、常见符号组成且不含 CJK
ID_RE = re.compile(r"^[A-Za-z0-9_\-\s\.\(\)\[\]{}<>/\\|!?、,;:〜〜～「」『』\u3000]*$")
# 含假名 → 一定是日文
KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")
# 含 CJK 汉字（日文汉字）
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 系统占位符标签（翻译时须原样保留）：<SG...>、\n、\v[1] 等
PLACEHOLDER_RE = re.compile(r"<[^>]+>|\\[a-zA-Z]\[\d+\]|\\n")


def load_mt_file(path: str) -> dict:
    """读取 Mtools 翻译文件，返回 {原文: 译文}。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"not a key-value translation file: {path}")
    return data


def file_hash(path: str, length: int = 10) -> str:
    """计算翻译文件内容的稳定 hash（用于区分不同翻译对象）。

    mtool 导出的默认文件名都是 ManualTransFile.json，
    必须按内容 hash 隔离进度/术语库/输出，防止串翻译。
    """
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


def classify_entry(key: str, value: str):
    """返回 (类别, 理由)。

    类别: passthrough | translated | need_translate
    """
    if key == value:
        # 值==键：可能是未翻译，也可能是 ID/数字
        if not key.strip():
            return "passthrough", "empty"
        if ID_RE.fullmatch(key):
            return "passthrough", "id_or_code"
        # 代码赋值语句（如下划线开头的脚本变量），原样保留
        if key.lstrip().startswith("_") and re.search(r"\s*=\s*", key):
            return "passthrough", "code_assignment"
        if not (KANA_RE.search(key) or CJK_RE.search(key)):
            # 纯西文无 CJK 但又不是 ID（如 "Hello World"）→ 一般也无需翻译，
            # 但保留给模型判断：若目标语言是中文，纯英文也可能需要翻译。
            # 这里策略：全角字符/日文假名存在才翻，否则视为可原文保留。
            return "passthrough", "latin_no_cjk"
        return "need_translate", "untranslated_jp"
    else:
        # 值 != 键
        if value and not ID_RE.fullmatch(value):
            return "translated", "has_translation"
        return "need_translate", "translated_but_value_is_id"


def analyze_file(data: dict):
    """对整本文件做分类，返回统计与分组。"""
    groups = {"passthrough": {}, "translated": {}, "need_translate": {}}
    for k, v in data.items():
        cat, reason = classify_entry(k, v)
        groups[cat][k] = v
    return groups


def extract_placeholder_preserved(text: str):
    """提取文本中须原样保留的占位符 token 列表。"""
    return PLACEHOLDER_RE.findall(text)
