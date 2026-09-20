"""配置加载/保存：config.json + 环境变量覆盖。

路径策略见 `transit.paths`：默认配置固定在 DATA_DIR（源码=项目根，冻结=exe 同目录），
不受进程 CWD 影响。
"""
import json
import os
import tempfile

from . import paths

DEFAULT_CONFIG = {
    "api": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "thinking": False,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout": 180,
    },
    "pipeline": {
        "concurrency": 6,
        "batch_size": 12,
        "max_retries": 4,
        # 分析采样量 = 待译条数 × sample_ratio，再夹到 [sample_min, sample_max]。
        # 旧版固定条数的 sample_size 已废弃（见 load_config 里的迁移处理）。
        "sample_ratio": 0.10,
        "sample_min": 200,
        "sample_max": 1000,
        "output_dir": "output",
    },
    "language": {
        "source": "ja",
        "target": "zh-CN",
        "target_note": "简体中文，游戏本地化风格，自然口语化",
    },
    "translation": {
        "r18_style": "explicit",
        "name_style": "auto",
        "context_window": 3,
        "custom_prompt": "",
        "polish": False,
        "polish_threshold": 80,
    },
    "webui": {
        # 关掉浏览器多少秒后自动退出（0 = 不自动退出，只能靠界面上的「退出程序」）
        # 打包版没有控制台窗口，不自动退出的话用户只能用任务管理器结束进程
        "exit_when_idle_seconds": 600,
    },
}


def load_config(path: str = None) -> dict:
    """加载配置。path 为 None 时用 DATA_DIR/config.json。

    文件不存在或字段缺失时用默认值兜底。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = path or paths.default_config_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                _deep_merge(cfg, user_cfg)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # 配置损坏不应导致程序无法启动：退回默认值，由调用方提示
            pass
    # 迁移：旧版 pipeline.sample_size（固定采样条数）已由
    # sample_ratio + sample_min/max 取代。这里必须删掉它，
    # 否则老配置里的 sample_size 会一直存在、让人以为新采样策略没生效。
    # 注意：_notices 是每次加载现算的派生字段，必须先清掉——否则它被存进
    # config.json 后会在下次加载时被当成用户配置合并回来，逐次累积重复提示。
    cfg.pop("_notices", None)
    legacy = cfg.get("pipeline", {}).pop("sample_size", None)
    if legacy is not None:
        cfg.setdefault("_notices", []).append(
            f"配置里的 pipeline.sample_size={legacy} 已废弃（新版按待译条数的比例采样），"
            f"已忽略；如需固定条数请调整 sample_ratio / sample_min / sample_max")
    # 环境变量覆盖（便于 CI / 不落盘 / 便携版免配置）
    if os.environ.get("TRANSIT_API_KEY"):
        cfg["api"]["api_key"] = os.environ["TRANSIT_API_KEY"]
    if os.environ.get("TRANSIT_BASE_URL"):
        cfg["api"]["base_url"] = os.environ["TRANSIT_BASE_URL"]
    if os.environ.get("TRANSIT_MODEL"):
        cfg["api"]["model"] = os.environ["TRANSIT_MODEL"]
    return cfg


def save_config(cfg: dict, path: str = None) -> str:
    """原子写入配置（先写临时文件再替换，避免中断产生半截 JSON）。"""
    path = path or paths.default_config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # _notices 是加载时现算的派生字段，不写盘（否则会累积重复提示）
    payload = {k: v for k, v in cfg.items() if k != "_notices"}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def ensure_config_file(path: str = None) -> tuple:
    """确保配置文件存在。返回 (路径, 是否新建)。

    首次运行（尤其是便携版）自动落一份带注释字段的配置，用户可直接编辑。
    """
    path = path or paths.default_config_path()
    if os.path.isfile(path):
        return path, False
    save_config(json.loads(json.dumps(DEFAULT_CONFIG)), path)
    return path, True


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
