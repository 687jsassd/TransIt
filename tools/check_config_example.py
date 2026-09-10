#!/usr/bin/env python3
"""校验 config.example.json 与 transit/config.py 的 DEFAULT_CONFIG 一致、且不含密钥。

打包前置检查：模板与实际默认值漂移会让用户照着模板填却得到不同行为。
用法：python tools/check_config_example.py [config.example.json]
退出码 0 = 通过，1 = 不一致或含密钥。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "config.example.json")
    sys.path.insert(0, ROOT)

    if not os.path.isfile(path):
        print(f"[FAIL] 找不到 {path}")
        return 1

    with open(path, encoding="utf-8") as f:
        example = json.load(f)

    key = (example.get("api") or {}).get("api_key")
    if key:
        print("[FAIL] config.example.json 里含有 api_key，必须清空后再打包")
        return 1

    from transit.config import DEFAULT_CONFIG
    expect = json.loads(json.dumps(DEFAULT_CONFIG))
    expect["api"]["api_key"] = ""
    expect.setdefault("webui", {})["recent_files"] = []

    if example != expect:
        print("[FAIL] config.example.json 与 DEFAULT_CONFIG 不一致：")
        print(f"  期望: {json.dumps(expect, ensure_ascii=False, sort_keys=True)}")
        print(f"  实际: {json.dumps(example, ensure_ascii=False, sort_keys=True)}")
        print("  修复: python tools/make_config_example.py")
        return 1

    print("[ok] config.example.json 一致且不含密钥")
    return 0


if __name__ == "__main__":
    sys.exit(main())
