#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cms_discover_ids.py — 通过 CMS API 自动查出「公司 corp_id」和「词包 pkg_id」
================================================================================
不用再盲猜 corp_id，也不用点界面。依赖已登录 huiyouhua 且带调试端口的 Chrome。

原理（2026-07 实测）：
  - GET /yunying/v1/corp/active               → 列出当前账号可访问的所有公司 {id,name}
  - GET /yunying/v1/auth/changecorp?corp_id=X → 切换当前公司为 X（返回"切换企业成功"）
  - GET /yunying/v1/keyword/package?page=1&page_size=10000 → 当前公司的词包列表
    注意：该接口只返回【当前公司】的词包，所以要先 changecorp 到目标公司再查。

用法：
    python cms_discover_ids.py --company "成都川蜀血管病医院有限公司" --pkg "成都静脉曲张医院"
    # 只查公司不查词包：
    python cms_discover_ids.py --company "某某公司"
    # 若公司已切对，只查词包：
    python cms_discover_ids.py --pkg "成都静脉曲张医院"

输出（JSON 到 stdout，便于脚本解析）：
    {"corp_id": 3041, "pkg_id": 5538, "corp_name": "...", "pkg_desc": "..."}
"""
import os
import sys
import json
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CDP_URL = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", help="公司全名（模糊匹配）")
    ap.add_argument("--pkg", help="词包名/描述（模糊匹配）")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    cdp = f"http://127.0.0.1:{args.port}"
    p = sync_playwright().start()
    b = p.chromium.connect_over_cdp(cdp)
    ctx = b.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if "huiyouhua" not in page.url:
        page.goto("https://yunying.huiyouhua.com/cms-yunying.html?tab=articles",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

    out = {"corp_id": None, "pkg_id": None}

    if args.company:
        corps = page.evaluate(
            "(async()=>{var r=await fetch('/yunying/v1/corp/active',{method:'GET'});"
            "var d=await r.json();return d.data.corps;})()")
        corp = None
        for c in corps:
            if args.company in (c.get("name") or ""):
                corp = c
                break
        if not corp:
            print("❌ 未找到公司:", args.company, file=sys.stderr)
            print(f"   可访问公司数: {len(corps)}", file=sys.stderr)
            b.close(); p.stop(); sys.exit(1)
        out["corp_id"] = corp["id"]
        out["corp_name"] = corp["name"]
        print(f"✅ 公司: {corp['name']}  corp_id={corp['id']}")
        # 切到该公司，才能查到它的词包
        page.evaluate(
            f"(async()=>{{await fetch('/yunying/v1/auth/changecorp?corp_id={corp['id']}',"
            f"{{method:'GET'}});}})()")
        page.wait_for_timeout(600)

    if args.pkg:
        if out["corp_id"] is None:
            # 不确定当前公司，先读当前
            cur = page.evaluate(
                "(async()=>{var r=await fetch('/yunying/v1/user/current?platform=win',"
                "{method:'GET'});var d=await r.json();return d.data.corp_id;})()")
            out["corp_id"] = cur
        pkgs = page.evaluate(
            "(async()=>{var r=await fetch('/yunying/v1/keyword/package?page=1&page_size=10000',"
            "{method:'GET'});var d=await r.json();return d.data.keyword_packages;})()")
        pkg = None
        for k in pkgs:
            desc = k.get("description") or ""
            name = k.get("name") or ""
            if args.pkg in desc or args.pkg in name:
                pkg = k
                break
        if not pkg:
            print(f"❌ 当前公司(corp_id={out['corp_id']})下未找到词包: {args.pkg}",
                  file=sys.stderr)
            b.close(); p.stop(); sys.exit(1)
        out["pkg_id"] = pkg["id"]
        out["pkg_desc"] = pkg.get("description")
        print(f"✅ 词包: {pkg.get('description')}  pkg_id={pkg['id']}")

    print(json.dumps(out, ensure_ascii=False))
    b.close()
    p.stop()


if __name__ == "__main__":
    main()
