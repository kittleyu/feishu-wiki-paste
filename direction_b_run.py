#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
direction_b_run.py — 方向 B 端到端一键跑（飞书读 → 转 HTML → 写 CMS）
================================================================================
用法：
    # 仅读取+转换（不写 CMS），产出 converted.json 供预览
    python direction_b_run.py --node-token OfcbwcdeyicTKlkgS6fczBYhn4e \\
        --pkg-id 1331 --dry-convert

    # 读取+转换+写 CMS（先自动核对 corp_id）
    python direction_b_run.py --node-token OfcbwcdeyicTKlkgS6fczBYhn4e \\
        --pkg-id 1331 --corp-id 1155 --write

    # 先写 1 篇探路
    python direction_b_run.py --node-token ... --pkg-id 1331 --corp-id 1155 \\
        --write --preflight

依赖：
    - .env 里 FEISHU_APP_ID / FEISHU_APP_SECRET（飞书读取用）
    - Chrome 带 --remote-debugging-port 启动（CMS 写入用，见 launch_chrome_debug.py）
    - pip install requests playwright
"""
import os
import sys
import json
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    env = os.path.join(HERE, ".env")
    if os.path.isfile(env):
        with open(env, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def stage1_convert(space_id, node_token, out_json):
    """调用 skill 原生函数读取飞书并转 HTML，返回 (converted, ok, fail)。"""
    sys.path.insert(0, HERE)
    import paste_utils as skill
    print(">>> [阶段1] 获取飞书 token ...")
    token = skill.get_token()
    print(">>> [阶段1] 列出目录文章 ...")
    items = skill.get_wiki_articles(token, space_id, node_token, page_size=50)
    print(f"    子节点数: {len(items)}")
    print(">>> [阶段1] 读取文档块并转 HTML ...")
    results = skill.batch_wiki_to_cms(items, token, space_id, 0, 0, dry_run=False)
    ok = [r for r in results if "html" in r]
    fail = [r for r in results if "error" in r]
    payload = [{"title": r["title"], "content": r["html"].strip()}
               for r in ok if r.get("html", "").strip()]
    json.dump(payload, open(out_json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"    转换成功 {len(payload)} 篇, 失败 {len(fail)} 篇")
    print(f"    已存 {out_json}")
    return payload, ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--space-id", default="7630734017544981692")
    ap.add_argument("--node-token", required=True)
    ap.add_argument("--pkg-id", type=int, required=True)
    ap.add_argument("--corp-id", type=int, default=None)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--out", default="converted.json")
    ap.add_argument("--dry-convert", action="store_true",
                    help="只转换不写 CMS")
    ap.add_argument("--write", action="store_true", help="转换后写 CMS")
    ap.add_argument("--preflight", action="store_true", help="只写 1 篇探路")
    ap.add_argument("--auto-corp", action="store_true")
    args = ap.parse_args()

    payload, ok, fail = stage1_convert(args.space_id, args.node_token, args.out)

    if args.dry_convert or not args.write:
        print("✅ 仅转换完成（未写 CMS）。用 --write 写回 CMS。")
        return

    sys.path.insert(0, HERE)
    import direction_b_cms_write as cw
    cdp_url = f"http://127.0.0.1:{args.port}"
    p, browser, page = cw._connect_cdp(cdp_url)
    try:
        corp_id = args.corp_id
        if args.auto_corp or corp_id is None:
            cid = cw.verify_corp(page)
            if cid is None:
                print("❌ 无法自动识别 corp_id，请用 --corp-id 指定。")
                return
            corp_id = cid
        if args.preflight:
            res = cw.write_articles(page, payload, corp_id, args.pkg_id, limit=1)
            good, bad = cw.verify_written(page, res, corp_id, args.pkg_id)
            print(f"    探路: 成功 {len(good)} 失败 {len(bad)}")
            json.dump(res, open("cms_write_results.json", "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            return
        res = cw.write_articles(page, payload, corp_id, args.pkg_id)
        good, bad = cw.verify_written(page, res, corp_id, args.pkg_id)
        print(f"✅ 写入完成: 正确落库 {len(good)} 篇, 异常 {len(bad)} 篇")
        json.dump(res, open("cms_write_results.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    finally:
        browser.close()
        p.stop()


if __name__ == "__main__":
    load_env()
    main()
