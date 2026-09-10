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
        "sample_size": 120,
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
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
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
