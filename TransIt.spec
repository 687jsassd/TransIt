# -*- mode: python ; coding: utf-8 -*-
"""TransIt 打包配置（PyInstaller >= 6）。

产出单目录便携版：两个 exe 共用同一个 `_internal`，体积几乎不增加。

    dist/TransIt/
      TransIt.exe       WebUI（窗口模式，双击即用，自动开浏览器）
      TransIt-CLI.exe   命令行（控制台模式，脚本/批处理用）
      _internal/        Python 运行时 + transit/ + web/

要点：
- onedir 而非 onefile：onefile 每次启动要解压到 %TEMP% 且退出即删，
  而本工具把 config.json / output / uploads 写在 exe 同目录（便携版语义），
  onefile 会导致用户数据丢失。
- 用户数据目录由 transit/paths.py 解析为 exe 所在目录，与 _internal 无关。
- 排除 tkinter（发布版以 WebUI 为唯一图形界面），顺带省掉 tcl/tk 约 5MB。
  注意不能排除 email/http/xml —— urllib.request 与 http.client 依赖 email.parser。

构建：pwsh -File build.ps1     （不要直接调 pyinstaller，见脚本内的前置步骤）
"""
import os

ROOT = os.path.abspath(SPECPATH)
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")
ICON = os.path.join(ROOT, "assets", "transit.ico")

# tkinter：发布版不含 tk 界面；unittest/pydoc 等纯开发期模块
EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "lib2to3",
    "distutils",
    "test",
    "sqlite3",
    "setuptools",
    "pip",
]

# 共享数据：静态前端。放到 _internal/web，两个 exe 都能通过 sys._MEIPASS 找到
DATAS = [(os.path.join(ROOT, "web"), "web")]

version_arg = VERSION_FILE if os.path.isfile(VERSION_FILE) else None
icon_arg = ICON if os.path.isfile(ICON) else None


def make_analysis(entry):
    return Analysis(
        [os.path.join(ROOT, entry)],
        pathex=[ROOT],
        binaries=[],
        datas=DATAS if entry == "webui.py" else [],  # 避免 COLLECT 出现重复条目
        hiddenimports=[],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=0,
    )


a_web = make_analysis("webui.py")
a_cli = make_analysis("transit_cli.py")

pyz_web = PYZ(a_web.pure)
pyz_cli = PYZ(a_cli.pure)

exe_web = EXE(
    pyz_web,
    a_web.scripts,
    [],
    exclude_binaries=True,
    name="TransIt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 会显著提高杀软误报率，体积收益不值得
    console=False,          # 窗口模式：双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
    version=version_arg,
)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="TransIt-CLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 控制台模式：管道/批处理可正常拿到输出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
    version=version_arg,
)

coll = COLLECT(
    exe_web,
    a_web.binaries,
    a_web.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TransIt",
)
