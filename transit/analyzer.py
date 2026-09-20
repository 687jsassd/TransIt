"""分析器：采样待译文本 → LLM 分析世界观/角色/风格 → 提取专有名词建库。

分析分三阶段，各自一次或多次请求（避免单次输出过大被截断）：
  1) 世界观 / 游戏上下文 / 命名方针 / 语气指南 —— 本地化的「风格圣经」
  2) 角色表（性别、身份、语气、自称、被称呼方式）—— 对白翻译的关键依据
  3) 术语分块提取（把角色表喂进去，保证人名在各块之间不冲突）

采样：按待译条数的比例取样（受上下限约束），并且用**窗口采样**保留原文顺序，
使样本覆盖剧本的开头 / 中段 / 结尾，而不是只在开头附近随机抓取。

产出结构 (output/xxx.glossary.json):
{
  "worldview": "...",
  "game_context": {"genre","setting","story","player_perspective","tone","style_notes"},
  "naming_policy": "...",                 # 专有名词总方针
  "register_guide": {"narration","dialogue","ui","adult"},   # 各类文本语气要求
  "translation_notes": "...",
  "characters": [{"name","reading","gender","role","personality",
                  "speech_style","first_person","called_by_others","notes"}],
  "terms": {"角色名": {...}, "物品名": {...}, ...},
  "term_conflicts": [{"category","source","kept","dropped","from"}]  # 各块译法分歧
}
"""
import json
import os
import random
import re

from .llm import LLMClient, LLMError

#: 世界观分析使用的样本条数上限（超出部分按「开头/中段/结尾」摘录）
WORLDVIEW_BUDGET = 90
#: 窗口采样：每个窗口约多少条（决定窗口数量）
WINDOW_TARGET = 40
#: 窗口数上下限（太少覆盖不全，太多则每个窗口太短失去上下文）
MIN_WINDOWS = 4
MAX_WINDOWS = 20


class TaskStopped(Exception):
    """任务被用户主动停止（WebUI 停止按钮）。"""


def resolve_sample_count(total: int, cfg: dict) -> int:
    """按配置算出采样条数：待译条数 × sample_ratio，再夹到 [sample_min, sample_max]。

    全量小于下限时取全量（不可能采出比文件更多的条目）。
    """
    total = int(total or 0)
    if total <= 0:
        return 0
    p = cfg.get("pipeline", {}) or {}
    try:
        ratio = float(p.get("sample_ratio", 0.10) or 0)
    except (TypeError, ValueError):
        ratio = 0.10
    try:
        lo = int(p.get("sample_min", 200) or 0)
        hi = int(p.get("sample_max", 1000) or 0)
    except (TypeError, ValueError):
        lo, hi = 200, 1000
    n = int(round(total * ratio)) if ratio > 0 else total
    if lo > 0:
        n = max(n, lo)
    if hi > 0:
        n = min(n, hi)
    return max(1, min(n, total))


def sample_texts(need_translate: dict, cfg: dict, seed: int = 42) -> list:
    """窗口采样，返回**按原文顺序**排列的样本文本列表。

    做法：把待译条目按原文顺序切成 K 段，每段取一段连续文本。
    这样既覆盖剧本的开头/中段/结尾，又保留段内的上下文连续性 ——
    世界观分析能读到连贯的对白，术语提取也不会只看到零散单句。
    另外优先补入超长条目（完整场景/规则说明对世界观与术语价值最高）。

    mtool 导出文件的键顺序即剧本顺序，这是该策略成立的前提。
    """
    keys = list(need_translate.keys())
    total = len(keys)
    if total == 0:
        return []
    n = resolve_sample_count(total, cfg)
    if n >= total:
        return keys

    rng = random.Random(seed)
    idx = set()

    # 1) 窗口采样：K 段各取一段连续文本
    windows = max(MIN_WINDOWS, min(MAX_WINDOWS, int(round(n / WINDOW_TARGET))))
    windows = min(windows, total)
    per = max(1, n // windows)
    for i in range(windows):
        lo = i * total // windows
        hi = (i + 1) * total // windows
        seg = hi - lo
        take = min(per, seg)
        if take <= 0:
            continue
        max_off = seg - take
        off = rng.randint(0, max_off) if max_off > 0 else 0
        idx.update(range(lo + off, lo + off + take))

    # 2) 长文本优先补足（完整场景 / 规则说明）
    if len(idx) < n:
        longs = sorted((i for i in range(total)
                        if i not in idx and len(keys[i]) > 120),
                       key=lambda i: -len(keys[i]))
        for i in longs:
            if len(idx) >= n:
                break
            idx.add(i)

    # 3) 仍不足则随机补足
    if len(idx) < n:
        rest = [i for i in range(total) if i not in idx]
        idx.update(rng.sample(rest, min(n - len(idx), len(rest))))

    return [keys[i] for i in sorted(idx)]


def _spread_blocks(samples: list, budget: int = WORLDVIEW_BUDGET) -> list:
    """把有序样本切成「开头 / 中段 / 结尾」三段连续文本。

    原先直接用 samples[:60]，只能看到文件开头 —— 剧本后段才出场的角色与设定
    完全进不了世界观分析。改成首/中/尾摘录后，故事结构才看得完整。
    """
    if not samples:
        return []
    if len(samples) <= budget:
        return [("全文", list(samples))]
    labels = ("开头", "中段", "结尾")
    per = max(1, budget // len(labels))
    blocks = []
    for i, label in enumerate(labels):
        lo = i * len(samples) // len(labels)
        hi = (i + 1) * len(samples) // len(labels) if i < len(labels) - 1 else len(samples)
        if hi <= lo:
            continue
        take = min(per, hi - lo)
        mid = (lo + hi) // 2
        start = max(lo, min(mid - take // 2, hi - take))
        blocks.append((label, samples[start:start + take]))
    return blocks


def _render_blocks(blocks: list) -> str:
    """把 [(标签, 文本列表)] 渲染成带段落标记的样本块。"""
    lines = []
    n = 0
    for label, texts in blocks:
        lines.append(f"——【{label}】——")
        for t in texts:
            n += 1
            lines.append(f"{n}. {t}")
    return "\n".join(lines)


def run_analysis(llm: LLMClient, samples: list, cfg: dict, stop_check=None,
                 step_cb=None) -> dict:
    """执行分析，返回 glossary dict。

    stop_check: 可选回调，返回 True 表示用户请求停止（抛 TaskStopped）。
    step_cb:    可选回调，每完成一个请求（阶段）调用一次，供 UI 推进进度条。
    三个阶段各自失败自动重试（最多 max_retries 次）。

    这是分析流程的**唯一实现**：CLI / WebUI / tkinter 界面都走这里，
    避免各处各写一份、改了一处另两处不生效。
    """
    lang = cfg["language"]
    max_retries = cfg["pipeline"].get("max_retries", 4)
    blocks = _spread_blocks(samples)

    def _check_stop():
        if stop_check is not None and stop_check():
            raise TaskStopped("用户停止了任务")

    def _tick():
        if step_cb is not None:
            step_cb()

    def _retry(fn, what):
        last = None
        for attempt in range(max_retries):
            _check_stop()
            try:
                out = fn()
                _tick()
                return out
            except LLMError as e:
                last = e
                if attempt == max_retries - 1:
                    raise
                print(f"[analyze] {what} 失败，重试 {attempt + 1}/{max_retries}: {e}")
        raise last

    # ---- 阶段 1：世界观 / 命名方针 / 语气指南 ----
    print(f"[analyze] 阶段1/3：世界观与风格分析（样本 {len(samples)} 条，"
          f"按 {len(blocks)} 段摘录）")
    worldview = _retry(lambda: _request_worldview(llm, blocks, lang), "世界观分析") or {}

    # ---- 阶段 2：角色表 ----
    print("[analyze] 阶段2/3：角色表（性别/语气/自称/称呼关系）")
    characters = _retry(
        lambda: _request_characters(llm, blocks, lang, worldview), "角色表提取") or []

    # ---- 阶段 3：术语分块提取（带角色表，避免人名在块间冲突）----
    chunk_size = 60
    chunks = [samples[i:i + chunk_size] for i in range(0, len(samples), chunk_size)]
    print(f"[analyze] 阶段3/3：术语提取（{len(chunks)} 块）")
    merged_terms = {}
    conflicts = []
    # 角色表里的名字先入表，作为权威译名，后续各块不得覆盖
    if characters:
        char_terms = {c["name"]: c["reading"] for c in characters
                      if c.get("name") and c.get("reading")}
        if char_terms:
            merged_terms.setdefault("角色名", {}).update(char_terms)
    for ci, chunk in enumerate(chunks):
        chunk_terms = _retry(
            lambda c=chunk, i=ci: _request_terms(llm, c, lang, i + 1, len(chunks), characters),
            f"术语块 {ci + 1}")
        _merge_terms(merged_terms, chunk_terms or {}, f"块{ci + 1}", conflicts)

    if conflicts:
        # 出现次数多的排在前面：越是反复冲突的译法越值得人工裁决
        conflicts.sort(key=lambda c: -c.get("count", 1))
        print(f"[analyze] 注意：{len(conflicts)} 处译法分歧（已保留先出现的译法，"
              f"可在术语库页面核对）")
        for c in conflicts[:5]:
            print(f"    {c['source']}: 保留「{c['kept']}」，忽略"
                  f"「{c['dropped']}」×{c.get('count', 1)}（{c['from']}）")

    return {
        "worldview": (worldview or {}).get("worldview", ""),
        "game_context": (worldview or {}).get("game_context", {}),
        "naming_policy": (worldview or {}).get("naming_policy", ""),
        "register_guide": (worldview or {}).get("register_guide", {}),
        "translation_notes": (worldview or {}).get("translation_notes", ""),
        "characters": characters if isinstance(characters, list) else [],
        "terms": merged_terms,
        "term_conflicts": conflicts,
    }


def analysis_requests(sample_count: int) -> int:
    """预估分析阶段的请求次数（供 UI 算进度条总分母）。"""
    chunks = (sample_count + 59) // 60 if sample_count > 0 else 0
    return 2 + chunks  # 世界观 + 角色表 + 术语块


def _merge_terms(merged: dict, incoming: dict, source_label: str, conflicts: list) -> None:
    """合并一块术语。同一原文出现不同译法时**保留先出现的**并记录分歧。

    原实现是后块直接覆盖前块，等于随机丢掉先前的译法；现在改为保留 + 上报，
    让用户能在术语库页面看到分歧并自行裁决。
    """
    for cat, mapping in (incoming or {}).items():
        if not isinstance(mapping, dict):
            continue
        bucket = merged.setdefault(cat, {})
        for src, dst in mapping.items():
            if not (isinstance(src, str) and isinstance(dst, str) and src.strip()):
                continue
            s, d = src.strip(), dst.strip()
            if not d:
                continue
            if s in bucket:
                if bucket[s] != d:
                    _record_conflict(conflicts, cat, s, bucket[s], d, source_label)
            else:
                bucket[s] = d


def _record_conflict(conflicts: list, category: str, source: str,
                     kept: str, dropped: str, source_label: str) -> None:
    """记录译法分歧。同一分歧在多块里重复出现时只累加次数，不重复列出。"""
    for c in conflicts:
        if (c["category"] == category and c["source"] == source
                and c["kept"] == kept and c["dropped"] == dropped):
            c["count"] = c.get("count", 1) + 1
            return
    conflicts.append({"category": category, "source": source, "kept": kept,
                      "dropped": dropped, "from": source_label, "count": 1})


def _request_worldview(llm: LLMClient, blocks: list, lang: dict) -> dict:
    """阶段 1：世界观 / 游戏上下文 / 命名方针 / 语气指南。

    输出的是本地化前的「风格圣经」——每一项都要求落到实处，避免"温馨感人的故事"
    这类无法指导翻译的空话。
    """
    system = (
        "你是一位资深的游戏本地化专家，精通日语与中文，负责在翻译动工前编写"
        "「风格圣经」（style bible）：把作品的设定、角色、语气、命名方针写清楚，"
        "供后续译者统一遵循。本游戏可能含成人（R18）内容，你应以专业、客观的视角"
        "分析，不回避成人向词汇。"
    )
    user = f"""以下是一款游戏的待翻译文本样本（{lang['source']}语，按剧情顺序分
「开头 / 中段 / 结尾」三段摘录）。请编写这份风格圣经。

【分析要求】每一项都要**具体、可执行**，能直接指导翻译；禁止"语言要自然流畅"
这类废话。如果某项在样本里看不出来，就写"样本不足以判断"。

1. worldview：世界观与故事概述（300 字内）。说清：什么世界/时代、核心冲突、
   主角的处境与目标。
2. game_context：
   - genre：游戏类型（日式麻将+RPG / 视觉小说 / 卡牌 / 养成 等）
   - setting：舞台设定
   - story：剧情梗概（150 字内）
   - player_perspective：玩家视角（第一人称代入主角 / 第三人称旁观 / 无固定主角）
   - tone：整体基调（轻松搞笑 / 黑暗严肃 / 甜腻暧昧 / 悬疑紧张 等）
   - style_notes：中文文风要求，落到具体写法（如"对白口语化，避免书面语；
     拟声词保留原节奏"）
3. naming_policy：专有名词总方针。明确：人名音译还是意译、音译用字偏好、
   哪些类别必须意译（招式名/组织名/称号）、是否保留日式敬称（さん/ちゃん）、
   西式人名还是日式读法。
4. register_guide：四类文本各自的语气与用词要求：
   - narration：旁白/叙述
   - dialogue：角色对白
   - ui：菜单/按钮/系统提示（说明要不要短、要不要统一为动词短语）
   - adult：成人场景的处理方针（露骨程度、拟声词、娇喘的处理）
5. translation_notes：给译者的具体注意事项——写"别人最容易译错的地方"，
   比如容易混淆的同形词、需要区分的近义称呼、机翻常犯的错误。

【输出要求】严格输出 JSON，不要任何其他文字、不要 Markdown 围栏：
{{"worldview": "...", "game_context": {{"genre": "", "setting": "", "story": "", "player_perspective": "", "tone": "", "style_notes": ""}}, "naming_policy": "...", "register_guide": {{"narration": "", "dialogue": "", "ui": "", "adult": ""}}, "translation_notes": "..."}}

【样本】
{_render_blocks(blocks)}"""
    return llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         temperature=0.3, max_tokens=3000)


def _request_characters(llm: LLMClient, blocks: list, lang: dict, worldview: dict) -> list:
    """阶段 2：角色表。

    这是对白翻译最关键的依据，也是原来的分析完全缺失的部分：日语不标性别而中文
    必须选"他/她"，同一角色的自称与别人对他的称呼也直接影响译文语气。
    """
    ctx = (worldview or {}).get("worldview", "")
    tone = ((worldview or {}).get("game_context") or {}).get("tone", "")
    naming = (worldview or {}).get("naming_policy", "")
    system = (
        "你是一位资深的游戏本地化专家，精通日语与中文。你正在为译者整理出场角色表，"
        "目的是让全篇译文里每个角色的性别、自称、语气和称呼方式保持统一。"
        "本游戏可能含成人（R18）内容，你应以专业视角处理，不回避。"
    )
    user = f"""已知本作世界观：{ctx[:400] or '（未提供）'}
已知整体基调：{tone or '（未提供）'}
已知命名方针：{naming[:300] or '（未提供）'}

下面是文本样本（按剧情顺序分「开头 / 中段 / 结尾」摘录）。请整理**出场角色表**。

【每个角色输出一条，字段含义】
- name：原文中的名字（照抄原文写法，不要翻译）
- reading：推荐中文译名（遵循上面的命名方针）
- gender：性别，只能是「男」「女」「未知」——决定中文用"他/她/它"
- role：身份定位（主角 / 女主角 / 伙伴 / 反派 / 旁白 / 店主 / 路人 等）
- personality：性格（简短，如 强势/怯懦/腹黑）
- speech_style：说话风格（如 傲娇、文雅、粗鲁、孩子气、敬语很多、带口癖）
- first_person：第一人称自称（如 私→"我"、僕→"我（男性化）"、わたくし→"本宫"、俺様→"本大爷"）
- called_by_others：别人怎么称呼 TA（多种叫法用 / 分隔，如"莉娜/莉娜小姐/姐姐"）
- notes：翻译该角色台词时的注意事项（可不填）

【硬性要求】
- 只列**有台词或有明确身份**的角色；有名字的路人（如「街の少女」「老書店主」）也要列。
- 性别必须给出；样本看不出时填「未知」，不要瞎猜。
- 不要编造样本里不存在的角色，也不要遗漏反复出场的角色。
- 译名必须遵循上面的命名方针；同一角色只能有一个译名。

【输出要求】严格输出 JSON，不要任何其他文字、不要 Markdown 围栏：
{{"characters": [{{"name": "", "reading": "", "gender": "", "role": "", "personality": "", "speech_style": "", "first_person": "", "called_by_others": "", "notes": ""}}]}}

【样本】
{_render_blocks(blocks)}"""
    result = llm.chat_json([{"role": "system", "content": system},
                            {"role": "user", "content": user}],
                           temperature=0.3, max_tokens=3000)
    chars = result.get("characters") if isinstance(result, dict) else None
    if isinstance(chars, dict):  # 容忍 {"角色名": {...}} 形式
        chars = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "reading": str(v)}
                 for k, v in chars.items()]
    return _normalize_characters(chars)


_CHAR_FIELDS = ("name", "reading", "gender", "role", "personality",
                "speech_style", "first_person", "called_by_others", "notes")


def _normalize_characters(chars) -> list:
    """归一化角色表：统一字段、丢弃无名字的条目、按名字去重。"""
    if not isinstance(chars, list):
        return []
    out, seen = [], set()
    for item in chars:
        if not isinstance(item, dict):
            continue
        rec = {}
        for f in _CHAR_FIELDS:
            v = item.get(f)
            rec[f] = str(v).strip() if v is not None else ""
        if not rec["name"]:
            continue
        if not rec["reading"]:
            rec["reading"] = rec["name"]  # 没给译名时退化为原文，译者可自行补
        g = rec["gender"]
        rec["gender"] = g if g in ("男", "女", "未知") else ("未知" if not g else g)
        if rec["name"] in seen:
            continue
        seen.add(rec["name"])
        out.append(rec)
    return out


def _request_terms(llm: LLMClient, chunk: list, lang: dict, idx: int, total: int,
                   characters: list = None) -> dict:
    """阶段 3：提取一个分块的术语（输出有界）。

    characters: 已确定的角色译名；传给模型可避免同一角色在不同块里被译成不同写法。
    """
    system = (
        "你是一位资深的游戏本地化专家，精通日语与中文，擅长为游戏建立专有名词术语库。"
        "本游戏可能含成人（R18）内容，不回避成人向词汇。"
    )
    known = ""
    if characters:
        lines = []
        for c in characters:
            if not c.get("name"):
                continue
            extra = "、".join(x for x in (c.get("gender"), c.get("role")) if x)
            lines.append(f"{c['name']} = {c['reading']}" + (f"（{extra}）" if extra else ""))
        if lines:
            known = ("\n【已确定的角色译名（必须沿用，不得自行更改）】\n"
                     + "\n".join(lines) + "\n")
    sample_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(chunk))
    user = f"""以下是一批游戏文本样本（{lang['source']}语，第 {idx}/{total} 块，{len(chunk)} 条）。
{known}
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
- 上面已给出的角色译名不要重复列出。
- 没有某类词条就省略该分类键。
- 严格输出 JSON，不要任何其他文字。

【样本】
{sample_block}"""
    return llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         temperature=0.2, max_tokens=3000)


def normalize_glossary(raw: dict) -> dict:
    """把 LLM 返回的 glossary 归一化：确保 terms 是 {分类: {原词: 译法}}。

    同时保留风格圣经字段（game_context / naming_policy / register_guide /
    characters / term_conflicts），前端编辑保存后回存不会丢失。
    """
    if not isinstance(raw, dict):
        raise LLMError("analysis output is not an object")

    def _s(key):
        v = raw.get(key, "")
        return v if isinstance(v, str) else ("" if v is None else str(v))

    def _d(key):
        v = raw.get(key, {})
        return {k: str(x) for k, x in v.items()} if isinstance(v, dict) else {}

    out = {
        "worldview": _s("worldview"),
        "game_context": raw.get("game_context", {}) if isinstance(raw.get("game_context"), dict) else {},
        "naming_policy": _s("naming_policy"),
        "register_guide": _d("register_guide"),
        "translation_notes": _s("translation_notes"),
        "characters": _normalize_characters(raw.get("characters")),
        "terms": {},
    }
    conflicts = raw.get("term_conflicts")
    if isinstance(conflicts, list):
        out["term_conflicts"] = [c for c in conflicts if isinstance(c, dict)]
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
