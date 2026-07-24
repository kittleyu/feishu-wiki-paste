#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_direction_b.py — 方向 B 通用驱动（飞书 Wiki → 约牛/某司 CMS 词包）

用法：
    # 仅读取 + 统计（不写 CMS），确认篇数与标题
    python run_direction_b.py --node <飞书目录token> --company 约牛 --pkg 约牛软件 --dry

    # 读取 + 转换 + 写 CMS 词包（先探路 1 篇，再全量写 + 逐篇校验）
    python run_direction_b.py --node <飞书目录token> --company 约牛 --pkg 约牛软件

说明：
    - 复用 prepare_multi.feishu_collect 读飞书并转 HTML
    - 复用 auto_paste.run_pipeline 做 登录→反查 corp/pkg→探路→全量写→校验
    - company/pkg 为 CMS「公司全名 / 词包名」的模糊匹配串
    - Chrome 由 ensure_chrome() 在沙箱自起并自动登录（HYH_PWD 来自 .env）
"""
import os
import sys
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True, help="源飞书 Wiki 目录 node_token")
    ap.add_argument("--company", required=True, help="CMS 公司全名（模糊匹配），如 约牛")
    ap.add_argument("--pkg", required=True, help="CMS 词包名（模糊匹配），如 约牛软件")
    ap.add_argument("--dry", action="store_true", help="仅读取统计，不写 CMS")
    ap.add_argument("--out", default="bot_articles.json")
    args = ap.parse_args()

    from prepare_multi import load_env, feishu_collect
    import auto_paste as ap_mod

    load_env()  # 填充 HYH_PWD 等环境变量，供 auto_paste 自动登录

    out_path = os.path.join(HERE, args.out)
    print(f">>> [阶段1] 读取飞书目录 {args.node} ...", flush=True)
    articles = feishu_collect(args.node, None, out_path)
    if not articles:
        print("❌ 没读到任何文章，请检查目录链接是否正确 / 是否有权限", flush=True)
        return
    print(f"✅ 读取 {len(articles)} 篇：", flush=True)
    for i, a in enumerate(articles[:30], 1):
        print(f"    {i:>3}. {a.get('title','')[:50]}", flush=True)
    if len(articles) > 30:
        print(f"    ... 共 {len(articles)} 篇", flush=True)

    if args.dry:
        print(f"\n✅ 仅统计完成（未写 CMS）。文章已存 {out_path}", flush=True)
        return

    print(f"\n>>> [阶段2] 写 CMS：公司={args.company} 词包={args.pkg}", flush=True)
    res = ap_mod.run_pipeline(args.company, args.pkg, out_path,
                              os.path.join(HERE, "bot_cms_results.json"))
    print(">>> 结果:", res, flush=True)
    if res and res.get("bad", 0) == 0:
        print(f"🎉 完成！{res['ok']} 篇已粘贴到「{args.company} / {args.pkg}」", flush=True)
    elif res:
        print(f"⚠️ 完成但有异常：成功 {res['ok']} / 异常 {res['bad']}", flush=True)
    else:
        print("❌ 未成功完成，请查看日志", flush=True)


if __name__ == "__main__":
    main()
