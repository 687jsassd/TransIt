"""分析器：采样待译文本 → LLM 分析世界观/故事 → 提取专有名词建库。

产出结构 (output/glossary.json):
{
  "worldview": "...",            # 世界观/故事/风格概述（供翻译上下文）
  "game_context": {...},         # 游戏类型、题材、舞台
  "terms": {                     # 专有名词库: 原文 -> 译文
    "角色名": "...",
    "物品名": "...",
    "势力/组织": "...",
    "地名": "...",
    "专业术语": "...",
    "特殊用语/口癖": "...",
    "其他专有名词": "..."
  },
  "translation_notes": "..."     # 翻译注意事项
}
"""
import json
import os
import random
import re

from .llm import LLMClient, LLMError


class TaskStopped(Exception):
    """任务被用户主动停止（WebUI 停止按钮）。"""


def sample_texts(need_translate: dict, sample_size: int = 120, seed: int = 42) -> list:
    """分层采样：长文本全取，短文本随机抽样，保证覆盖各种句式。"""
    keys = list(need_translate.keys())
    short = [k for k in keys if len(k) <= 40]
    mid = [k for k in keys if 40 < len(k) <= 120]
    long = [k for k in keys if len(k) > 120]

    rng = random.Random(seed)
    chosen = list(long)  # 长文本（含完整剧情/规则说明）全保留
    n_short = min(len(short), max(0, sample_size // 3))
    n_mid = min(len(mid), max(0, sample_size - len(chosen) - n_short))
    chosen += rng.sample(short, n_short) if short else []
    chosen += rng.sample(mid, n_mid) if mid else []
    # 兜底补足
    remain = sample_size - len(chosen)
    if remain > 0:
        rest = [k for k in keys if k not in chosen]
        chosen += rng.sample(rest, min(remain, len(rest)))
    return chosen


def run_analysis(llm: LLMClient, samples: list, cfg: dict, stop_check=None) -> dict:
    """执行分析，返回 glossary dict。

    stop_check: 可选回调，返回 True 表示用户请求停止（抛 TaskStopped）。
    分阶段多次请求，避免单次输出过大被截断（max_tokens 上限）：
      1) 世界观/游戏上下文/翻译注意（一次请求，输出小）
      2) 术语提取：采样文本分块，每块一次请求（输出有界）
      3) 合并各块术语
    任一步失败自动重试（最多 max_retries 次）。
    """
    lang = cfg["language"]
    max_retries = cfg["pipeline"].get("max_retries", 4)

    def _check_stop():
        if stop_check is not None and stop_check():
            raise TaskStopped("用户停止了任务")

    # ---- 阶段 1：世界观（用小样本即可，输出小）----
    worldview_samples = samples[:60]
    worldview = None
    for attempt in range(max_retries):
        _check_stop()
        try:
            worldview = _request_worldview(llm, worldview_samples, lang)
            break
        except LLMError as e:
            if attempt == max_retries - 1:
                raise
            print(f"[analyze] 世界观分析失败，重试 {attempt + 1}: {e}")

    # ---- 阶段 2：术语分块提取 ----
    chunk_size = 60
    chunks = [samples[i:i + chunk_size] for i in range(0, len(samples), chunk_size)]
    merged_terms = {}
    for ci, chunk in enumerate(chunks):
        _check_stop()
        chunk_terms = None
        for attempt in range(max_retries):
            try:
                chunk_terms = _request_terms(llm, chunk, lang, ci + 1, len(chunks))
                break
            except LLMError as e:
                if attempt == max_retries - 1:
                    raise
                print(f"[analyze] 术语块 {ci + 1} 失败，重试 {attempt + 1}: {e}")
        # 合并（同词条后块覆盖前块，多数情况一致）
        for cat, mapping in (chunk_terms or {}).items():
            if not isinstance(mapping, dict):
                continue
            merged_terms.setdefault(cat, {})
            for src, dst in mapping.items():
                if isinstance(src, str) and isinstance(dst, str) and src.strip():
                    merged_terms[cat][src.strip()] = dst.strip()

    return {
        "worldview": (worldview or {}).get("worldview", ""),
        "game_context": (worldview or {}).get("game_context", {}),
        "terms": merged_terms,
        "translation_notes": (worldview or {}).get("translation_notes", ""),
    }


def _request_worldview(llm: LLMClient, samples: list, lang: dict) -> dict:
    """请求世界观/游戏上下文/翻译注意（不提取术语，输出小）。"""
    sample_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(samples))
    system = (
        "你是一位资深的游戏本地化专家，精通日语与中文。本游戏可能含成人（R18）内容，"
        "你应以专业、客观的视角分析，不回避成人向词汇。"
    )
    user = f"""以下是一款游戏的待翻译文本样本（{lang['source']}语，{len(samples)} 条）。

请分析并输出：
1. worldview：世界观概述（200字内）
2. game_context：{{"genre": 游戏类型, "setting": 设定, "story": 剧情梗概, "style_notes": 翻译风格要求}}
3. translation_notes：翻译全局注意事项（称呼一致性、H场景尺度、保留占位符等）

【输出要求】严格输出 JSON，不要任何其他文字：
{{"worldview": "...", "game_context": {{"genre": "...", "setting": "...", "story": "...", "style_notes": "..."}}, "translation_notes": "..."}}

【样本】
{sample_block}"""
    return llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         temperature=0.3, max_tokens=2000)


def _request_terms(llm: LLMClient, chunk: list, lang: dict, idx: int, total: int) -> dict:
    """请求提取一个分块的术语（输出有界）。"""
    sample_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
    system = (
        "你是一位资深的游戏本地化专家，精通日语与中文，擅长为游戏建立专有名词术语库。"
        "本游戏可能含成人（R18）内容，不回避成人向词汇。"
    )
    user = f"""以下是一批游戏文本样本（{lang['source']}语，第 {idx}/{total} 块，{len(chunk)} 条）。

提取其中所有专有名词与术语，按分类给出推荐中文译法，输出 JSON：
{{
  "角色名": {{"原词": "译法", ...}},
  "物品名": {{"原词": "译法", ...}},
  "势力/组织": {{"原词": "译法", ...}},
  "地名": {{"原词": "译法", ...}},
  "专业术语": {{"原词": "译法", ...}},
  "特殊用语/口癖": {{"原词": "译法", ...}},
  "其他专有名词": {{"原词": "译法", ...}}
}}

【约束】
- 只收有实义的名词/术语，不收语法词尾、助词（ます/です/に/を 等）。
- 译法必须纯净，禁止括号注释/注音/解释（如「小穴（俗语）」禁止，只写「小穴」）。
- 音译用常见字，不用生僻字；角色名按角色气质选译。
- 没有某类词条就省略该分类键。
- 严格输出 JSON，不要任何其他文字。

【样本】
{sample_block}"""
    return llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         temperature=0.2, max_tokens=3000)


def normalize_glossary(raw: dict) -> dict:
    """把 LLM 返回的 glossary 归一化：确保 terms 是 {分类: {原词: 译法}}。"""
    if not isinstance(raw, dict):
        raise LLMError("analysis output is not an object")
    out = {
        "worldview": str(raw.get("worldview", "")),
        "game_context": raw.get("game_context", {}) if isinstance(raw.get("game_context"), dict) else {},
        "terms": {},
        "translation_notes": str(raw.get("translation_notes", "")),
    }
    terms = raw.get("terms", {})
    if isinstance(terms, dict):
        for cat, mapping in terms.items():
            if isinstance(mapping, dict):
                norm = {}
                for k, v in mapping.items():
                    if isinstance(k, str) and k.strip():
                        src = k.strip()
                        dst = str(v).strip() if v else src
                        # 译法去括号注释（防止 LLM 附注 注音/解释/分类）
                        dst = _strip_paren_annotations(dst) or dst
                        if _is_valid_term(src, dst):
                            norm[src] = dst
                if norm:
                    out["terms"][cat] = norm
            elif isinstance(mapping, list):
                # 容忍 [{"原文": ..., "译法": ...}] 或 ["原词=译法"]
                norm = {}
                for item in mapping:
                    if isinstance(item, dict):
                        src = item.get("原文") or item.get("原词") or item.get("source")
                        dst = item.get("译法") or item.get("译文") or item.get("target")
                        if src and dst:
                            src, dst = str(src).strip(), str(dst).strip()
                            dst = _strip_paren_annotations(dst) or dst
                            if _is_valid_term(src, dst):
                                norm[src] = dst
                    elif isinstance(item, str) and "=" in item:
                        s, d = item.split("=", 1)
                        s, d = s.strip(), d.strip()
                        d = _strip_paren_annotations(d) or d
                        if _is_valid_term(s, d):
                            norm[s] = d
                if norm:
                    out["terms"][cat] = norm
    return out


# 日语语法成分/敬语词尾/助词——不应作为专有名词收录
_JAPANESE_GRAMMAR_TERMS = {
    "ます", "です", "でした", "でしょう", "ましょう", "ません", "ました", "ましたら",
    "だ", "じゃ", "だろう", "ではない", "ではないか", "のだ", "んだ",
    "に", "を", "は", "が", "の", "も", "と", "や", "へ", "で", "から", "まで",
    "より", "ば", "こそ", "でも", "しか", "だけ", "ほど", "まま", "など", "とか",
    "って", "ん", "つ", "う", "ぅ", "ね", "よ", "か", "な", "わ", "ぞ", "ぜ",
    "て", "た", "り", "ら", "れ", "る", "い", "く", "け", "さ", "し", "す", "せ",
    "お", "ご", "さん", "ちゃん", "くん", "さま", "様", "ですよ", "ますね", "ますよ",
    "でしたっけ", "ですから", "ですが", "ですの", "でしょ", "じゃない", "ない",
}

# 括号注释正则：译法里禁止的（…）（…）附注
_PAREN_ANNOT_RE = None


def _strip_paren_annotations(dst: str) -> str:
    """移除译法中的括号注释（LLM 常附注 注音/解释/分类）。"""
    global _PAREN_ANNOT_RE
    if _PAREN_ANNOT_RE is None:
        import re as _re
        _PAREN_ANNOT_RE = _re.compile(r"[（(][^（()）]*[）)]")
    return _PAREN_ANNOT_RE.sub("", dst).strip()


def _is_valid_term(src: str, dst: str) -> bool:
    """过滤无效词条：语法词尾、空、纯助词等。"""
    s = src.strip()
    if not s or len(s) > 60:
        return False
    if s in _JAPANESE_GRAMMAR_TERMS:
        return False
    # 原文=译文 且 无实义（纯假名短词、单个符号）
    if s == dst:
        import re
        # 纯假名（无汉字/无字母）且短于4字：如 ます、にゃ——这类没有翻译意义
        if re.fullmatch(r"[ぁ-んァ-ヶー～〜]+", s) and len(s) <= 4:
            return False
        # 纯符号
        if re.fullmatch(r"[♡♥☆★※◆◇■□○●〜～・…、。！？!?]+", s):
            return False
    return True


def save_glossary(glossary: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2)


def load_glossary(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def flatten_terms(glossary: dict) -> dict:
    """把分层术语库压平为 {原文: 译文} 单层映射（供翻译 prompt 用）。"""
    flat = {}
    for cat, mapping in glossary.get("terms", {}).items():
        if isinstance(mapping, dict):
            flat.update(mapping)
    return flat
