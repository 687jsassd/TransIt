#!/usr/bin/env python3
"""从 transit/__init__.py 的 __version__ 生成 PyInstaller 版本资源文件。

单一版本来源，避免 spec / 安装包 / 界面上的版本号各写一份而漂移。

用法：python tools/make_version_info.py [输出路径]
默认输出 build/version_info.txt
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_version() -> str:
    init = os.path.join(ROOT, "transit", "__init__.py")
    with open(init, encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
    if not m:
        raise SystemExit(f"在 {init} 中找不到 __version__")
    return m.group(1)


def as_tuple(v: str) -> tuple:
    parts = []
    for piece in v.split("."):
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


TEMPLATE = """\
# UTF-8
# 由 tools/make_version_info.py 自动生成，请勿手工编辑（改 transit/__init__.py 的 __version__）
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver},
    prodvers={ver},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'TransIt'),
        StringStruct('FileDescription', '{desc}'),
        StringStruct('FileVersion', '{verstr}'),
        StringStruct('InternalName', '{internal}'),
        StringStruct('LegalCopyright', 'TransIt'),
        StringStruct('OriginalFilename', '{internal}.exe'),
        StringStruct('ProductName', 'TransIt'),
        StringStruct('ProductVersion', '{verstr}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def main():
    version = read_version()
    out = (sys.argv[1] if len(sys.argv) > 1
           else os.path.join(ROOT, "build", "version_info.txt"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    # 两个 exe 共用同一版本资源文件（EXE 的 name 决定 OriginalFilename 之外的属性）
    with open(out, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.format(
            ver=as_tuple(version),
            verstr=version,
            desc="TransIt — Mtools 翻译文件 AI 精翻工作台",
            internal="TransIt",
        ))
    print(f"wrote {out} (version {version})")


if __name__ == "__main__":
    main()
