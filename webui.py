#!/usr/bin/env python3
"""TransIt WebUI — 浏览器图形界面（纯标准库，零依赖）。

架构：
- http.server ThreadingHTTPServer 提供 REST API + 静态页面（web/index.html）
- 复用 transit/ 核心模块（reader/analyzer/translator/writer/llm/config）
- 单任务模型：分析/翻译/精修 同一时间只跑一个，后台线程执行
- 前端轮询 /api/state + /api/logs 获取实时进度

目录策略见 transit.paths：只读资源（web/）与可写数据（config.json/output/uploads）
分开，打包成 exe 后数据落在 exe 同目录（便携版）。

安全：仅监听 127.0.0.1，并且所有 /api/* 需要启动时随机生成的 token
（URL 里携带一次，前端存入 sessionStorage 后走请求头），同时校验 Host/Origin，
防止其他网页对本地服务发起 CSRF / DNS 重绑定攻击。

启动：python webui.py [--port 8765] [--no-browser] [--print-token]
"""
import json
import os
import re
import secrets
import string
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from transit import paths
from transit import __version__
from transit.config import load_config, save_config, ensure_config_file
from transit.llm import LLMClient, LLMError
from transit.reader import load_mt_file, analyze_file, file_hash
from transit.analyzer import (
    sample_texts, run_analysis, normalize_glossary,
    save_glossary, load_glossary, TaskStopped, analysis_requests,
)
from transit.translator import (Translator, build_contextual_batches, pick_translation,
                                _merge_translations)
from transit.writer import export_file

CONFIG_PATH = paths.default_config_path()
WEB_DIR = paths.resource_path("web")
UPLOAD_DIR = paths.data_path("uploads")
RECENT_LIMIT = 8

#: 可选的本地接口访问令牌（仅当设置 TRANSIT_API_TOKEN 时启用；默认 None = 不校验）
API_TOKEN = {"value": None}

#: 当前 HTTP 服务器实例（由 _serve 填充），供退出接口与看门狗使用
SERVER = {"inst": None}


#: 「无人连接时自动退出」默认秒数（0 = 不自动退出）
DEFAULT_IDLE_EXIT_SECONDS = 600
#: 界面上设置的最小值 —— 太小会让程序在用户还没打开页面时就退出
MIN_IDLE_EXIT_SECONDS = 30


def resolve_idle_timeout(cfg: dict, override=None) -> float:
    """解析「无人连接时自动退出」的秒数。0 表示不自动退出。

    override 来自命令行（--exit-when-idle），不受界面最小值限制，便于测试。
    """
    if override is not None:
        try:
            return max(0.0, float(override))
        except (TypeError, ValueError):
            pass
    raw = (cfg.get("webui") or {}).get("exit_when_idle_seconds",
                                      DEFAULT_IDLE_EXIT_SECONDS)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_IDLE_EXIT_SECONDS)
    if v <= 0:
        return 0.0
    return max(float(MIN_IDLE_EXIT_SECONDS), v)


def request_shutdown(delay: float = 0.3) -> None:
    """安排退出：先让当前响应写回浏览器，再关服务器。

    BaseHTTPRequestHandler 的 shutdown() 必须在 serve_forever() 之外的线程调用，
    而请求处理本身就跑在独立线程里，这里再起一个线程是为了确保响应已经 flush。
    """
    def run():
        time.sleep(delay)
        srv = SERVER.get("inst")
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def _idle_watchdog(timeout: float) -> None:
    """无人连接时自动退出。

    为什么需要：打包成 exe 后是窗口模式（刻意不要黑色控制台窗口），关掉浏览器后
    进程仍在后台，用户就只能靠任务管理器结束它。这里以「浏览器最后一次请求」为
    心跳——页面每 1.5 秒轮询一次，关掉标签页后心跳停止，超时即退出。
    有任务在跑时不退出：翻译/分析可能还要跑很久，而且进度是持续落盘的，
    贸然退出会让用户以为任务丢了。
    """
    while True:
        time.sleep(5)
        srv = SERVER.get("inst")
        if srv is None:
            return
        with STATE.lock:
            running = STATE.task.get("status") == "running"
        if running:
            continue
        idle = time.time() - STATE.last_request
        if idle >= timeout:
            STATE.log(f"已 {int(idle)} 秒没有浏览器连接，自动退出"
                      f"（不需要这个行为可在设置里把「无人连接时自动退出」设为 0）")
            request_shutdown(0.0)
            return


# ---------------- 全局状态 ----------------
class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = load_config(CONFIG_PATH)
        self.input_path = None
        self.data = None
        self.groups = None
        self.src_hash = None
        self.paths = None
        self.glossary = None
        self.glossary_path = None
        self.translations = {}
        self.task = {
            "type": None, "status": "idle", "detail": "",
            "done": 0, "total": 0, "failed": 0,
            "started_at": None, "ended_at": None, "error": None,
        }
        self.stop_event = threading.Event()
        self.thread = None
        self.logs = deque(maxlen=800)
        self.log_seq = 0
        self.progress_samples = []  # (t, done) 用于 ETA/速度
        # 最近一次收到浏览器请求的时间 —— 用于「无人连接时自动退出」
        self.last_request = time.time()

    def touch(self):
        """标记「浏览器还活着」。每个 HTTP 请求都会调用。"""
        self.last_request = time.time()

    # ---------- 日志 ----------
    def log(self, msg, level="info"):
        with self.lock:
            self.log_seq += 1
            ts = time.strftime("%H:%M:%S")
            self.logs.append({"seq": self.log_seq, "ts": ts, "level": level, "msg": str(msg)})

    def logs_since(self, since: int):
        with self.lock:
            return [e for e in self.logs if e["seq"] > since]

    # ---------- 任务管理 ----------
    def start_task(self, ttype: str, target, detail: str = ""):
        with self.lock:
            if self.task["status"] == "running":
                return False, "已有任务在运行"
            self.stop_event.clear()
            self.task = {
                "type": ttype, "status": "running", "detail": detail,
                "done": 0, "total": 0, "failed": 0,
                "started_at": time.time(), "ended_at": None, "error": None,
            }
            self.progress_samples = [(time.time(), 0)]
            th = threading.Thread(target=self._task_wrapper, args=(target,), daemon=True)
            self.thread = th
        th.start()
        return True, ""

    def _task_wrapper(self, target):
        try:
            target()
            with self.lock:
                if self.task["status"] == "running":
                    self.task["status"] = "done"
                self.task["ended_at"] = time.time()
            self.log(f"任务完成：{self.task['type']}")
        except TaskStopped:
            with self.lock:
                self.task["status"] = "stopped"
                self.task["ended_at"] = time.time()
            self.log("任务已被用户停止", "warn")
        except Exception as e:
            with self.lock:
                self.task["status"] = "error"
                self.task["error"] = str(e)
                self.task["ended_at"] = time.time()
            self.log(f"任务失败：{e}", "error")

    def request_stop(self):
        self.stop_event.set()

    def update_progress(self, done, total, failed, err=None):
        with self.lock:
            self.task["done"] = done
            self.task["total"] = total
            self.task["failed"] = failed
            if err:
                self.task["detail"] = f"最近失败: {str(err)[:120]}"
            self.progress_samples.append((time.time(), done))
            if len(self.progress_samples) > 60:
                self.progress_samples = self.progress_samples[-60:]

    def speed_eta(self):
        """(条/分钟, 预计剩余秒)"""
        with self.lock:
            samples = self.progress_samples
            total = self.task.get("total") or 0
        if len(samples) < 2 or not total:
            return 0.0, None
        (t0, d0), (t1, d1) = samples[0], samples[-1]
        dt = t1 - t0
        if dt <= 0 or d1 <= d0:
            return 0.0, None
        rate = (d1 - d0) / dt * 60.0
        remain = max(0, total - d1)
        return round(rate, 1), round(remain / rate * 60.0) if rate > 0 else None


STATE = AppState()


# ---------------- 输出路径 ----------------
def make_paths(cfg: dict, input_path: str, src_hash: str):
    # 相对 output_dir 锚定 DATA_DIR（exe 同目录），不受进程 CWD 影响
    out_dir = paths.resolve_output_dir(cfg["pipeline"].get("output_dir"))
    base = os.path.splitext(os.path.basename(input_path))[0]
    tag = f"{base}_{src_hash}" if src_hash else base
    return {
        "glossary": os.path.join(out_dir, f"{tag}.glossary.json"),
        "progress": os.path.join(out_dir, f"{tag}.progress.json"),
        "output": os.path.join(out_dir, f"{tag}.translated.json"),
        "log": os.path.join(out_dir, f"{tag}.translate.log"),
    }


def file_stats_payload(st: AppState):
    with st.lock:
        if not st.data:
            return {"loaded": False}
        g = st.groups
        has_progress = 0
        if st.paths and os.path.isfile(st.paths["progress"]):
            try:
                with open(st.paths["progress"], encoding="utf-8") as f:
                    has_progress = len(json.load(f))
            except Exception:
                has_progress = 0
        return {
            "loaded": True,
            "path": st.input_path,
            "hash": st.src_hash,
            "total": len(st.data),
            "passthrough": len(g["passthrough"]),
            "translated": len(g["translated"]),
            "need": len(g["need_translate"]),
            "has_progress": has_progress,
            "paths": st.paths,
        }


def glossary_payload(st: AppState):
    with st.lock:
        g = st.glossary
        path = st.glossary_path
    if not g:
        # 尝试从已有路径加载
        return {"exists": False, "terms": {}, "categories": []}
    terms = g.get("terms", {})
    return {
        "exists": True,
        "path": path,
        "worldview": g.get("worldview", ""),
        "translation_notes": g.get("translation_notes", ""),
        "game_context": g.get("game_context", {}),
        # 风格圣经的新字段：让用户能看到分析到底产出了什么（并可编辑回存）
        "naming_policy": g.get("naming_policy", ""),
        "register_guide": g.get("register_guide", {}) or {},
        "characters": g.get("characters", []) or [],
        "term_conflicts": g.get("term_conflicts", []) or [],
        # 自动剔除的非术语（整句/片段/幻觉）—— 让用户知道有东西被丢掉了，可追溯
        "term_pruned": g.get("term_pruned", []) or [],
        "terms": terms,
        "categories": list(terms.keys()),
        "terms_count": sum(len(v) for v in terms.values() if isinstance(v, dict)),
    }


def state_payload():
    st = STATE
    with st.lock:
        task = dict(st.task)
        cfg = json.loads(json.dumps(st.cfg))  # deep copy
        recent = cfg.get("webui", {}).get("recent_files", [])
        tokens = {
            "requests": getattr(st, "_llm_requests", 0),
        }
    rate, eta = st.speed_eta()
    with st.lock:
        llm = getattr(st, "last_llm", None)
    if llm is not None:
        tokens = {
            "requests": llm.total_requests,
            "prompt": llm.total_prompt_tokens,
            "completion": llm.total_completion_tokens,
            "total": llm.total_prompt_tokens + llm.total_completion_tokens,
        }
    else:
        tokens = {"requests": 0, "prompt": 0, "completion": 0, "total": 0}
    task["speed"] = rate
    task["eta_sec"] = eta
    return {
        "file": file_stats_payload(st),
        "task": task,
        "glossary": glossary_payload(st),
        "config": cfg,
        "tokens": tokens,
        "log_cursor": st.log_seq,
        "output_exists": bool(st.paths and os.path.isfile(st.paths["output"])),
        "recent": [{"path": p, "exists": os.path.isfile(p),
                    "name": os.path.basename(p)} for p in recent],
    }


# ---------------- 任务实现 ----------------
def _make_llm(st: AppState) -> LLMClient:
    llm = LLMClient(**st.cfg["api"])
    with st.lock:
        st.last_llm = llm
    return llm


# ---------------- 工程（输入文件）身份守卫 ----------------
class ProjectChanged(RuntimeError):
    """任务执行期间用户切换了输入文件 —— 结果不能再写回。"""


def _snapshot_project(st):
    """把当前「工程」的身份快照下来：hash + 输出路径 + 术语库路径。

    任务必须用**启动时**的快照，不能在跑完之后再读 st.paths/st.src_hash ——
    否则用户在分析 A 的过程中载入 B，A 的术语库就会写进 B 的文件，
    同时内存里的 glossary 是 A 的、data 是 B 的，表现为「术语库变成别的工程的内容」。
    """
    with st.lock:
        return {
            "hash": st.src_hash,
            "paths": dict(st.paths) if st.paths else None,
            "glossary": st.glossary,
            "data_keys": list(st.data.keys()) if st.data else [],
        }


def _assert_same_project(st, snap, what="任务"):
    """写回前校验工程没变；变了就抛错并**不写任何文件**。"""
    with st.lock:
        cur = st.src_hash
    if cur != snap["hash"]:
        raise ProjectChanged(
            f"{what}期间输入文件已被切换（{snap['hash']} → {cur}），"
            f"本次结果已丢弃，以免写入错误工程。请重新运行。")


def task_analyze():
    st = STATE
    if not st.data:
        raise RuntimeError("请先加载输入文件")
    snap = _snapshot_project(st)
    need = st.groups["need_translate"]
    llm = _make_llm(st)
    p = st.cfg["pipeline"]
    samples = sample_texts(need, st.cfg)
    total_steps = analysis_requests(len(samples))
    st.log(f"采样 {len(samples)}/{len(need)} 条"
           f"（待译条数的 {float(p.get('sample_ratio', 0.1)) * 100:.0f}%，"
           f"限制在 {p.get('sample_min')}~{p.get('sample_max')} 条）")
    st.log(f"分三阶段分析：世界观与风格 → 角色表 → 术语提取（共约 {total_steps} 次请求）")
    st.update_progress(0, total_steps, 0)
    done_steps = [0]

    def step_cb():
        done_steps[0] += 1
        st.update_progress(done_steps[0], total_steps, 0)

    raw = run_analysis(llm, samples, st.cfg, stop_check=st.stop_event.is_set,
                       step_cb=step_cb, corpus=list(need.keys()))
    glossary = normalize_glossary(raw)
    _assert_same_project(st, snap, "分析")
    paths = snap["paths"]
    save_glossary(glossary, paths["glossary"])
    with st.lock:
        st.glossary = glossary
        st.glossary_path = paths["glossary"]
    n_terms = sum(len(v) for v in glossary["terms"].values())
    n_chars = len(glossary.get("characters") or [])
    st.log(f"分析完成：术语库 {n_terms} 词条 | 角色表 {n_chars} 人 -> {paths['glossary']}")
    st.log(f"世界观: {glossary.get('worldview', '')[:160]}")
    pruned = glossary.get("term_pruned") or []
    if pruned:
        st.log(f"已自动剔除 {len(pruned)} 条非术语（整句/片段/全文不存在），"
               f"详见 glossary.json 的 term_pruned", "warn")
    conflicts = glossary.get("term_conflicts") or []
    if conflicts:
        st.log(f"发现 {len(conflicts)} 处译法分歧 —— 到「术语库」页可直接点选采用哪个",
               "warn")
    st.update_progress(total_steps, total_steps, 0)


def task_retranslate_selected(srcs, hint=""):
    """批量重译选中的条目。

    为什么需要：批量翻译里偶有顽固条目（多行长文本居多）失败，逐条点「重译」太累。
    这里把选中的条目按**小批**送去重译 —— 批比正常翻译更小，因为顽固条目基本是
    多行长文本，小批（尤其单条）下模型更容易逐条认真处理；整批里仍失败的条目
    会再单独试一次。
    """
    st = STATE
    if not st.data:
        raise RuntimeError("请先加载输入文件")
    snap = _snapshot_project(st)
    glossary = st.glossary
    if not glossary:
        raise RuntimeError("没有术语库，请先运行分析")
    llm = _make_llm(st)
    tr = Translator(llm, st.cfg)
    all_keys = list(snap["data_keys"])
    with st.lock:
        known = set(st.data.keys())
    todo = [s for s in srcs if s in known]
    if not todo:
        raise RuntimeError("没有选中任何可重译的条目")

    cfg_bs = int(st.cfg.get("pipeline", {}).get("batch_size", 12) or 12)
    bs = max(1, min(cfg_bs, 4))
    window = int(st.cfg.get("translation", {}).get("context_window", 3) or 0)
    instruction = (hint or "").strip() or None
    total = len(todo)
    st.log(f"批量重译：{total} 条（批大小 {bs}"
           + (f"，附加要求：{instruction[:40]}" if instruction else "") + "）")
    st.update_progress(0, total, 0)

    ok_n = 0
    fail_n = 0

    def _ctx(keys):
        return (build_contextual_batches(keys, all_keys, len(keys), window)[0][1]
                if window > 0 else None)

    def _store(mapping):
        with st.lock:
            for k, v in mapping.items():
                st.translations[k] = v
            Translator._save_progress(st.translations, snap["paths"]["progress"])

    for i in range(0, total, bs):
        if st.stop_event.is_set():
            raise TaskStopped("用户停止了任务")
        # 每个小批写回前都校验工程没变（切换已被接口层挡住，这里是兜底）
        _assert_same_project(st, snap, "批量重译")
        chunk = todo[i:i + bs]
        try:
            result = tr.translate_batch(chunk, glossary, i // bs, _ctx(chunk),
                                        extra_instruction=instruction)
        except (LLMError, OSError, ValueError) as e:
            st.log(f"重译第 {i // bs + 1} 批失败（{len(chunk)} 条）：{e}", "warn")
            result = {}
        out = {}
        _merge_translations(chunk, result, out, [])
        if out:
            _store(out)
            ok_n += len(out)
        for k in [c for c in chunk if c not in out]:
            # 整批里没匹配上的再单独试一次 —— 单条最容易成功
            if st.stop_event.is_set():
                raise TaskStopped("用户停止了任务")
            try:
                one = tr.translate_batch([k], glossary, 0, _ctx([k]),
                                         extra_instruction=instruction)
                single = pick_translation(one, k)
            except (LLMError, OSError, ValueError):
                single = ""
            if single:
                _store({k: single})
                ok_n += 1
            else:
                fail_n += 1
                st.log(f"仍无法重译：{k[:50]}", "warn")
        st.update_progress(ok_n + fail_n, total, fail_n)

    st.log(f"批量重译完成：成功 {ok_n}/{total} 条"
           + (f"，仍失败 {fail_n} 条（可在弹窗里附上修改要求再单独重试）" if fail_n else ""))
    st.update_progress(total, total, fail_n)


def st_log(msg, level="info"):
    STATE.log(msg, level)


def task_translate(limit: int = 0, do_polish: bool = False):
    st = STATE
    if not st.data:
        raise RuntimeError("请先加载输入文件")
    snap = _snapshot_project(st)
    llm = _make_llm(st)
    glossary = st.glossary
    if not glossary:
        raise RuntimeError("没有术语库，请先运行分析（或导入术语库）")
    need = dict(st.groups["need_translate"])
    if limit and limit > 0:
        keys = list(need.keys())[:limit]
        need = {k: need[k] for k in keys}
        st.log(f"测试模式：只翻译前 {limit} 条")
    paths = snap["paths"]
    all_keys = list(snap["data_keys"])
    tr = Translator(llm, st.cfg)
    tcfg = st.cfg.get("translation", {})
    ctx_win = int(tcfg.get("context_window", 3) or 0)
    st.log(f"开始翻译：待译 {len(need)} 条 | 模型 {st.cfg['api']['model']} | "
           f"上下文行 {ctx_win} | R18 {tcfg.get('r18_style', 'explicit')}")

    def on_progress(done, total, failed, err):
        st.update_progress(done, total, failed, err)
        if done and done % 120 == 0:
            rate, eta = st.speed_eta()
            eta_s = f"，预计剩余 {eta // 60} 分 {eta % 60:02d} 秒" if eta else ""
            st.log(f"进度 {done}/{total}（{rate:.0f} 条/分{eta_s}）")

    result = tr.run(
        need, glossary, paths["progress"],
        on_progress=on_progress,
        all_keys=all_keys,
        context_window=ctx_win,
        stop_check=st.stop_event.is_set,
    )
    _assert_same_project(st, snap, "翻译")
    with st.lock:
        st.translations = result
    st.log(f"翻译完成：共 {len(result)} 条译文。{llm.usage_report()}")
    st.update_progress(len(result), len(need), 0)

    if do_polish:
        threshold = int(tcfg.get("polish_threshold", 80) or 80)
        st.log(f"开始精修润色（阈值 {threshold} 字符）...")
        st.task["type"] = "polish"
        st.task["detail"] = "精修润色中"

        def on_polish(done, total, failed, err):
            st.update_progress(done, total, failed, err)

        tr.polish(result, glossary, st.cfg, threshold=threshold,
                  on_progress=on_polish)
        Translator._save_progress(result, paths["progress"])
        st.log("精修完成，进度已保存")


def task_polish_all():
    """对已有全部译文做精修（不重新翻译）。"""
    st = STATE
    if not st.data:
        raise RuntimeError("请先加载输入文件")
    snap = _snapshot_project(st)
    llm = _make_llm(st)
    glossary = st.glossary
    if not glossary:
        raise RuntimeError("没有术语库")
    with st.lock:
        translations = dict(st.translations)
    if not translations:
        # 从进度文件载入
        if snap["paths"] and os.path.isfile(snap["paths"]["progress"]):
            with open(snap["paths"]["progress"], encoding="utf-8") as f:
                translations = json.load(f)
    if not translations:
        raise RuntimeError("没有可精修的译文，请先翻译")
    tr = Translator(llm, st.cfg)
    threshold = int(st.cfg.get("translation", {}).get("polish_threshold", 80) or 80)
    st.log(f"精修已有译文（阈值 {threshold} 字符）...")

    def on_polish(done, total, failed, err):
        st.update_progress(done, total, failed, err)

    n = tr.polish(translations, glossary, st.cfg, threshold=threshold,
                  on_progress=on_polish)
    _assert_same_project(st, snap, "精修")
    with st.lock:
        st.translations.update(translations)
    Translator._save_progress(translations, snap["paths"]["progress"])
    st.log(f"精修完成：润色 {n} 条，进度已保存")


def task_export():
    st = STATE
    if not st.data:
        raise RuntimeError("请先加载输入文件")
    snap = _snapshot_project(st)
    with st.lock:
        translations = dict(st.translations)
    # 若内存为空，从进度文件恢复
    if not translations and snap["paths"] and os.path.isfile(snap["paths"]["progress"]):
        with open(snap["paths"]["progress"], encoding="utf-8") as f:
            translations = json.load(f)
        with st.lock:
            st.translations = translations
    out = export_file(st.data, st.groups, translations, snap["paths"]["output"])
    st.log(f"导出完成：{snap['paths']['output']}（共 {len(out)} 条）")


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TransItWebUI/1.0"
    # BaseHTTPRequestHandler 默认 HTTP/1.0（每请求一连接）。前端在持续轮询
    # /api/state 与 /api/logs，用 1.1 的 keep-alive 可避免大量 TIME_WAIT 与重复握手。
    # 前提是每条响应都带 Content-Length —— _json 与 _static 都设了，故安全。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志（避免刷屏）

    # ---------- 本地接口防护 ----------
    # 只监听 127.0.0.1 并不足够：任意网页都能向 localhost 发「简单请求」
    # （text/plain 的 POST 不触发 CORS 预检），从而盲写 /api/config、/api/translate，
    # 或调用 /api/browse 列目录。真正管用的是下面两道，而且用户完全无感：
    #
    #   1) Host 白名单 —— 阻断 DNS 重绑定（攻击者域名解析到 127.0.0.1 后，
    #      页面发出的请求 Host 仍是攻击者域名，浏览器无法伪造该头）
    #   2) Origin 白名单 —— 阻断 CSRF（跨站 fetch / 表单提交都会带 Origin，
    #      同样无法被页面 JS 伪造），配合响应头 X-Frame-Options 阻断点击劫持
    #
    # 经此两道，跨站页面既发不出通过校验的请求，也读不到响应（无 CORS 头）。
    # 令牌（--token / TRANSIT_API_TOKEN）作为**可选**加固保留，默认关闭 ——
    # 因为它对上述攻击没有额外价值，却会带来「链接一变就永久 403」的使用陷阱。
    def _local_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _host_origin_reason(self):
        """Host + Origin 校验（这两道不需要任何令牌，本地页面永远能过）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._local_hosts():
            return f"Host 不被允许：{host!r}（仅接受本机地址）"
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return f"跨站来源被拒绝：{origin}"
        return None

    def _token_reason(self):
        """可选的令牌校验（仅当用户显式启用时生效）。"""
        expected = API_TOKEN.get("value")
        if not expected:
            return None
        supplied = (self.headers.get("X-TransIt-Token")
                    or (parse_qs(urlsplit(self.path).query).get("token") or [""])[0])
        try:
            if supplied and secrets.compare_digest(supplied, expected):
                return None
        except TypeError:  # 非 ASCII 输入
            pass
        return ("已启用访问令牌（--token / TRANSIT_API_TOKEN），但请求未携带或不匹配。"
                "请用程序启动时自动打开的链接访问；若浏览器复用了旧标签页，"
                "请关闭该标签页后重新启动程序。")

    def _deny_reason(self):
        """返回拒绝原因；None 表示放行。原因会回给客户端，便于排错。"""
        return self._host_origin_reason() or self._token_reason()

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

    # ---------- 基础 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _static(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        # commonpath 而非 startswith：后者会让 web_x/ 这类同前缀目录穿透
        try:
            inside = os.path.commonpath([path, WEB_DIR]) == os.path.normpath(WEB_DIR)
        except ValueError:  # 不同盘符
            inside = False
        if not inside or not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------
    def do_GET(self):
        try:
            STATE.touch()   # 心跳：有请求就说明浏览器还在
            route = urlsplit(self.path).path
            # 静态页面不设防：它本身不含任何数据，且必须能被加载才能谈其它
            if route == "/" or route.startswith("/index"):
                self._static("index.html")
                return
            if not route.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return
            # 探活端点：只做 Host/Origin 校验，不要求令牌。
            # 供启动时判断「端口上是不是本版本的 TransIt」——若指向旧版本实例，
            # 它既没有这个端点又会因缺令牌而 403，于是会被正确判定为不可复用。
            if route == "/api/ping":
                reason = self._host_origin_reason()
                if reason:
                    self._json({"error": reason}, 403)
                    return
                self._json({"app": "TransIt", "version": __version__,
                            "token_required": bool(API_TOKEN.get("value"))})
                return
            reason = self._deny_reason()
            if reason:
                self._json({"error": reason}, 403)
                return
            if route.startswith("/api/state"):
                self._json(state_payload())
            elif route.startswith("/api/logs"):
                q = self._parse_qs()
                since = int(q.get("since", ["0"])[0])
                self._json({"logs": STATE.logs_since(since), "cursor": STATE.log_seq})
            elif route.startswith("/api/glossary"):
                self._json(glossary_payload(STATE))
            elif route.startswith("/api/recent"):
                self._json(self.api_recent())
            elif route.startswith("/api/entries"):
                self._json(self.api_entries())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _parse_qs(self):
        return parse_qs(urlsplit(self.path).query)

    # ---------- POST ----------
    def do_POST(self):
        try:
            STATE.touch()   # 心跳：有请求就说明浏览器还在
            reason = self._deny_reason()
            if reason:
                self._json({"error": reason}, 403)
                return
            body = self._read_body()
            route = urlsplit(self.path).path
            if route == "/api/file":
                self.api_load_file(body)
            elif route == "/api/upload":
                self.api_upload(body)
            elif route == "/api/browse":
                self._json(self.api_browse(body))
            elif route == "/api/config":
                self.api_save_config(body)
            elif route == "/api/analyze":
                ok, msg = STATE.start_task("analyze", task_analyze, "分析世界观与建库")
                self._json({"ok": ok, "error": msg}, 200 if ok else 409)
            elif route == "/api/translate":
                limit = int(body.get("limit", 0) or 0)
                polish = bool(body.get("polish", False))
                ok, msg = STATE.start_task(
                    "translate", lambda: task_translate(limit, polish), "批量精翻")
                self._json({"ok": ok, "error": msg}, 200 if ok else 409)
            elif route == "/api/polish":
                ok, msg = STATE.start_task("polish", task_polish_all, "精修润色")
                self._json({"ok": ok, "error": msg}, 200 if ok else 409)
            elif route == "/api/export":
                ok, msg = STATE.start_task("export", task_export, "导出成品")
                self._json({"ok": ok, "error": msg}, 200 if ok else 409)
            elif route == "/api/stop":
                STATE.request_stop()
                self._json({"ok": True})
            elif route == "/api/shutdown":
                # 先回答再退出，避免浏览器看到连接被重置
                self._json({"ok": True, "message": "TransIt 即将退出，可以关闭本页了"})
                STATE.log("收到退出请求，正在关闭…")
                request_shutdown()
            elif route == "/api/glossary":
                self.api_save_glossary(body)
            elif route == "/api/review":
                self.api_review_edit(body)
            elif route == "/api/retranslate":
                self.api_retranslate(body)
            elif route == "/api/retranslate_selected":
                self.api_retranslate_selected(body)
            elif route == "/api/test":
                self.api_test_llm()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ---------- API 实现 ----------
    def _do_load(self, p: str):
        """加载文件到状态（共享逻辑：路径加载与上传共用）。"""
        if not os.path.isfile(p):
            return {"error": f"文件不存在: {p}"}
        st = STATE
        try:
            data = load_mt_file(p)
            groups = analyze_file(data)
        except Exception as e:
            return {"error": f"读取失败: {e}"}
        with st.lock:
            # 清空上一文件状态（防串数据）
            st.data = data
            st.groups = groups
            st.input_path = p
            st.src_hash = file_hash(p)
            st.paths = make_paths(st.cfg, p, st.src_hash)
            st.glossary = None
            st.glossary_path = None
            st.translations = {}
            # 自动加载已有术语库
            if os.path.isfile(st.paths["glossary"]):
                try:
                    st.glossary = load_glossary(st.paths["glossary"])
                    st.glossary_path = st.paths["glossary"]
                except Exception:
                    pass
            # 自动加载已有翻译进度（断点续传）
            if os.path.isfile(st.paths["progress"]):
                try:
                    with open(st.paths["progress"], encoding="utf-8") as f:
                        st.translations = json.load(f)
                except Exception:
                    st.translations = {}
            # 记录最近文件（persist 到 config.json 的 webui 段）
            self._push_recent(p)
        st.log(f"已加载 {p}（hash {st.src_hash}），输出按此隔离")
        if st.translations:
            st.log(f"发现已有翻译进度 {len(st.translations)} 条，续跑将跳过已完成部分")
        return None

    def _push_recent(self, p: str):
        st = STATE
        with st.lock:
            cfg = st.cfg
            recent = cfg.setdefault("webui", {}).setdefault("recent_files", [])
            if p in recent:
                recent.remove(p)
            recent.insert(0, p)
            del recent[RECENT_LIMIT:]
            self._save_cfg_locked(cfg)

    @staticmethod
    def _save_cfg_locked(cfg: dict):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _busy_reason(self):
        """有任务在跑时不允许切换工程 —— 否则任务的写回会落到新工程上。

        这一层是「防呆」；任务内部还有 _assert_same_project 兜底，
        两道加起来才能保证不会把 A 的术语库写成 B 的。
        """
        with STATE.lock:
            if STATE.task.get("status") == "running":
                t = STATE.task.get("type") or "任务"
                return (f"有任务正在运行（{t}），切换文件会让它的结果写错地方。"
                        f"请先点「停止」或等它跑完再切换。")
        return None

    def api_load_file(self, body):
        p = (body.get("path") or "").strip().strip('"')
        if not p:
            self._json({"error": "路径为空"}, 400)
            return
        busy = self._busy_reason()
        if busy:
            self._json({"error": busy}, 409)
            return
        err = self._do_load(p)
        if err:
            self._json(err, 400)
            return
        self._json({"ok": True, "file": file_stats_payload(STATE)})

    def api_upload(self, body):
        """上传文件内容（浏览器 <input type=file> / 拖拽），落到 uploads/ 后加载。"""
        busy = self._busy_reason()
        if busy:
            self._json({"error": busy}, 409)
            return
        name = (body.get("name") or "uploaded.json").strip()
        name = os.path.basename(name) or "uploaded.json"
        content = body.get("content")
        if content is None:
            self._json({"error": "内容为空"}, 400)
            return
        if not isinstance(content, str) or len(content.encode("utf-8")) > 200 * 1024 * 1024:
            self._json({"error": "文件过大（>200MB）或格式错误"}, 400)
            return
        # 校验为合法 JSON 对象（提前拒绝乱文件）
        try:
            obj = json.loads(content)
            if not isinstance(obj, dict):
                raise ValueError("顶层必须是 JSON 对象")
        except Exception as e:
            self._json({"error": f"不是有效的翻译 JSON: {e}"}, 400)
            return
        # 内容 hash 命名落盘（同名不同内容不冲突；与输出隔离 hash 一致）
        import hashlib
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        p = os.path.join(UPLOAD_DIR, f"{os.path.splitext(name)[0]}_{digest}.json")
        if not os.path.isfile(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        STATE.log(f"已接收上传文件 {name}（{len(obj)} 条）-> {p}")
        err = self._do_load(p)
        if err:
            self._json(err, 400)
            return
        self._json({"ok": True, "file": file_stats_payload(STATE),
                    "stored_path": p, "name": name})

    def api_recent(self):
        with STATE.lock:
            recent = STATE.cfg.get("webui", {}).get("recent_files", [])
            items = []
            for p in recent:
                items.append({"path": p, "exists": os.path.isfile(p),
                              "name": os.path.basename(p)})
        return {"recent": items}

    def api_browse(self, body):
        cur = (body.get("path") or "").strip()
        # Windows 盘符列表
        if not cur or cur in ("此电脑", "/", "\\", "drives"):
            drives = [f"{L}:\\" for L in string.ascii_uppercase
                      if os.path.exists(f"{L}:\\")]
            return {"cwd": "此电脑", "parent": None, "dirs": drives, "files": []}
        cur = os.path.normpath(cur)
        if not os.path.isdir(cur):
            cur = os.path.dirname(cur) or "\\"
        dirs, files = [], []
        try:
            for name in sorted(os.listdir(cur)):
                full = os.path.join(cur, name)
                if os.path.isdir(full):
                    dirs.append(name + ("（空）" if not os.listdir(full) else ""))
                elif name.lower().endswith(".json"):
                    try:
                        size = os.path.getsize(full)
                        files.append({"name": name, "size": size})
                    except OSError:
                        pass
        except PermissionError:
            return {"cwd": cur, "parent": os.path.dirname(cur), "dirs": [], "files": [],
                    "error": "无权限访问该目录"}
        parent = os.path.dirname(cur)
        return {"cwd": cur, "parent": parent if parent != cur else None,
                "dirs": dirs, "files": files}

    def api_save_config(self, body):
        st = STATE
        with st.lock:
            cfg = st.cfg
            if "api" in body and isinstance(body["api"], dict):
                for k in ("base_url", "api_key", "model", "thinking",
                          "temperature", "max_tokens", "timeout"):
                    if k in body["api"]:
                        v = body["api"][k]
                        if k in ("temperature",):
                            v = float(v or 0.2)
                        elif k in ("max_tokens", "timeout"):
                            v = int(v or 8192) if k == "max_tokens" else int(v or 180)
                        elif k == "thinking":
                            v = bool(v)
                        cfg["api"][k] = v
            if "pipeline" in body and isinstance(body["pipeline"], dict):
                for k in ("concurrency", "batch_size", "max_retries",
                          "sample_ratio", "sample_min", "sample_max", "output_dir"):
                    if k in body["pipeline"]:
                        v = body["pipeline"][k]
                        if k == "output_dir":
                            cfg["pipeline"][k] = str(v or "output")
                        elif k == "sample_ratio":
                            # 比例用小数（0.1 = 10%），容错处理百分数写法
                            try:
                                r = float(v)
                            except (TypeError, ValueError):
                                r = 0.1
                            if r > 1:
                                r = r / 100.0
                            cfg["pipeline"][k] = min(max(r, 0.0), 1.0)
                        else:
                            cfg["pipeline"][k] = int(v or 1)
            if "webui" in body and isinstance(body["webui"], dict):
                w = cfg.setdefault("webui", {})
                if "exit_when_idle_seconds" in body["webui"]:
                    try:
                        iv = float(body["webui"]["exit_when_idle_seconds"])
                    except (TypeError, ValueError):
                        iv = DEFAULT_IDLE_EXIT_SECONDS
                    # 0 表示不自动退出；正数不得小于最小值，否则页面还没打开就退出了
                    if iv > 0:
                        iv = max(float(MIN_IDLE_EXIT_SECONDS), iv)
                    w["exit_when_idle_seconds"] = iv
            if "translation" in body and isinstance(body["translation"], dict):
                t = cfg.setdefault("translation", {})
                for k in ("r18_style", "name_style", "context_window",
                          "custom_prompt", "polish", "polish_threshold"):
                    if k in body["translation"]:
                        v = body["translation"][k]
                        if k in ("context_window", "polish_threshold"):
                            v = int(v or 0)
                        elif k == "polish":
                            v = bool(v)
                        else:
                            v = str(v or "")
                        t[k] = v
            save_config(cfg, CONFIG_PATH)
        st.log("配置已保存")
        self._json({"ok": True})

    def api_save_glossary(self, body):
        g = body.get("glossary")
        if not isinstance(g, dict):
            self._json({"error": "glossary 格式错误"}, 400)
            return
        st = STATE
        with st.lock:
            old = st.glossary or {}
        # 以「客户端实际提交的字段」为准做合并：未提交的字段沿用原值。
        # 不能先 normalize 再 setdefault —— normalize 会给缺失字段填默认空值
        # （characters=[]、register_guide={}），setdefault 就再也救不回来了。
        merged = dict(old)
        merged.update({k: v for k, v in g.items() if v is not None})
        norm = normalize_glossary(merged)
        with st.lock:
            st.glossary = norm
            path = st.paths["glossary"] if st.paths else paths.data_path(
                "output", "glossary.manual.json")
            save_glossary(norm, path)
            st.glossary_path = path
        n_chars = len(norm.get("characters") or [])
        st.log(f"术语库已保存：{sum(len(v) for v in norm['terms'].values())} 词条"
               + (f" | 角色表 {n_chars} 人" if n_chars else ""))
        self._json({"ok": True, "terms_count": sum(
            len(v) for v in norm["terms"].values() if isinstance(v, dict)),
            "characters_count": n_chars})

    def api_review_edit(self, body):
        src = body.get("src")
        dst = body.get("dst")
        if not src or dst is None:
            self._json({"error": "参数缺失"}, 400)
            return
        st = STATE
        with st.lock:
            if not st.paths:
                self._json({"error": "未加载文件"}, 400)
                return
            progress = st.paths["progress"]
            st.translations[src] = str(dst)
            Translator._save_progress(st.translations, progress)
        self._json({"ok": True, "src": src, "dst": str(dst)})

    def api_entries(self):
        st = STATE
        q = self._parse_qs()
        query = (q.get("q", [""])[0] or "").strip()
        page = max(1, int(q.get("page", ["1"])[0] or 1))
        page_size = min(500, max(10, int(q.get("page_size", ["100"])[0] or 100)))
        only_untranslated = q.get("untranslated", ["0"])[0] == "1"
        # 排除「原样保留」条目（ID/数字/代码/纯西文，本来就不需要翻译）
        exclude_passthrough = q.get("exclude_passthrough", ["0"])[0] == "1"
        cat_filter = (q.get("cat", [""])[0] or "").strip()

        with st.lock:
            translations = dict(st.translations)
            data = st.data
            groups = st.groups
        passthrough = set(groups["passthrough"]) if groups else set()
        pretranslated = set(groups["translated"]) if groups else set()

        rows = []
        stats = {"passthrough": 0, "pretranslated": 0, "need": 0, "untranslated": 0}
        if data:
            for k, v in data.items():
                if k in passthrough:
                    cat = "passthrough"
                elif k in pretranslated:
                    cat = "pretranslated"
                else:
                    cat = "need"
                cur = translations.get(k, "")
                stats[cat] += 1
                untranslated = (cat == "need" and not cur)
                if untranslated:
                    stats["untranslated"] += 1

                # 搜索要同时覆盖原文与**当前译文**——原来只比对了原始文件的值，
                # 导致按中文译文搜索永远搜不到
                if query and query not in k and query not in str(cur) and query not in str(v):
                    continue
                if exclude_passthrough and cat == "passthrough":
                    continue
                if cat_filter and cat != cat_filter:
                    continue
                # 「只看未翻译」只对「待译」类有意义：原样保留条目永远不会有译文，
                # 若把它们算作未翻译，这个筛选就永远被噪声淹没
                if only_untranslated and not untranslated:
                    continue
                rows.append({"src": k, "cur": cur, "cat": cat,
                             "orig": str(v) if v != k else ""})

        total = len(rows)
        start = (page - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size,
                "stats": stats,
                "rows": rows[start:start + page_size]}

    def api_retranslate(self, body):
        """单条重译：只对指定条目请求一次翻译，可附带本次修改要求。

        比"整轮重跑"实用得多——发现某句译得不好时，不必重译上千条。
        """
        src = (body.get("src") or "").strip()
        if not src:
            self._json({"error": "缺少 src"}, 400)
            return
        st = STATE
        with st.lock:
            if not st.data:
                self._json({"error": "未加载文件"}, 400)
                return
            if src not in st.data:
                self._json({"error": "该条目不在当前文件中"}, 400)
                return
            if not st.glossary:
                self._json({"error": "没有术语库，请先运行分析"}, 400)
                return
            snap = _snapshot_project(st)
            all_keys = snap["data_keys"]
            glossary = st.glossary
            cfg = st.cfg
        hint = str(body.get("hint") or "")[:1500]
        current = str(body.get("current") or "")

        t0 = time.time()
        try:
            llm = _make_llm(st)
            tr = Translator(llm, cfg)
            window = int(cfg.get("translation", {}).get("context_window", 3) or 0)
            batches = build_contextual_batches([src], all_keys, 1, window) if window > 0 \
                else [([src], None)]
            _, context = batches[0]
            # 已有译文时把它作为"要改进的旧译"告诉模型
            instruction = ""
            if current.strip():
                instruction += f"上一版译文是：「{current}」，请给出更好的版本。"
            if hint.strip():
                instruction += f"\n本次修改要求：{hint}"
            # compact=True：单条重译时固定上下文会占掉 99% 的 token
            # （角色表一项就占 59%），裁剪后实测提示词小 68%、响应明显更快。
            # max_tokens 也收紧：一条译文不需要配置里那个很大的输出上限。
            result = tr.translate_batch([src], glossary, 0, context,
                                        extra_instruction=instruction or None,
                                        compact=True, max_tokens=2048)
        except (LLMError, OSError, ValueError) as e:
            self._json({"error": f"重译失败（耗时 {time.time() - t0:.1f}s）：{e}"}, 500)
            return
        elapsed = time.time() - t0

        new = pick_translation(result, src)
        if not new:
            self._json({"error": f"模型没有返回该条目的译文（耗时 {elapsed:.1f}s），"
                                 f"请重试或换个要求"}, 502)
            return
        _assert_same_project(st, snap, "重译")
        with st.lock:
            st.translations[src] = new
            Translator._save_progress(st.translations, snap["paths"]["progress"])
        st.log(f"单条重译（{elapsed:.1f}s）：{src[:40]} -> {new[:40]}")
        self._json({"ok": True, "src": src, "dst": new,
                    "elapsed": round(elapsed, 1),
                    "tokens": llm.total_prompt_tokens + llm.total_completion_tokens})

    def api_retranslate_selected(self, body):
        """批量重译选中的条目（后台任务，进度走既有任务面板）。"""
        raw = body.get("srcs")
        if not isinstance(raw, list) or not raw:
            self._json({"error": "没有选中任何条目"}, 400)
            return
        st = STATE
        with st.lock:
            if not st.data:
                self._json({"error": "未加载文件"}, 400)
                return
            known = set(st.data.keys())
        srcs = [str(s) for s in raw if str(s) in known]
        if not srcs:
            self._json({"error": "选中的条目都不在当前文件中"}, 400)
            return
        if len(srcs) > 2000:
            self._json({"error": f"一次最多重译 2000 条（当前 {len(srcs)} 条）"}, 400)
            return
        hint = str(body.get("hint") or "")[:1500]
        ok, msg = STATE.start_task(
            "retranslate", lambda: task_retranslate_selected(srcs, hint),
            f"批量重译 {len(srcs)} 条")
        self._json({"ok": ok, "error": msg, "count": len(srcs)},
                   200 if ok else 409)

    def api_test_llm(self):
        st = STATE
        try:
            llm = _make_llm(st)
            t0 = time.time()
            reply = llm.chat_text("回复两个字：连接成功"[:20])
            dt = time.time() - t0
            self._json({"ok": True, "reply": reply[:50], "latency": round(dt, 1),
                        "usage": llm.usage_report()})
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:300]})


# ---------------- 入口 ----------------
def _report_fatal(text: str) -> None:
    """窗口模式（--noconsole）没有控制台：把启动失败写日志并弹对话框。

    打包版不能依赖 tkinter（已被 exclude），所以直接用 Win32 MessageBox。
    """
    log_path = paths.data_path("transit-error.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        log_path = "(无法写入日志)"
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"{text}\n\n详细信息已写入：\n{log_path}",
            "TransIt 启动失败", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _probe_existing_instance(port: int, timeout: float = 0.8):
    """探测该端口上是否已有**本版本**的 TransIt WebUI 在跑。

    返回 "ours" / "other" / None。用 /api/ping 而不是静态页做判据：静态页是从磁盘
    读的，新旧版本内容可能一致；而 /api/ping 由服务端代码回答，旧版本实例没有这个
    端点（或缺令牌会 403），因此能被正确识别为「不是可复用的实例」——避免把用户
    引导到一个仍在要求令牌的旧服务上。
    """
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:{port}/api/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            info = json.loads(resp.read(2048).decode("utf-8"))
    except urllib.error.HTTPError:
        # 端口上确实有服务，但不是本版本的 TransIt（或需要令牌）
        return "other"
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if info.get("app") == "TransIt" and info.get("version") == __version__:
        return "ours"
    return "other"


def _open_browser(url: str) -> None:
    """打开浏览器；失败时把地址告诉用户（窗口模式没有控制台，必须弹出来）。"""
    def run():
        try:
            ok = webbrowser.open(url)
        except Exception:
            ok = False
        if not ok:
            STATE.log(f"未能自动打开浏览器，请手动访问：{url}", "warn")
            _report_fatal(f"未能自动打开浏览器。\n\n请手动在浏览器中打开：\n{url}\n\n"
                          f"（若浏览器已打开旧页面，请先关闭它再刷新）")

    threading.Timer(0.6, run).start()


def main(argv=None):
    # 非中文 Windows 上 stdout 被重定向时默认用 cp1252，打印中文会崩
    from transit import enable_utf8_stdio
    enable_utf8_stdio()

    port = 8765
    no_browser = False
    idle_override = None
    args = list(sys.argv[1:] if argv is None else argv)
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--no-browser":
            no_browser = True
            i += 1
        elif args[i] == "--exit-when-idle" and i + 1 < len(args):
            # 无人连接多少秒后自动退出；0 = 不自动退出
            idle_override = float(args[i + 1])
            i += 2
        else:
            i += 1

    # pythonw / --noconsole 下 sys.stdout 为 None，兜底（核心模块的 print 会被丢弃，
    # 但 WebUI 的进度与日志都走 AppState.log，不受影响）
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    try:
        return _serve(port, no_browser, idle_override)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _report_fatal("TransIt 启动失败：\n\n" + traceback.format_exc())
        raise


def _serve(port: int, no_browser: bool, idle_override=None) -> int:
    # 已经有一个**本版本**实例在跑（用户重复双击）→ 直接复用，不再起第二个
    stale_ports = []
    for p in range(port, port + 10):
        found = _probe_existing_instance(p)
        if found == "ours":
            url = f"http://127.0.0.1:{p}/"
            print(f"[webui] 检测到已在运行的 TransIt 实例，复用端口 {p}：{url}")
            if not no_browser:
                _open_browser(url)
            else:
                print(f"[webui] 请访问 {url}")
            return 0
        if found == "other":
            stale_ports.append(p)
    if stale_ports:
        msg = (f"端口 {', '.join(map(str, stale_ports))} 上已有别的程序或旧版本 TransIt 在运行，"
               f"将改用其他端口。若那是旧版本，请先把它关闭，否则会同时存在两个服务。")
        print(f"[webui] {msg}")

    # 首次运行（尤其是便携版）自动生成 config.json 与数据目录
    cfg_path, created = ensure_config_file(CONFIG_PATH)
    paths.ensure_data_dirs()
    if created:
        STATE.log(f"已生成默认配置：{cfg_path}（请到「设置」页填入 API Key）")
    for note in STATE.cfg.get("_notices", []):
        STATE.log(note, "warn")

    # 可选加固：默认不启用令牌（Host + Origin + X-Frame-Options 已足够，
    # 且令牌会让「浏览器复用旧标签页」变成永久 403 的死局）
    API_TOKEN["value"] = os.environ.get("TRANSIT_API_TOKEN") or None

    server = None
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        msg = f"端口 {port}~{port + 9} 均被占用，无法启动本地服务。"
        print(f"[webui] {msg}", file=sys.stderr)
        _report_fatal(msg + "\n请关闭占用端口的程序，或用 --port 指定其他端口。")
        return 1

    url = f"http://127.0.0.1:{port}/"
    if API_TOKEN["value"]:
        # 令牌走 URL 片段：不进服务端日志 / Referer
        url += f"#token={API_TOKEN['value']}"

    SERVER["inst"] = server
    idle_timeout = resolve_idle_timeout(STATE.cfg, idle_override)
    if idle_timeout > 0:
        threading.Thread(target=_idle_watchdog, args=(idle_timeout,), daemon=True).start()

    STATE.log(f"TransIt WebUI 已启动：http://127.0.0.1:{port}/")
    STATE.log(f"数据目录：{paths.data_dir()}")
    STATE.log(f"配置文件：{CONFIG_PATH}{'（本次新建）' if created else ''}")
    if API_TOKEN["value"]:
        STATE.log("已启用访问令牌（TRANSIT_API_TOKEN）")
    if idle_timeout > 0:
        STATE.log(f"关掉浏览器 {int(idle_timeout // 60)} 分钟后将自动退出"
                  f"（界面右上角「退出程序」可立即关闭；设置里可关闭此行为）")
    else:
        STATE.log("已关闭「无人连接时自动退出」，请用界面右上角「退出程序」关闭")
    print(f"[webui] TransIt WebUI 运行中: {url}  (Ctrl+C 退出)")
    print(f"[webui] {paths.describe()}")
    if idle_timeout > 0:
        print(f"[webui] 关掉浏览器 {int(idle_timeout // 60)} 分钟后自动退出；"
              f"或点界面右上角「退出程序」")
    if not no_browser:
        _open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.log("TransIt 已退出")
        SERVER["inst"] = None
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
