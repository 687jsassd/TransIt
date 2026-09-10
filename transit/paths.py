"""资源目录与数据目录解析（源码运行 / PyInstaller 冻结 双模式）。

打包成 exe 后 `__file__` 不再指向项目目录：
- onedir  : 指向解包出的 `_internal/`（资源可读，但**不适合写用户数据**）
- onefile : 指向 `%TEMP%\\_MEIxxxx`，退出即删 —— 写到这里的数据会丢

因此本模块把两类目录彻底分开：

| 用途 | 源码运行 | 冻结运行 |
|------|----------|----------|
| RESOURCE_DIR 只读资源（web/ 等） | 项目根目录 | `sys._MEIPASS` |
| DATA_DIR 可写数据（config.json / output / uploads） | 项目根目录 | exe 所在目录 |

`TRANSIT_DATA_DIR` 环境变量可覆盖 DATA_DIR（便携版做数据隔离 / 测试用）。
"""
import os
import sys

#: 数据目录环境变量覆盖
ENV_DATA_DIR = "TRANSIT_DATA_DIR"


def is_frozen() -> bool:
    """是否运行在 PyInstaller(或同类) 冻结环境中。"""
    return bool(getattr(sys, "frozen", False))


def _source_root() -> str:
    """源码模式下的项目根目录（本文件的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """只读资源根目录（web/ 等静态资源所在处）。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return os.path.abspath(base)
        return os.path.dirname(os.path.abspath(sys.executable))
    return _source_root()


def data_dir() -> str:
    """可写数据根目录（config.json / output / uploads 所在处）。

    冻结后固定为 exe 所在目录（便携版：解压即用、数据跟着走）。
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return os.path.abspath(override)
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _source_root()


def resource_path(*parts: str) -> str:
    """拼接只读资源路径。"""
    return os.path.join(resource_dir(), *parts)


def data_path(*parts: str) -> str:
    """拼接可写数据路径。"""
    return os.path.join(data_dir(), *parts)


def default_config_path() -> str:
    """默认配置文件路径（数据目录下的 config.json）。"""
    return data_path("config.json")


def resolve_output_dir(out_dir: str = None) -> str:
    """把配置里的 output_dir 解析为绝对路径并建目录。

    相对路径一律锚定 DATA_DIR，不依赖进程 CWD —— 从快捷方式、任意工作目录
    启动 exe 时行为一致。
    """
    d = (out_dir or "output").strip() or "output"
    if not os.path.isabs(d):
        d = os.path.join(data_dir(), d)
    d = os.path.normpath(d)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_user_path(p: str) -> str:
    """把用户显式给出的路径解析为绝对路径。

    绝对路径原样返回；相对路径按 CWD 解析（符合命令行直觉），因为这是用户
    主动输入的目标，而非程序内部假设。
    """
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def ensure_data_dirs() -> None:
    """确保数据目录存在（uploads/ 与默认 output/）。"""
    os.makedirs(data_path("uploads"), exist_ok=True)
    resolve_output_dir("output")


def describe() -> str:
    """返回一行环境描述，便于排错时确认目录来源。"""
    mode = "frozen" if is_frozen() else "source"
    return (f"mode={mode} | RESOURCE_DIR={resource_dir()} | "
            f"DATA_DIR={data_dir()}")
