#!/usr/bin/env python3
"""从 transit/config.py 的 DEFAULT_CONFIG 重新生成 config.example.json。

模板必须与代码默认值保持同步（build.ps1 会用 tools/check_config_example.py 校验）。
用法：python tools/make_config_example.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    sys.path.insert(0, ROOT)
    from transit.config import DEFAULT_CONFIG

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["api"]["api_key"] = ""          # 模板绝不含密钥
    cfg.setdefault("webui", {})["recent_files"] = []

    out = os.path.join(ROOT, "config.example.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
