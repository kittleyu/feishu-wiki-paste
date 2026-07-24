#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_client.py — 方向 A 通用驱动（基于 client_map.json）

用法：
  python run_client.py <公司名> [数量] [--subdir 7.22] [--dry-run]
  python run_client.py 永安期货 10
  python run_client.py 中泰期货 20 --subdir 7.22

行为：
  1. 从 client_map.json 读取该公司「顶层目录 node」+「审核表 node」
  2. 在顶层目录下建/复用子目录（默认当日日期 M.D，可用 --subdir 覆盖）
  3. 拉 CMS 最新 N 篇 → 写入该子目录
  4. 把链接回填审核表（B 列链接，A 列合并写日期「M月D日」）

依赖：.env 含 HYH_USER/HYH_PWD（huiyouhua 自动登录）。
说明：飞书机器人（feishu_bot.py）的方向 A 走的就是同一套 client_map + 流程；
      本脚本用于「在对话里/本地」直接驱动，无需 @机器人。
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_map():
    with open(os.path.join(HERE, "client_map.json"), encoding="utf-8") as f:
        return json.load(f)


def today_subdir():
    d = datetime.date.today()
    return f"{d.month}.{d.day}"


def subdir_to_datelabel(s):
    for sep in (".", "/"):
        if sep in s:
            m, d = s.split(sep, 1)
            return f"{m}月{d}日"
    return s + "日"


def main():
    ap = argparse.ArgumentParser(description="方向A：CMS最新N篇→飞书目录(建日期子目录)→填审核表")
    ap.add_argument("company", help="client_map.json 里的公司名，如 永安期货")
    ap.add_argument("limit", nargs="?", default=10, type=int, help="最新几篇，默认 10")
    ap.add_argument("--subdir", default=None, help="子目录名，默认当日日期 M.D（如 7.22）")
    ap.add_argument("--audit-status", type=int, default=None,
                    help="按审核状态筛选：1=已审核通过；-1=审核驳回；不填=全部（CMS 无独立待审核态）")
    ap.add_argument("--dry-run", action="store_true", help="只预览转换，不写飞书/不填表")
    args = ap.parse_args()

    cmap = load_map()
    if args.company not in cmap:
        print(f"❌ client_map.json 中找不到「{args.company}」")
        print("   已有客户：" + " / ".join(cmap.keys()))
        sys.exit(1)
    entry = cmap[args.company]
    dir_node = entry["dir_node"]
    sheet_node = entry.get("sheet_node")

    subdir = args.subdir or today_subdir()
    st_hint = {1: "已审核通过", -1: "审核驳回"}.get(args.audit_status,
                 "全部状态" if args.audit_status is None else f"audit_status={args.audit_status}")
    print(f">>> 公司={args.company}  数量={args.limit}  子目录={subdir}  审核状态={st_hint}  "
          f"目录={dir_node}  审核表={sheet_node or '(未配置)'}")

    # 1) 粘贴到飞书（建/复用子目录）
    from direction_a_cms_to_feishu import run_pipeline_a
    res = run_pipeline_a(
        args.company, dir_node, args.limit,
        subdir=subdir,
        out_file=os.path.join(HERE, "bot_direction_a_results.json"),
        dry_run=args.dry_run,
        audit_status=args.audit_status,
    )
    if not res or res.get("total", 0) == 0:
        print("❌ 未取到文章（可能该公司无最新文章或登录失败）")
        sys.exit(1)
    if res.get("bad", 0) == res.get("total", 0):
        print("❌ 粘贴阶段全部失败，未产生可填链接")
        sys.exit(1)
    urls = [u for u in res.get("urls", []) if u]
    print(f"✅ 粘贴 {res['ok']}/{res['total']} 篇 → 子目录 {subdir}")

    if args.dry_run:
        print("（dry-run，跳过填表）")
        return

    # 2) 回填审核表
    if not sheet_node:
        print("⚠️ 该客户未配置审核表（sheet_node 缺失），跳过填表")
        return
    from fill_sheet import run_fill
    from prepare_multi import get_token
    token = get_token()
    date_label = subdir_to_datelabel(subdir)
    run_fill(sheet_node, urls, col=2, token=token, date_label=date_label)
    print(f"✅ 已填审核表（B 列链接，A 列合并「{date_label}」）")


if __name__ == "__main__":
    main()
