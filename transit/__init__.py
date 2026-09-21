"""TransIt — Mtools 式翻译文件的 AI 精翻流水线（OpenAI 兼容 API）。

流水线: 读取 -> 分类 -> 风格圣经分析(世界观/角色/术语) -> 批量精翻 -> 导出
纯标准库实现，零第三方依赖。
"""
import sys

__version__ = "1.3.1"


def enable_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8（各入口启动时第一件事就调用）。

    为什么需要：Windows 上当 stdout 被重定向（管道 / 文件 / CI）时，Python 不再走
    控制台宽字符 API，而是按系统 ANSI 代码页编码 —— 英文系统是 cp1252，编码不了中文，
    于是任何带中文的输出（`TransIt-CLI.exe --help` 的帮助文本、进度行）都会直接抛
    UnicodeEncodeError 把程序打崩。中文系统是 cp936 能编码中文，所以这个坑只在
    非中文 Windows 上暴露。

    交互式控制台上 Python 本来就用 UTF-8（走 WriteConsoleW），因此这里是空操作。
    另外 PyInstaller 打包后的解释器不会应用 PYTHONIOENCODING，只能靠代码里改，
    这也是必须在入口处调用而不是靠环境变量的原因。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 已被替换成非 TextIOWrapper，或流已关闭 —— 忽略即可
            pass
