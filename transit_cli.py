#!/usr/bin/env python3
"""TransIt CLI — Mtools 翻译文件的 AI 精翻流水线。

用法:
  python transit_cli.py analyze    <input.json>   # 分析世界观+建术语库
  python transit_cli.py translate  <input.json>   # 批量精翻（断点续传）
  python transit_cli.py run        <input.json>   # 全流程 analyze + translate + export
  python transit_cli.py export     <input.json>   # 仅从进度合并导出

可选参数:
  --config <path>   配置文件（默认：数据目录下的 config.json）
  --model <name>    覆盖模型
  --out <dir>       输出目录（默认 config 里的 output_dir；相对路径锚定数据目录）
  --batch <n>       批大小
  --workers <n>     并发数
  --glossary <path> 指定/复用术语库文件

数据目录：源码运行时是项目根目录，打包成 exe 后是 exe 所在目录
（可用环境变量 TRANSIT_DATA_DIR 覆盖）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transit import paths
from transit.config import load_config, ensure_config_file
from transit.llm import LLMClient
from transit.reader import load_mt_file, analyze_file, file_hash
from transit.analyzer import (
    sample_texts, run_analysis, normalize_glossary,
    save_glossary, load_glossary, flatten_terms,
)
from transit.translator import Translator
from transit.writer import export_file


def make_paths(cfg: dict, input_path: str, out_dir: str, glossary_path: str = None,
               src_hash: str = None):
    """生成输出路径。src_hash 用于区分不同翻译对象（mtool 文件名可能相同）。

    相对 output_dir 锚定数据目录（源码=项目根 / 冻结=exe 同目录），不依赖 CWD。
    """
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_dir = paths.resolve_output_dir(
        out_dir or cfg["pipeline"].get("output_dir", "output"))
    # 带 hash 后缀，避免不同文件的 ManualTransFile.json 互相覆盖/串翻译
    tag = f"{base}_{src_hash}" if src_hash else base
    return {
        "glossary": glossary_path or os.path.join(out_dir, f"{tag}.glossary.json"),
        "progress": os.path.join(out_dir, f"{tag}.progress.json"),
        "output": os.path.join(out_dir, f"{tag}.translated.json"),
    }


def make_progress_callback(log_path: str = None):
    """返回进度回调：打印百分比 + ETA，并（可选）实时追加写入日志文件。

    log_path: 若提供，进度行同时追加到该文件（后台任务可用 tail 实时查看）。
    """
    import time
    start = time.time()
    last_pct = -1

    def _emit(status):
        print(status, flush=True)
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(status + "\n")
            except OSError:
                pass

    def cb(completed, total, failed, err):
        nonlocal last_pct
        if total == 0:
            return
        pct = completed * 100 // total
        if pct != last_pct or err is not None:
            last_pct = pct
            elapsed = time.time() - start
            if completed > 0:
                eta = elapsed / completed * (total - completed)
                eta_s = f"{int(eta // 60):02d}:{int(eta % 60):02d}"
            else:
                eta_s = "--:--"
            status = f"[{time.strftime('%H:%M:%S')}] 进度 {pct:3d}% ({completed}/{total}) | 已用 {int(elapsed // 60):02d}:{int(elapsed % 60):02d} | 剩余约 {eta_s}"
            if err:
                status += f" | ⚠ 失败批 +1: {err}"
            _emit(status)

    return cb


def cmd_analyze(args, cfg, llm):
    input_path = args.input
    data = load_mt_file(input_path)
    groups = analyze_file(data)
    need = groups["need_translate"]
    print(f"[read] 共 {len(data)} 条: 跳过 {len(groups['passthrough'])} | "
          f"已有译文 {len(groups['translated'])} | 待译 {len(need)}")

    samples = sample_texts(need, cfg["pipeline"].get("sample_size", 120))
    print(f"[analyze] 采样 {len(samples)} 条文本进行分析...")
    raw = run_analysis(llm, samples, cfg)
    glossary = normalize_glossary(raw)

    paths = make_paths(cfg, input_path, args.out, args.glossary, file_hash(input_path))
    save_glossary(glossary, paths["glossary"])

    n_terms = sum(len(v) for v in glossary["terms"].values())
    print(f"[analyze] 完成: 术语库 {n_terms} 个词条 -> {paths['glossary']}")
    print(f"[analyze] 世界观: {glossary['worldview'][:120]}...")
    for cat, mp in glossary["terms"].items():
        print(f"  - {cat}: {list(mp.items())[:6]}{'...' if len(mp) > 6 else ''}")


def cmd_translate(args, cfg, llm):
    input_path = args.input
    data = load_mt_file(input_path)
    groups = analyze_file(data)
    need = groups["need_translate"]
    paths = make_paths(cfg, input_path, args.out, args.glossary, file_hash(input_path))

    if not os.path.isfile(paths["glossary"]):
        print(f"[translate] 找不到术语库 {paths['glossary']}，请先运行 analyze")
        sys.exit(1)
    glossary = load_glossary(paths["glossary"])
    n_terms = sum(len(v) for v in glossary.get("terms", {}).values())
    print(f"[translate] 使用术语库: {n_terms} 词条")

    if args.limit:
        need = dict(list(need.items())[:args.limit])
        print(f"[translate] 测试模式：仅处理前 {args.limit} 条")

    # 进度实时写入日志文件（后台运行时可 tail 查看）
    log_path = os.path.splitext(os.path.basename(input_path))[0] + ".translate.log"
    log_path = os.path.join(os.path.dirname(paths["progress"]), log_path)
    translator = Translator(llm, cfg)
    translations = translator.run(need, glossary, paths["progress"],
                                  on_progress=make_progress_callback(log_path),
                                  all_keys=list(data.keys()),
                                  context_window=cfg.get("translation", {}).get("context_window", 3))

    # 精修（美化）轮——可选
    tcfg = cfg.get("translation", {})
    if args.polish or tcfg.get("polish"):
        print(f"[translate] 开始精修润色（阈值 {tcfg.get('polish_threshold', 80)}）...")
        translator.polish(translations, glossary, cfg,
                          threshold=tcfg.get("polish_threshold", 80))
        with open(paths["progress"], "w", encoding="utf-8") as f:
            json.dump(translations, f, ensure_ascii=False, indent=2)
        print("[translate] 精修结果已写回进度")

    print(f"[translate] 进度保存于 {paths['progress']}")
    print(f"[translate] 日志（实时进度）: {log_path}")
    print(f"[translate] token 用量: {llm.usage_report()}")


def cmd_export(args, cfg):
    input_path = args.input
    data = load_mt_file(input_path)
    groups = analyze_file(data)
    paths = make_paths(cfg, input_path, args.out, args.glossary, file_hash(input_path))

    if not os.path.isfile(paths["progress"]):
        print(f"[export] 找不到进度文件 {paths['progress']}，请先运行 translate")
        sys.exit(1)
    with open(paths["progress"], encoding="utf-8") as f:
        translations = json.load(f)
    export_file(data, groups, translations, paths["output"])


def cmd_run(args, cfg, llm):
    input_path = args.input
    data = load_mt_file(input_path)
    groups = analyze_file(data)
    need = groups["need_translate"]
    paths = make_paths(cfg, input_path, args.out, args.glossary, file_hash(input_path))

    if not os.path.isfile(paths["glossary"]) or args.force_analyze:
        print(f"[run] 第一步：分析世界观并建术语库")
        samples = sample_texts(need, cfg["pipeline"].get("sample_size", 120))
        raw = run_analysis(llm, samples, cfg)
        glossary = normalize_glossary(raw)
        save_glossary(glossary, paths["glossary"])
        n_terms = sum(len(v) for v in glossary["terms"].values())
        print(f"[run] 术语库 {n_terms} 词条 -> {paths['glossary']}")
    else:
        glossary = load_glossary(paths["glossary"])
        n_terms = sum(len(v) for v in glossary.get("terms", {}).values())
        print(f"[run] 复用现有术语库: {n_terms} 词条")

    translator = Translator(llm, cfg)
    log_path = os.path.splitext(os.path.basename(input_path))[0] + ".translate.log"
    log_path = os.path.join(os.path.dirname(paths["progress"]), log_path)
    translations = translator.run(need, glossary, paths["progress"],
                                  on_progress=make_progress_callback(log_path),
                                  all_keys=list(data.keys()),
                                  context_window=cfg.get("translation", {}).get("context_window", 3))
    export_file(data, groups, translations, paths["output"])
    print(f"[run] token 用量: {llm.usage_report()}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="TransIt: Mtools 翻译文件 AI 精翻")
    parser.add_argument("cmd", choices=["analyze", "translate", "run", "export"])
    parser.add_argument("input", help="输入的 Mtools 翻译 JSON 文件")
    parser.add_argument("--config", default=None,
                        help="配置文件（默认：数据目录下的 config.json）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=None, help="输出目录（相对路径锚定数据目录）")
    parser.add_argument("--batch", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--glossary", default=None, help="术语库路径")
    parser.add_argument("--force-analyze", action="store_true", help="run 时强制重新分析")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条待译文本（测试用）")
    parser.add_argument("--polish", action="store_true", help="翻译后执行精修润色轮")
    parser.add_argument("--prompt", default=None, help="追加自定义提示词（附加到翻译指令）")
    args = parser.parse_args(argv)

    if args.config:
        # 用户显式给出的路径按 CWD 解析（符合命令行直觉）
        cfg_path = paths.resolve_user_path(args.config)
    else:
        cfg_path, created = ensure_config_file()
        if created:
            print(f"[config] 已生成默认配置：{cfg_path}（请填入 API key）")
    cfg = load_config(cfg_path)
    if args.model:
        cfg["api"]["model"] = args.model
    if args.batch:
        cfg["pipeline"]["batch_size"] = args.batch
    if args.workers:
        cfg["pipeline"]["concurrency"] = args.workers
    if args.prompt:
        cfg.setdefault("translation", {})["custom_prompt"] = args.prompt

    # export 只用本地进度文件，不需要 API key
    needs_llm = args.cmd in ("analyze", "translate", "run")
    if needs_llm and not cfg["api"].get("api_key"):
        print(f"[error] 未配置 API key：请编辑 {cfg_path} 的 api.api_key，"
              f"或设置环境变量 TRANSIT_API_KEY")
        sys.exit(1)

    llm = LLMClient(**cfg["api"]) if needs_llm else None

    if args.cmd == "analyze":
        cmd_analyze(args, cfg, llm)
    elif args.cmd == "translate":
        cmd_translate(args, cfg, llm)
    elif args.cmd == "export":
        cmd_export(args, cfg)
    elif args.cmd == "run":
        cmd_run(args, cfg, llm)


if __name__ == "__main__":
    main()
