"""翻译器：带词汇表+世界观上下文的批量精翻。

特性：
- 分批提交，每批返回 {原文: 译文} JSON 映射
- 占位符（<SG...>、\\n、\\v[1]）强制保留
- 并发请求（ThreadPoolExecutor）
- 断点续传：进度存 progress.json，失败/中断可续跑
- 结果逐条校验：键必须匹配，值非空
- R18 内容忠实翻译，不做审查弱化
- 超长文本单独小批，防上下文/输出截断
- 术语库按批次子串过滤，只注入本批出现的词条（省 token）
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .llm import LLMClient, LLMError
from .reader import PLACEHOLDER_RE
from .analyzer import TaskStopped

# 超长文本阈值：超过则单独小批
LONG_TEXT_LEN = 180
# 超长文本批次大小
LONG_BATCH_SIZE = 3


def filter_terms_for_batch(terms: dict, batch: list) -> dict:
    """按批次文本过滤术语库：只保留在批次文本中出现的词条（子串匹配）。

    省 token 的关键：大批文本往往只涉及少量专有名词，
    全量注入术语库（可能几百条）是巨大浪费。
    """
    joined = "\n".join(batch)
    out = {}
    for cat, mapping in terms.items():
        if not isinstance(mapping, dict):
            continue
        keep = {s: d for s, d in mapping.items() if s and s in joined}
        if keep:
            out[cat] = keep
    return out


# ---------------- 上下文窗口（组合句子翻译） ----------------
# mtool 按行提取文本，原文常被截断成碎片；逐行翻译会丢失上下文导致语义割裂。
# 方案：按原文顺序分批次，并把每行的前后邻行（原文）注入 prompt 作为上下文，
# 让模型翻译单行时能理解它在完整对话/叙述中的位置。

def build_contextual_batches(need_keys: list, all_keys: list, batch_size: int,
                             window: int = 3) -> list:
    """按原文顺序构建批次，每个批次附带邻行上下文信息。

    need_keys: 待译键（保持原文顺序）
    all_keys: 原文件全部键（有序）
    window: 每批在原文中向外扩展的邻行数
    返回: [(batch_keys, context_lines), ...]
      context_lines: [(line_no, key, is_target), ...]（含邻行，is_target 标记是否本批待译）
    """
    if not need_keys:
        return []
    idx_of = {k: i for i, k in enumerate(all_keys)}
    # 待译键按原文位置排序（need_keys 可能已乱序）
    ordered = sorted(need_keys, key=lambda k: idx_of.get(k, 10 ** 9))
    batches = []
    n = len(all_keys)
    for i in range(0, len(ordered), batch_size):
        chunk = ordered[i:i + batch_size]
        min_idx = max(0, min(idx_of[k] for k in chunk) - window)
        max_idx = min(n - 1, max(idx_of[k] for k in chunk) + window)
        context = [(j, all_keys[j], all_keys[j] in chunk)
                   for j in range(min_idx, max_idx + 1)]
        batches.append((chunk, context))
    return batches


def render_context_block(context_lines: list) -> str:
    """把上下文行渲染为 prompt 片段（截断超长邻行省 token）。"""
    if not context_lines:
        return ""
    parts = []
    for no, key, is_target in context_lines:
        marker = "[待译]" if is_target else "[邻行]"
        text = key
        if len(text) > 60:
            text = text[:60] + "…"
        parts.append(f"{no}: {marker} {text}")
    return "\n".join(parts)


def build_translate_prompt(batch: list, glossary: dict, cfg: dict, batch_no: int = 0,
                           context_lines: list = None) -> list:
    """构造翻译用 messages：系统提示含世界观+术语库，用户消息含待译文本。

    context_lines: 邻行上下文（[(行号, 原文, 是否本批待译), ...]），
    提供原文行序上下文，缓解 mtool 按行截断导致的语义割裂。
    """
    lang = cfg["language"]
    tcfg = cfg.get("translation", {})
    r18_style = tcfg.get("r18_style", "explicit")
    name_style = tcfg.get("name_style", "auto")
    custom_prompt = tcfg.get("custom_prompt", "").strip()
    worldview = glossary.get("worldview", "")
    game_ctx = glossary.get("game_context", {})
    notes = glossary.get("translation_notes", "")
    terms = glossary.get("terms", {})
    target_note = lang.get("target_note", "简体中文，游戏本地化风格")

    # 术语库按本批过滤，只注入相关词条
    terms = filter_terms_for_batch(terms, batch)

    term_lines = []
    for cat, mapping in terms.items():
        if isinstance(mapping, dict) and mapping:
            term_lines.append(f"【{cat}】")
            for src, dst in mapping.items():
                term_lines.append(f"{src} = {dst}")
    term_block = "\n".join(term_lines) if term_lines else "（本批无专有名词）"

    ctx_parts = []
    if worldview:
        ctx_parts.append(f"【世界观】{worldview}")
    if game_ctx:
        if game_ctx.get("genre"):
            ctx_parts.append(f"【游戏类型】{game_ctx['genre']}")
        if game_ctx.get("setting"):
            ctx_parts.append(f"【设定】{game_ctx['setting']}")
        if game_ctx.get("story"):
            ctx_parts.append(f"【剧情】{game_ctx['story']}")
        if game_ctx.get("style_notes"):
            ctx_parts.append(f"【风格】{game_ctx['style_notes']}")
    if notes:
        ctx_parts.append(f"【翻译注意】{notes}")
    ctx_block = "\n".join(ctx_parts)

    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch))
    # 专有名词风格规则（用户可选：auto/kawaii/simple/transliterate）
    name_style_rules = {
        "kawaii": (
            "【命名风格：可爱化】专有名词（尤其角色名）的译法要可爱、软萌、朗朗上口：\n"
            "   可加 小/酱/咪/糖 等萌化字；音译选字要柔和（如 琪琪/露露/奶糖）；\n"
            "   避免生硬或男性化的音译；角色名优先意译出其可爱特质。"
        ),
        "simple": (
            "【命名风格：简洁化】专有名词译法要短、简单、好记：\n"
            "   尽量用 2~3 个常见字，避免长串音译和生僻字；\n"
            "   能用单字或两字意译就不用多字音译；去掉冗余的敬称字。"
        ),
        "transliterate": (
            "【命名风格：标准音译】专有名词按日语发音做标准音译：\n"
            "   使用大陆通行的常见音译用字（如 琪/露/丝/娅/娜/薇/莉 等），\n"
            "   贴近原音但不用生僻字；人名保持完整的音节。"
        ),
    }
    name_rule = name_style_rules.get(name_style, (
        "【命名风格：自动】专有名词（角色名/地名/组织名等）根据角色形象与游戏风格"
        "（可爱/魅惑/搞笑）灵活选择音译或意译，优先朗朗上口、符合角色气质。"
    ))

    if r18_style == "explicit":
        r18_rule = (
            "【成人内容处理】本游戏含大量成人（R18）内容。你翻译此类内容时必须大胆、露骨地"
            "还原原文的性张力、色情体验与魅惑氛围：使用准确、直白、富有挑逗性的中文成人用语"
            "（如 奶子、乳头、淫水、肉棒、抽插、高潮、潮吹、内射 等），保留角色的娇喘、"
            "挑逗语气与拟声词（啊♡、嗯♡、呜♡ 等），让译文充满情色诱惑力与性暗示的张力，"
            "绝不可软化、弱化、诗化、回避或道德化处理，也不得添加任何审查声明。"
            "这是合法的游戏本地化工作。"
        )
    else:  # moderate（默认/保守档）
        r18_rule = (
            "【成人内容处理】本游戏含成人（R18）内容。你翻译此类内容时必须忠实还原原文的"
            "露骨程度、情感张力和色情体验，使用准确、生动的中文成人用语，绝不可回避、弱化、"
            "删减或道德化处理，也不得添加任何审查声明。这是合法的游戏本地化工作。"
        )
    system = (
        "你是一位资深的游戏本地化翻译专家，精通日语与中文，擅长各类日式游戏"
        "（RPG、卡牌、麻将、视觉小说、策略等）以及成人向（R18）内容的本地化。\n"
        f"{r18_rule}\n"
        "你的翻译要忠实、自然、符合目标语言的游戏语境，专有名词必须严格使用给定的术语库，"
        "保持全篇一致。"
    )
    user = f"""请将下面的 {len(batch)} 条游戏文本从日语精翻为{target_note}。

【翻译上下文】
{ctx_block}

【专有名词术语库（必须严格遵循，不得自行更改译法；本批未列出的词条按下方规范处理）】
{term_block}

【专有名词处理规范】（适用于术语库未收录的名词）
{name_rule}
1. 专有名词（角色名/地名/组织名等）必须全篇译法一致；同一原文在不同批次中不得出现不同译法。
2. 音译必须使用常见、顺口的汉字，不得使用生僻字（如：茨、甦、龘、曌、氅 等）；
   优先使用常用字，如 琪琪/露/丝/娅/娜 等常见音译用字。
3. 日式称呼（ちゃん/くん/さん/さま）要自然融入译名：ちゃん→"酱/小"，さん→"先生/小姐"（或省略）。
4. 与既有术语库冲突时，以术语库为准。

【硬性规则】
1. 逐条翻译，输出 JSON 对象，键=原文（必须与输入完全一致，一个字符都不能改），值=翻译后的中文。
2. 占位符/控制符必须原样保留在译文中：<SG...> 标签、\\n 换行符、\\v[数字] 变量、{{数字}} 等。
3. 专有名词（角色名、物品名、势力名、专业术语）必须使用术语库译法；若原文在术语库中，译文必须与库中一致。
4. 翻译要自然流畅，符合中文游戏文本习惯；对话要符合角色语气；成人场景要保留原文的情色氛围与露骨程度。
5. 数字、数值、牌名后的 " : 数字" 映射保持原样。
6. 原文若包含换行（\\n），译文中也要保留相应的换行结构。
7. 【纯中文约束】译文必须是规范简体中文：除占位符（<SG...>、\\n、\\v[数字]）、数字、以及
   MAP001/EV001 这类代码标识外，不得残留任何日文假名、日文字、英文单词或字母串。
   术语库中的英文缩写（如 HOP）可保留原样。
8. 【禁止括号注释】译文不得添加原文没有的括号、括注、注释或解释性文字
   （如「小穴（女性生殖器俗语）」这种一律禁止）；原文本身含括号的除外。
9. 【游戏语境】这是游戏内文本（界面/剧情/对话/系统提示），不是操作系统或硬件文本：
   UI 词如 閉じる/クローズ/オフ 应译为"关闭/关掉"而非"关机/关闭电源"；
   术语按游戏界面习惯（选项/菜单/设置 等语境）。
10. 【句子碎片】原文常被 mtool 按行截断成碎片，单行可能不是完整句。
    翻译单行时参考【行上下文】理解它属于哪句话；若该行是句子的中间片段，
    译文要作为片段自然衔接（可用"的/了/然后"等连接词或保持句读节奏），
    不要硬补成完整句，也不要脱离上下文臆测。
11. 输出 ONLY 一个 JSON 对象，不要任何解释、注释或围栏。"""
    if custom_prompt:
        user += f"\n\n【用户附加要求】\n{custom_prompt}"

    # 邻行上下文（组合句子翻译）
    if context_lines:
        ctx_block_text = render_context_block(context_lines)
        user += (
            "\n\n【行上下文】以下是这些待译行在原文中的位置及前后邻行，"
            "用于理解语义。[待译]=本批要翻译的行，[邻行]=仅供参考不要翻译。\n"
            f"{ctx_block_text}"
        )

    user += f"\n\n【待翻译文本】\n{numbered}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _norm_key(s: str) -> str:
    """键归一化：全角→半角、去空白，用于模糊匹配。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).replace(" ", "").replace("\u3000", "")


def _merge_translations(batch: list, result: dict, out: dict, warnings: list) -> int:
    """校验并合并批次结果。返回成功条数。

    容错策略（模型偶尔会漏译或微改键）：
    1. 精确匹配
    2. 归一化匹配（全角/半角/空白）
    3. 仍未匹配的键【不做顺序对齐】——留给上层收敛轮重译。
       顺序对齐曾在模型输出顺序与输入不一致时造成键值错位（严重质量事故），
       错译比漏译危害大得多：漏译会重试，错译会静默进入成品。
    """
    ok = 0
    result_norm = {_norm_key(k): v for k, v in result.items() if isinstance(v, str)}
    unmatched = []
    for src in batch:
        val = None
        if src in result and isinstance(result[src], str):
            val = result[src]
        elif _norm_key(src) in result_norm:
            val = result_norm[_norm_key(src)]
        if val is not None and val.strip():
            # 校验占位符保留
            src_ph = PLACEHOLDER_RE.findall(src)
            dst_ph = PLACEHOLDER_RE.findall(val)
            if src_ph and src_ph != dst_ph:
                warnings.append(f"placeholder mismatch: {src!r}")
            # 【防错位】译文中出现"本批其他行原文的标签"= 键值错位的强特征
            # （错位值带着别的行的未翻译标签）。注意：模型会把 <SG説明:...> 的
            # 内容翻译成 <SG说明:...>，标签串会变，所以不能直接比较全集，
            # 只拒绝与本批【其他行】标签精确相同的情况。
            src_tags = [p for p in src_ph if p.startswith("<")]
            dst_tags = [p for p in dst_ph if p.startswith("<")]
            other_tags = set()
            for other in batch:
                if other == src:
                    continue
                other_tags.update(p for p in PLACEHOLDER_RE.findall(other) if p.startswith("<"))
            bad_tags = [t for t in dst_tags if t not in src_tags and t in other_tags]
            if bad_tags:
                warnings.append(f"misaligned-tag rejected: {src!r} -> {val[:60]!r}")
                unmatched.append(src)
                continue
            out[src] = val
            ok += 1
        else:
            unmatched.append(src)
    # 未匹配的键不写入任何值（防错位），返回让收敛轮重译
    return ok


class Translator:
    def __init__(self, llm: LLMClient, cfg: dict):
        self.llm = llm
        self.cfg = cfg
        self.pipeline_cfg = cfg["pipeline"]
        self.lock = threading.Lock()

    def translate_batch(self, batch: list, glossary: dict, batch_no: int = 0,
                        context_lines: list = None) -> dict:
        """翻译一个批次，返回 {原文: 译文}。

        context_lines: 邻行上下文（组合句子翻译），None 则纯按行翻译。
        """
        messages = build_translate_prompt(batch, glossary, self.cfg, batch_no, context_lines)
        result = self.llm.chat_json(messages)
        if not isinstance(result, dict):
            raise LLMError(f"batch {batch_no}: result not an object")
        return result

    @staticmethod
    def split_batches(keys: list, batch_size: int):
        """分批次：超长文本单独小批（防截断），普通文本按 batch_size 分。"""
        long_keys = [k for k in keys if len(k) > LONG_TEXT_LEN]
        normal_keys = [k for k in keys if len(k) <= LONG_TEXT_LEN]
        batches = []
        # 超长：每条一个批次，或小批（3 条）
        for i in range(0, len(long_keys), LONG_BATCH_SIZE):
            batches.append(long_keys[i:i + LONG_BATCH_SIZE])
        for i in range(0, len(normal_keys), batch_size):
            batches.append(normal_keys[i:i + batch_size])
        return batches

    def run(self, need_translate: dict, glossary: dict, progress_path: str,
            on_progress=None, all_keys: list = None, context_window: int = 3,
            stop_check=None) -> dict:
        """翻译全部待译条目。支持断点续传。

        need_translate: {原文: 原值}（键是唯一 ID）
        progress_path: 进度文件（{原文: 译文}），已存在的条目跳过
        all_keys: 原文件全部键（有序），用于构建行上下文（组合句子翻译）
        context_window: 上下文邻行窗口大小（0=关闭）
        返回合并后的 {原文: 译文} 完整结果。
        """
        batch_size = self.pipeline_cfg["batch_size"]
        concurrency = self.pipeline_cfg["concurrency"]

        # 载入进度
        done = {}
        if os.path.isfile(progress_path):
            try:
                with open(progress_path, encoding="utf-8") as f:
                    done = json.load(f)
            except (json.JSONDecodeError, OSError):
                done = {}

        pending = [k for k in need_translate if k not in done]
        print(f"[translate] 总待译 {len(need_translate)} 条，已完成 {len(done)} 条，本次需译 {len(pending)} 条")

        # 上下文窗口：0 表示关闭（纯按行翻译）
        use_ctx = bool(all_keys) and context_window > 0

        # 分批次（按原文顺序 + 邻行上下文）
        if use_ctx:
            batches = build_contextual_batches(pending, list(all_keys), batch_size, context_window)
            total_batches = len(batches)
            n_long = sum(1 for k in pending if len(k) > LONG_TEXT_LEN)
            print(f"[translate] 共 {total_batches} 批（行上下文窗口={context_window}，含超长 {n_long} 条），并发 {concurrency}")
        else:
            batches = [(b, None) for b in self.split_batches(pending, batch_size)]
            total_batches = len(batches)
            n_long = sum(1 for k in pending if len(k) > LONG_TEXT_LEN)
            print(f"[translate] 共 {total_batches} 批（无行上下文），并发 {concurrency}")

        completed = 0
        failed_batches = []
        warnings = []

        def work(bi, batch_info):
            batch, ctx = batch_info
            try:
                result = self.translate_batch(batch, glossary, bi, ctx)
                return bi, batch, result, None
            except LLMError as e:
                return bi, batch, None, e

        if total_batches == 0:
            return dict(done)

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {ex.submit(work, i, b): i for i, b in enumerate(batches)}
                for fut in as_completed(futures):
                    if stop_check is not None and stop_check():
                        for f2 in futures:
                            f2.cancel()
                        raise TaskStopped("用户停止了任务")
                    bi, batch, result, err = fut.result()
                    if err is not None:
                        failed_batches.append((bi, batch, err))
                    else:
                        with self.lock:
                            merged = _merge_translations(batch, result, done, warnings)
                            completed += merged
                            # 每批完成即保存进度（防中断丢失，文件小开销可忽略）
                            self._save_progress(done, progress_path)
                    if on_progress:
                        on_progress(completed, len(pending), len(failed_batches), err)
        except TaskStopped:
            raise
        finally:
            # 中断/异常也尽量保存已完成的进度
            with self.lock:
                self._save_progress(done, progress_path)

        # 多轮收敛：把漏译/键不匹配的条目重试（最多 3 轮）
        for round_no in range(1, 4):
            if stop_check is not None and stop_check():
                raise TaskStopped("用户停止了任务")
            still_missing = [k for k in pending if k not in done]
            if not still_missing:
                break
            print(f"\n[translate] 第 {round_no} 轮收敛：补译漏译 {len(still_missing)} 条...")
            if use_ctx:
                retry_batches = build_contextual_batches(still_missing, list(all_keys), batch_size, context_window)
            else:
                retry_batches = [(b, None) for b in self.split_batches(still_missing, batch_size)]
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {ex.submit(work, i, b): i for i, b in enumerate(retry_batches)}
                for fut in as_completed(futures):
                    bi, batch, result, err = fut.result()
                    if err is not None:
                        failed_batches.append((bi, batch, err))
                    else:
                        with self.lock:
                            merged = _merge_translations(batch, result, done, warnings)
                            completed += merged
                            self._save_progress(done, progress_path)
            self._save_progress(done, progress_path)

        # 清理混入的非翻译键
        for junk in list(done):
            if junk.startswith("_"):
                del done[junk]
        self._save_progress(done, progress_path)

        if failed_batches:
            print(f"\n[translate] 警告：{len(failed_batches)} 批失败，未写入进度。")
            for bi, batch, err in failed_batches[:5]:
                print(f"  batch {bi} ({len(batch)}条): {err}")
            print("  可重新运行本命令续跑（失败的批次会重试）")
        if warnings:
            print(f"[translate] 注意：{len(warnings)} 条占位符与原文本不一致（已记录，可人工检查）")
            for w in warnings[:3]:
                print(f"  {w[:120]}")

        return dict(done)

    @staticmethod
    def _save_progress(done: dict, progress_path: str) -> None:
        tmp = progress_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=2)
        os.replace(tmp, progress_path)

    # ---------- 精修（美化）轮 ----------
    def polish(self, translations: dict, glossary: dict, cfg: dict,
               threshold: int = 80, batch_size: int = 5,
               on_progress=None) -> int:
        """对已翻译文本做一轮精修润色。

        只处理值得精修的条目：长文本（剧情/说明/诗歌/谜语）、含 <SG> 占位符之外的
        叙事性内容。返回成功润色条数。就地更新 translations（dict）。
        """
        candidates = {}
        for src, dst in translations.items():
            if src.startswith("_") or not isinstance(dst, str):
                continue
            # 值得精修：长度超过阈值，或含诗歌/谜语类标记
            if len(dst) >= threshold or any(m in src for m in ("歌", "詩", "謎", "詩", "俳句", "短歌", "呪文", "詠唱")):
                candidates[src] = dst
        if not candidates:
            print("[polish] 没有符合条件的条目，跳过精修")
            return 0
        print(f"[polish] 共 {len(candidates)} 条候选（阈值 {threshold}），开始精修...")

        keys = list(candidates.keys())
        batches = [keys[i:i + batch_size] for i in range(0, len(keys), batch_size)]
        done_count = 0
        failed = 0

        for bi, batch in enumerate(batches):
            messages = self._build_polish_prompt(batch, candidates, glossary, cfg)
            try:
                result = self.llm.chat_json(messages, temperature=0.5)
                if not isinstance(result, dict):
                    failed += 1
                    continue
                for src in batch:
                    if src in result and isinstance(result[src], str) and result[src].strip():
                        translations[src] = result[src].strip()
                        done_count += 1
                    elif _norm_key(src) in {_norm_key(k): v for k, v in result.items() if isinstance(v, str)}:
                        rn = {_norm_key(k): v for k, v in result.items() if isinstance(v, str)}
                        translations[src] = rn[_norm_key(src)].strip()
                        done_count += 1
            except LLMError as e:
                failed += 1
                print(f"[polish] 批次 {bi} 失败: {e}")
            if on_progress:
                on_progress(done_count + failed, len(batches), failed, None)

        print(f"[polish] 完成：润色 {done_count} 条，失败 {failed} 批")
        return done_count

    def _build_polish_prompt(self, batch: list, candidates: dict, glossary: dict, cfg: dict) -> list:
        """构造精修 prompt：给原文+现译文，要求润色得更优美。"""
        worldview = glossary.get("worldview", "")
        style_notes = glossary.get("translation_notes", "")
        terms = glossary.get("terms", {})
        # 术语库：只注入本批涉及的
        term_lines = []
        joined = "\n".join(batch)
        for cat, mapping in terms.items():
            if isinstance(mapping, dict):
                keep = {s: d for s, d in mapping.items() if s and s in joined}
                if keep:
                    term_lines.append(f"【{cat}】" + "、".join(f"{s}={d}" for s, d in keep.items()))
        term_block = "\n".join(term_lines) if term_lines else "（无）"

        system = (
            "你是一位资深的游戏本地化润色专家，精通日语与中文，擅长让译文在忠实原意的基础上"
            "更优美、更有文采、更贴合中文游戏文本的审美。尤其擅长诗歌/谜语/咒文的韵律感处理，"
            "以及成人场景的魅惑氛围渲染。"
        )
        # 提供原文+现译文对照，让模型知道要改什么
        pairs = "\n".join(f"{i + 1}. 【原文】{s}\n   【现译文】{candidates[s]}" for i, s in enumerate(batch))
        user = f"""请对下列已翻译的游戏文本进行精修润色（polish），目标：更优美、更有文采、更自然，避免生硬直白。

【硬性规则】
1. 保持原意、专有名词、占位符（<SG...>、\\n、\\v[数字] 等）完全不变。
2. 诗歌/谜语/咒文类文本：保留韵律感、对仗与意境；谜语保持提示性；咒文保留仪式感。
3. 成人场景：增强魅惑感与挑逗性，用词更鲜活露骨（与原文露骨程度一致）。
4. 术语库词条必须沿用，不得改动。
5. 【禁止括号注释】不得添加原文没有的括号、括注、注释或解释性文字。
6. 输出 JSON 对象：键=【原文】（必须逐字一致），值=润色后的译文。
7. 每条译文要独立润色，不要互相影响。

【世界观】{worldview}
【翻译注意】{style_notes}
【术语库】{term_block}

【待润色条目】
{pairs}"""
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
