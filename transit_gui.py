#!/usr/bin/env python3
"""TransIt GUI — 图形化界面。

功能：
- 配置 API（base_url / key / model / 温度）与流水线参数（并发/批大小/采样数）
- 选择输入文件，读取并统计
- 运行 analyze：世界观分析 + 术语建库（进度显示）
- 查看/编辑术语库（增删改词条、改分类、改世界观概述）
- 运行 translate：批量精翻（实时进度条 + 日志）
- 查看/修正译文：搜索原文/译文，逐句修改后写回进度
- 导出最终翻译文件

依赖：tkinter（Python 标准库，Windows 自带）
"""
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transit import paths
from transit.config import load_config, DEFAULT_CONFIG
from transit.llm import LLMClient, LLMError
from transit.reader import load_mt_file, analyze_file, file_hash
from transit.analyzer import (
    sample_texts, run_analysis, normalize_glossary,
    save_glossary, load_glossary,
)
from transit.translator import Translator
from transit.writer import export_file


class TransItGUI:
    def __init__(self, root):
        self.root = root
        root.title("TransIt — Mtools 翻译文件 AI 精翻")
        root.geometry("1080x760")

        # 锚定数据目录，避免依赖进程 CWD（用快捷方式启动时 CWD 可能任意）
        self.cfg = load_config(paths.default_config_path())
        self.input_path = None
        self.data = None
        self.groups = None
        self.glossary = None
        self.translations = {}  # {原文: 译文} 最终结果
        self.translator = None
        self.worker = None
        self.stop_flag = False

        self._build_ui()
        self._log("TransIt 就绪。请先选择输入文件，然后依次：分析 -> 翻译 -> 导出。")

    # ================= UI 构建 =================
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        # ---- 文件选择 ----
        ttk.Label(top, text="输入文件:").pack(side="left")
        self.file_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.file_var, width=55).pack(side="left", padx=4)
        ttk.Button(top, text="浏览...", command=self._pick_file).pack(side="left")
        ttk.Button(top, text="读取统计", command=self._load_stats).pack(side="left", padx=6)
        self.stats_var = tk.StringVar(value="未读取")
        ttk.Label(top, textvariable=self.stats_var).pack(side="left", padx=6)

        # ---- 配置区（可折叠 Notebook）----
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_api_tab()
        self._build_glossary_tab()
        self._build_translate_tab()
        self._build_review_tab()

        # ---- 底部状态/日志 ----
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=2)

    def _build_api_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="API 配置")
        rows = [
            ("Base URL", "base_url", 60),
            ("API Key", "api_key", 60),
            ("模型", "model", 45),
            ("温度", "temperature", 10),
        ]
        self.api_vars = {}
        for i, (label, key, width) in enumerate(rows):
            ttk.Label(f, text=label + ":").grid(row=i, column=0, sticky="e", pady=3)
            var = tk.StringVar(value=str(self.cfg["api"].get(key, "")))
            self.api_vars[key] = var
            ent = ttk.Entry(f, textvariable=var, width=width, show="*" if key == "api_key" else "")
            ent.grid(row=i, column=1, sticky="w", pady=3)
        ttk.Label(f, text="提示：Key 支持显示/隐藏，模型可填 SiliconFlow 或任意 OpenAI 兼容模型名。").grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=6)
        ttk.Button(f, text="应用配置", command=self._apply_api).grid(
            row=len(rows) + 1, column=0, columnspan=2, pady=6)

        # 流水线参数
        ttk.Label(f, text="— 流水线参数 —").grid(row=len(rows) + 2, column=0, columnspan=2, sticky="w", pady=(10, 2))
        pl = self.cfg.get("pipeline", {})
        self.pl_vars = {}
        pl_rows = [
            ("并发数", "concurrency"),
            ("批大小", "batch_size"),
            ("采样数(分析)", "sample_size"),
            ("最大重试", "max_retries"),
        ]
        for i, (label, key) in enumerate(pl_rows):
            ttk.Label(f, text=label + ":").grid(row=len(rows) + 3 + i, column=0, sticky="e", pady=2)
            var = tk.StringVar(value=str(pl.get(key, "")))
            self.pl_vars[key] = var
            ttk.Entry(f, textvariable=var, width=10).grid(row=len(rows) + 3 + i, column=1, sticky="w", pady=2)

    def _build_glossary_tab(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="名词库")
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="从文件加载", command=self._load_glossary_file).pack(side="left")
        ttk.Button(top, text="保存到文件", command=self._save_glossary_file).pack(side="left", padx=4)
        ttk.Button(top, text="新增词条", command=self._glossary_add).pack(side="left", padx=4)
        ttk.Button(top, text="删除选中", command=self._glossary_delete).pack(side="left", padx=4)
        ttk.Button(top, text="在翻译前更新", command=self._apply_glossary).pack(side="right")
        self.glossary_status = tk.StringVar(value="未加载")
        ttk.Label(top, textvariable=self.glossary_status).pack(side="right", padx=8)

        # 词条表格：分类 | 原文 | 译文（带滚动条）
        tree_frame = ttk.Frame(f)
        tree_frame.pack(fill="both", expand=True, pady=4)
        cols = ("分类", "原文", "译文")
        self.gtree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=12)
        for c, w in zip(cols, (110, 240, 240)):
            self.gtree.heading(c, text=c)
            self.gtree.column(c, width=w)
        gy = ttk.Scrollbar(tree_frame, orient="vertical", command=self.gtree.yview)
        gx = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.gtree.xview)
        self.gtree.configure(yscrollcommand=gy.set, xscrollcommand=gx.set)
        self.gtree.grid(row=0, column=0, sticky="nsew")
        gy.grid(row=0, column=1, sticky="ns")
        gx.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.gtree.bind("<Double-1>", self._glossary_edit_dialog)

        # 世界观概述
        ttk.Label(f, text="世界观 / 翻译注意（双击表格编辑词条，此处编辑全局上下文）:").pack(anchor="w")
        self.glossary_notes = scrolledtext.ScrolledText(f, height=6)
        self.glossary_notes.pack(fill="x", pady=2)

    def _build_translate_tab(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="翻译")
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="▶ 运行分析(建库)", command=self._run_analyze).pack(side="left")
        ttk.Button(top, text="▶ 开始翻译", command=self._run_translate).pack(side="left", padx=6)
        ttk.Button(top, text="⏹ 停止", command=self._stop).pack(side="left")
        ttk.Button(top, text="▶ 导出文件", command=self._run_export).pack(side="left", padx=6)
        self.limit_var = tk.StringVar(value="0")
        ttk.Label(top, text="测试条数(0=全部):").pack(side="left", padx=(16, 2))
        ttk.Entry(top, textvariable=self.limit_var, width=6).pack(side="left")

        # 翻译选项行：R18 风格 / 名词风格 / 精修 / 阈值
        opt = ttk.Frame(f)
        opt.pack(fill="x", pady=4)
        ttk.Label(opt, text="R18风格:").pack(side="left")
        self.r18_var = tk.StringVar(value=self.cfg.get("translation", {}).get("r18_style", "explicit"))
        ttk.Combobox(opt, textvariable=self.r18_var, values=("explicit", "moderate"),
                     width=9, state="readonly").pack(side="left", padx=(2, 10))
        ttk.Label(opt, text="名词风格:").pack(side="left")
        self.name_style_var = tk.StringVar(value=self.cfg.get("translation", {}).get("name_style", "auto"))
        ttk.Combobox(opt, textvariable=self.name_style_var,
                     values=("auto", "kawaii", "simple", "transliterate"),
                     width=12, state="readonly").pack(side="left", padx=(2, 10))
        self.polish_var = tk.BooleanVar(value=bool(self.cfg.get("translation", {}).get("polish", False)))
        ttk.Checkbutton(opt, text="精修润色(美化)", variable=self.polish_var).pack(side="left")
        ttk.Label(opt, text="阈值:").pack(side="left", padx=(8, 2))
        self.polish_threshold_var = tk.StringVar(
            value=str(self.cfg.get("translation", {}).get("polish_threshold", 80)))
        ttk.Entry(opt, textvariable=self.polish_threshold_var, width=5).pack(side="left")
        ttk.Label(opt, text="上下文行:").pack(side="left", padx=(10, 2))
        self.ctx_win_var = tk.StringVar(
            value=str(self.cfg.get("translation", {}).get("context_window", 3)))
        ttk.Entry(opt, textvariable=self.ctx_win_var, width=4).pack(side="left")
        ttk.Label(opt, text="(0=关,组合句子用)").pack(side="left", padx=2)

        # 自定义提示词
        ttk.Label(f, text="自定义提示词（附加到每条翻译指令，可空）:").pack(anchor="w", pady=(6, 0))
        self.custom_prompt_text = scrolledtext.ScrolledText(f, height=4)
        self.custom_prompt_text.pack(fill="x", pady=2)
        if self.cfg.get("translation", {}).get("custom_prompt"):
            self.custom_prompt_text.insert("1.0", self.cfg["translation"]["custom_prompt"])

        self.log = scrolledtext.ScrolledText(f, height=18, state="disabled")
        self.log.pack(fill="both", expand=True, pady=4)

    def _build_review_tab(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="译文修正")
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.search_var = tk.StringVar()
        ttk.Label(top, text="搜索:").pack(side="left")
        ttk.Entry(top, textvariable=self.search_var, width=40).pack(side="left", padx=4)
        ttk.Button(top, text="查找", command=self._review_search).pack(side="left")
        ttk.Button(top, text="保存修改", command=self._review_save).pack(side="left", padx=6)
        ttk.Button(top, text="刷新", command=self._review_refresh).pack(side="left")

        cols = ("原文", "译文")
        rtree_frame = ttk.Frame(f)
        rtree_frame.pack(fill="both", expand=True, pady=4)
        self.rtree = ttk.Treeview(rtree_frame, columns=cols, show="headings", height=16)
        for c, w in zip(cols, (520, 520)):
            self.rtree.heading(c, text=c)
            self.rtree.column(c, width=w)
        ry = ttk.Scrollbar(rtree_frame, orient="vertical", command=self.rtree.yview)
        rx = ttk.Scrollbar(rtree_frame, orient="horizontal", command=self.rtree.xview)
        self.rtree.configure(yscrollcommand=ry.set, xscrollcommand=rx.set)
        self.rtree.grid(row=0, column=0, sticky="nsew")
        ry.grid(row=0, column=1, sticky="ns")
        rx.grid(row=1, column=0, sticky="ew")
        rtree_frame.rowconfigure(0, weight=1)
        rtree_frame.columnconfigure(0, weight=1)
        self.rtree.bind("<Double-1>", self._review_edit)

    # ================= 文件 =================
    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="选择 Mtools 翻译 JSON",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if p:
            self.file_var.set(p)
            self._load_stats()

    def _clear_state(self):
        """彻底清除当前文件的所有状态（切换文件/读取失败时调用）。"""
        self.data = None
        self.groups = None
        self.input_path = None
        self.glossary = None
        self.glossary_path = None
        self.translations = {}
        self.progress_path = None
        self.log_path = None
        self.file_hash = None
        self._refresh_glossary_tree()   # 清空名词库表格
        self._review_refresh()          # 清空译文修正表格
        self.gtree.delete(*self.gtree.get_children())
        self.glossary_status.set("未加载")
        self.glossary_notes.delete("1.0", "end")
        self.progress["value"] = 0

    def _load_stats(self):
        p = self.file_var.get().strip()
        if not p or not os.path.isfile(p):
            # 文件不存在/未选择：同样清空旧状态，防止残留
            self._clear_state()
            self.stats_var.set("未读取")
            if p and not os.path.isfile(p):
                messagebox.showerror("错误", "文件不存在")
            return
        try:
            data = load_mt_file(p)
            groups = analyze_file(data)
            # 切换文件时彻底清除上一个文件的全部状态，防止串数据
            self._clear_state()
            self.data = data
            self.groups = groups
            self.input_path = p
            self.file_hash = file_hash(p)
            g = self.groups
            self.stats_var.set(
                f"共 {len(self.data)} 条 | 跳过 {len(g['passthrough'])} | "
                f"已有译文 {len(g['translated'])} | 待译 {len(g['need_translate'])}")
            self._log(f"已读取 {p}：{len(self.data)} 条，待译 {len(g['need_translate'])} 条"
                      f"（文件标识 {self.file_hash}，输出按此隔离）")
            self._set_status(f"已加载：{self.file_hash}（hash 标识）")
        except Exception as e:
            # 读取失败也清空旧状态
            self._clear_state()
            self.stats_var.set("未读取")
            messagebox.showerror("读取失败", str(e))

    # ================= API 配置 =================
    def _apply_api(self):
        for k, var in self.api_vars.items():
            v = var.get().strip()
            if k == "temperature":
                try:
                    self.cfg["api"][k] = float(v)
                except ValueError:
                    messagebox.showerror("错误", "温度必须是数字")
                    return
            else:
                self.cfg["api"][k] = v
        for k, var in self.pl_vars.items():
            try:
                self.cfg["pipeline"][k] = int(var.get().strip())
            except ValueError:
                pass
        self._make_llm()
        self._log(f"配置已应用：模型={self.cfg['api']['model']}，并发={self.cfg['pipeline']['concurrency']}")

    def _make_llm(self):
        self.llm = LLMClient(**self.cfg["api"])

    # ================= 日志 =================
    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.root.update_idletasks()

    def _set_status(self, msg):
        self.status_var.set(msg)
        self.root.update_idletasks()

    # ================= 分析 =================
    def _run_analyze(self):
        if not self.data:
            self._load_stats()
            if not self.data:
                messagebox.showerror("错误", "请先选择并读取输入文件")
                return
        if not hasattr(self, "llm"):
            self._make_llm()
        self._log("开始分析世界观与术语...")
        self._set_status("分析中...")
        # 分析为单次大调用，用不确定进度条表示进行中
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._worker_start(self._analyze_worker)

    def _analyze_worker(self):
        try:
            need = self.groups["need_translate"]
            n = self.cfg["pipeline"].get("sample_size", 120)
            self._log(f"采样 {n} 条文本，分阶段分析（世界观 1 次 + 术语分块若干次）...")
            samples = sample_texts(need, n)
            raw = run_analysis(self.llm, samples, self.cfg)
            self.glossary = normalize_glossary(raw)
            self._save_glossary_auto()
            self._refresh_glossary_tree()
            n_terms = sum(len(v) for v in self.glossary["terms"].values())
            self._log(f"分析完成：术语库 {n_terms} 词条")
            self._log(f"世界观: {self.glossary.get('worldview', '')[:150]}")
            self._set_status(f"分析完成：{n_terms} 词条")
        except Exception as e:
            self._log(f"[错误] 分析失败: {e}")
            self._set_status("分析失败")

    # ================= 名词库 =================
    def _save_glossary_auto(self):
        base = os.path.splitext(os.path.basename(self.input_path))[0]
        tag = f"{base}_{self.file_hash}" if getattr(self, "file_hash", None) else base
        out_dir = paths.resolve_output_dir(self.cfg["pipeline"].get("output_dir"))
        p = os.path.join(out_dir, f"{tag}.glossary.json")
        save_glossary(self.glossary, p)
        self.glossary_path = p
        self._log(f"术语库已保存: {p}")

    def _load_glossary_file(self):
        p = filedialog.askopenfilename(title="选择术语库 JSON", filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            self.glossary = load_glossary(p)
            self.glossary_path = p
            self._refresh_glossary_tree()
            n = sum(len(v) for v in self.glossary.get("terms", {}).values())
            self._log(f"已加载术语库 {p}：{n} 词条")
        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    def _save_glossary_file(self):
        if not self.glossary:
            messagebox.showerror("错误", "没有术语库可保存")
            return
        self._sync_glossary_notes()
        p = filedialog.asksaveasfilename(
            title="保存术语库", defaultextension=".json",
            initialfile=os.path.basename(getattr(self, "glossary_path", "glossary.json")))
        if p:
            save_glossary(self.glossary, p)
            self.glossary_path = p
            self._log(f"术语库已保存: {p}")

    def _refresh_glossary_tree(self):
        self.gtree.delete(*self.gtree.get_children())
        if not self.glossary:
            return
        for cat, mapping in self.glossary.get("terms", {}).items():
            if isinstance(mapping, dict):
                for src, dst in mapping.items():
                    self.gtree.insert("", "end", values=(cat, src, dst))
        notes = self.glossary.get("worldview", "")
        tn = self.glossary.get("translation_notes", "")
        self.glossary_notes.delete("1.0", "end")
        self.glossary_notes.insert("1.0", f"【世界观】{notes}\n【翻译注意】{tn}")
        self.glossary_status.set(f"{sum(len(v) for v in self.glossary.get('terms', {}).values())} 词条")

    def _sync_glossary_notes(self):
        """把文本框里的世界观/翻译注意同步回 glossary。"""
        if not self.glossary:
            return
        txt = self.glossary_notes.get("1.0", "end").strip()
        worldview = ""
        notes = ""
        for line in txt.splitlines():
            if line.startswith("【世界观】"):
                worldview = line[len("【世界观】"):].strip()
            elif line.startswith("【翻译注意】"):
                notes = line[len("【翻译注意】"):].strip()
        if not worldview and not notes:
            # 整块作为世界观
            worldview = txt
        self.glossary["worldview"] = worldview
        self.glossary["translation_notes"] = notes

    def _glossary_add(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("新增词条")
        dlg.geometry("420x180")
        ttk.Label(dlg, text="分类:").pack(anchor="w", padx=8, pady=(8, 0))
        cat = ttk.Combobox(dlg, values=list(self.glossary.get("terms", {}).keys()) if self.glossary else [],
                           state="normal")
        cat.pack(fill="x", padx=8)
        ttk.Label(dlg, text="原文:").pack(anchor="w", padx=8, pady=(6, 0))
        src = ttk.Entry(dlg)
        src.pack(fill="x", padx=8)
        ttk.Label(dlg, text="译文:").pack(anchor="w", padx=8, pady=(6, 0))
        dst = ttk.Entry(dlg)
        dst.pack(fill="x", padx=8)

        def ok():
            c, s, d = cat.get().strip(), src.get().strip(), dst.get().strip()
            if not c or not s or not d:
                messagebox.showerror("错误", "三项都要填")
                return
            if "terms" not in self.glossary:
                self.glossary["terms"] = {}
            self.glossary["terms"].setdefault(c, {})[s] = d
            self._refresh_glossary_tree()
            self._log(f"新增词条 [{c}] {s} = {d}")
            dlg.destroy()

        ttk.Button(dlg, text="确定", command=ok).pack(pady=10)

    def _glossary_delete(self):
        sel = self.gtree.selection()
        if not sel:
            return
        for item in sel:
            cat, src, dst = self.gtree.item(item, "values")
            if cat in self.glossary.get("terms", {}):
                self.glossary["terms"][cat].pop(src, None)
        self._refresh_glossary_tree()
        self._log("已删除选中词条")

    def _glossary_edit_dialog(self, event=None):
        sel = self.gtree.selection()
        if not sel:
            return
        cat, src, dst = self.gtree.item(sel[0], "values")
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑词条")
        dlg.geometry("460x200")
        ttk.Label(dlg, text="分类:").pack(anchor="w", padx=8, pady=(8, 0))
        cat_e = ttk.Entry(dlg)
        cat_e.insert(0, cat)
        cat_e.pack(fill="x", padx=8)
        ttk.Label(dlg, text="原文:").pack(anchor="w", padx=8, pady=(6, 0))
        src_e = ttk.Entry(dlg)
        src_e.insert(0, src)
        src_e.pack(fill="x", padx=8)
        ttk.Label(dlg, text="译文:").pack(anchor="w", padx=8, pady=(6, 0))
        dst_e = ttk.Entry(dlg)
        dst_e.insert(0, dst)
        dst_e.pack(fill="x", padx=8)

        def ok():
            nc, ns, nd = cat_e.get().strip(), src_e.get().strip(), dst_e.get().strip()
            if not nc or not ns or not nd:
                return
            self.glossary["terms"][cat].pop(src, None)
            self.glossary["terms"].setdefault(nc, {})[ns] = nd
            self._refresh_glossary_tree()
            self._log(f"编辑词条 [{nc}] {ns} = {nd}")
            dlg.destroy()

        ttk.Button(dlg, text="确定", command=ok).pack(pady=10)

    def _apply_glossary(self):
        self._sync_glossary_notes()
        if not getattr(self, "glossary_path", None):
            self._save_glossary_auto()
        else:
            save_glossary(self.glossary, self.glossary_path)
        self._log("术语库已更新，后续翻译批次将使用新词条")

    # ================= 翻译 =================
    def _run_translate(self):
        if not self.data:
            self._load_stats()
            if not self.data:
                messagebox.showerror("错误", "请先选择并读取输入文件")
                return
        if not self.glossary:
            messagebox.showerror("错误", "请先运行分析或加载术语库")
            return
        if not hasattr(self, "llm"):
            self._make_llm()
        # 应用翻译选项：R18 风格 / 名词风格 / 自定义提示词
        self.cfg.setdefault("translation", {})["r18_style"] = self.r18_var.get()
        self.cfg["translation"]["name_style"] = self.name_style_var.get()
        self.cfg["translation"]["custom_prompt"] = self.custom_prompt_text.get("1.0", "end").strip()
        self.cfg["translation"]["polish"] = self.polish_var.get()
        try:
            self.cfg["translation"]["polish_threshold"] = int(self.polish_threshold_var.get() or 80)
        except ValueError:
            self.cfg["translation"]["polish_threshold"] = 80
        try:
            self.cfg["translation"]["context_window"] = int(self.ctx_win_var.get() or 3)
        except ValueError:
            self.cfg["translation"]["context_window"] = 3
        self.translator = Translator(self.llm, self.cfg)
        self.stop_flag = False
        self._log(f"开始翻译...（R18={self.cfg['translation']['r18_style']}，"
                  f"名词风格={self.cfg['translation']['name_style']}"
                  f"{'，精修' if self.cfg['translation']['polish'] else ''}）")
        self._set_status("翻译中...")
        self._worker_start(self._translate_worker)

    def _translate_worker(self):
        try:
            need = dict(self.groups["need_translate"])
            limit = int(self.limit_var.get() or 0)
            if limit > 0:
                need = dict(list(need.items())[:limit])
                self._log(f"测试模式：仅翻译前 {limit} 条")

            base = os.path.splitext(os.path.basename(self.input_path))[0]
            tag = f"{base}_{self.file_hash}" if getattr(self, "file_hash", None) else base
            out_dir = paths.resolve_output_dir(self.cfg["pipeline"].get("output_dir"))
            progress_path = os.path.join(out_dir, f"{tag}.progress.json")
            self.progress_path = progress_path
            self.log_path = os.path.join(out_dir, f"{tag}.translate.log")

            done = {}
            if os.path.isfile(progress_path):
                with open(progress_path, encoding="utf-8") as f:
                    done = json.load(f)
            self.translations = done

            def on_progress(completed, total, failed, err):
                if self.stop_flag:
                    raise KeyboardInterrupt("用户停止")
                if total > 0:
                    self.progress["maximum"] = total
                    self.progress["value"] = completed
                    pct = completed * 100 // total
                    status = f"翻译中 {pct}% ({completed}/{total})" + (f" 失败批:{failed}" if failed else "")
                    self._set_status(status)
                    # 同步写日志文件
                    try:
                        with open(self.log_path, "a", encoding="utf-8") as f:
                            f.write(f"[{time.strftime('%H:%M:%S')}] {status}\n")
                    except OSError:
                        pass

            translations = self.translator.run(need, self.glossary, progress_path,
                                               on_progress=on_progress,
                                               all_keys=list(self.data.keys()),
                                               context_window=self.cfg.get("translation", {}).get("context_window", 3))
            self.translations = translations
            self._log(f"翻译完成：{len(translations)} 条（含已有）")

            # 精修润色（可选）
            if self.cfg["translation"].get("polish"):
                self._log("开始精修润色（美化长句/诗歌/谜语）...")
                self._set_status("精修润色中...")
                n = self.translator.polish(
                    translations, self.glossary, self.cfg,
                    threshold=self.cfg["translation"].get("polish_threshold", 80),
                    on_progress=lambda c, t, fl, e: self._set_status(f"精修 {c}/{t}"))
                with open(progress_path, "w", encoding="utf-8") as f:
                    json.dump(translations, f, ensure_ascii=False, indent=2)
                self._log(f"精修完成：{n} 条已润色，结果已写回进度")

            self._log(f"token 用量: {self.llm.usage_report()}")
            self._set_status(f"翻译完成 {len(translations)} 条")
            self._review_refresh()
        except KeyboardInterrupt:
            self._log("[停止] 用户手动停止，进度已保存，可随时继续")
            self._set_status("已停止")
        except Exception as e:
            self._log(f"[错误] 翻译失败: {e}")
            self._set_status("翻译失败")

    # ================= 停止 =================
    def _stop(self):
        self.stop_flag = True
        self._log("请求停止（当前批次完成后退出）...")

    # ================= 导出 =================
    def _run_export(self):
        if not self.data or not self.groups:
            self._load_stats()
        if not self.data:
            messagebox.showerror("错误", "请先读取输入文件")
            return
        base = os.path.splitext(os.path.basename(self.input_path))[0]
        tag = f"{base}_{self.file_hash}" if getattr(self, "file_hash", None) else base
        out_dir = paths.resolve_output_dir(self.cfg["pipeline"].get("output_dir"))
        progress_path = os.path.join(out_dir, f"{tag}.progress.json")
        if os.path.isfile(progress_path):
            with open(progress_path, encoding="utf-8") as f:
                self.translations = json.load(f)
        out = filedialog.asksaveasfilename(
            title="导出翻译文件", defaultextension=".json",
            initialfile=f"{tag}.translated.json")
        if not out:
            return
        export_file(self.data, self.groups, self.translations, out)
        self._log(f"导出完成: {out}")
        self._set_status("导出完成")

    # ================= 译文修正 =================
    def _review_refresh(self):
        self.rtree.delete(*self.rtree.get_children())
        if not self.translations:
            return
        for src, dst in self.translations.items():
            self.rtree.insert("", "end", values=(src, dst))

    def _review_search(self):
        kw = self.search_var.get().strip()
        self.rtree.delete(*self.rtree.get_children())
        if not self.translations:
            return
        for src, dst in self.translations.items():
            if not kw or kw in src or kw in dst:
                self.rtree.insert("", "end", values=(src, dst))

    def _review_edit(self, event=None):
        sel = self.rtree.selection()
        if not sel:
            return
        src, dst = self.rtree.item(sel[0], "values")
        dlg = tk.Toplevel(self.root)
        dlg.title("修正译文")
        dlg.geometry("640x260")
        ttk.Label(dlg, text="原文:").pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(dlg, text=src, wraplength=600).pack(anchor="w", padx=8)
        ttk.Label(dlg, text="译文:").pack(anchor="w", padx=8, pady=(6, 0))
        txt = scrolledtext.ScrolledText(dlg, height=6)
        txt.insert("1.0", dst)
        txt.pack(fill="both", expand=True, padx=8, pady=4)

        def ok():
            new_dst = txt.get("1.0", "end").strip()
            self.translations[src] = new_dst
            self._save_progress_now()
            self._review_refresh()
            self._log(f"已修正译文: {src[:40]}... -> {new_dst[:40]}")
            dlg.destroy()

        ttk.Button(dlg, text="保存", command=ok).pack(pady=6)

    def _save_progress_now(self):
        if getattr(self, "progress_path", None) and os.path.isdir(os.path.dirname(self.progress_path)):
            tmp = self.progress_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.translations, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.progress_path)

    def _review_save(self):
        self._save_progress_now()
        self._log("译文修改已保存到进度文件")

    # ================= 线程 =================
    def _worker_start(self, fn):
        def wrapper():
            try:
                fn()
            finally:
                self.root.after(0, self._worker_done)
        t = threading.Thread(target=wrapper, daemon=True)
        self.worker = t
        t.start()

    def _worker_done(self):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0
        self._set_status("空闲")


def main():
    root = tk.Tk()
    TransItGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
