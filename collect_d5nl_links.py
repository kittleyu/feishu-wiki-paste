#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收集指定飞书目录（示例客户）下全部子文章节点链接，
并查看其审核表(sheet) 的工作表结构，供 fill_sheet.py 回填第二列。

用法：
  python collect_d5nl_links.py --dir-node YOUR_DIR_NODE
  python collect_d5nl_links.py --dir-node YOUR_DIR_NODE --sheet-node YOUR_SHEET_NODE --save
（租户域名由环境变量 FEISHU_WIKI_DOMAIN 配置，见 .env.example）
"""
import argparse, json, os, sys
import requests
from prepare_multi import (load_env, get_token, get_node_space, list_nodes,
                           feishu_wiki_domain, default_space_id)

HERE = os.path.dirname(os.path.abspath(__file__))


def get_node(token, node):
    r = requests.get("https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"token": node, "token_type": "wiki"}, timeout=10)
    return r.json().get("data", {}).get("node", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir-node", required=True, help="飞书 Wiki 文章目录 node_token")
    ap.add_argument("--sheet-node", help="飞书审核表(sheet) node_token（可选，查看其结构）")
    ap.add_argument("--save", action="store_true", help="额外写出 yn_links.json（urls 字段）")
    args = ap.parse_args()
    load_env()
    token = get_token()
    domain = feishu_wiki_domain()

    # ---- 目录子节点 ----
    space = get_node_space(token, args.dir_node) or default_space_id()
    print(f"dir space_id = {space}")
    items = list_nodes(token, space, args.dir_node)
    print(f"目录子节点总数: {len(items)}")
    docs = [it for it in items if (it.get("obj_type") or "") == "docx"]
    print(f"  其中 docx(文章) 数: {len(docs)}")
    urls = [f"{domain}/{it['node_token']}" for it in docs]
    print("前3条:", *urls[:3], sep="\n  ")
    print("后3条:", *urls[-3:], sep="\n  ")

    # ---- 审核表工作表结构 ----
    if args.sheet_node:
        nd = get_node(token, args.sheet_node)
        print(f"\n审核表节点类型: {nd.get('obj_type')}  标题: {nd.get('title')!r}  obj_token: {nd.get('obj_token')}")
        if nd.get("obj_type") == "sheet":
            spt = nd["obj_token"]
            r = requests.get(f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spt}/metainfo",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
            sheets = (r.get("data") or {}).get("sheets") or []
            print(f"审核表工作表({len(sheets)}个):")
            for s in sheets:
                print(f"   - {s.get('title')!r}  sheetId={s.get('sheetId')}")

    if args.save:
        out = os.path.join(HERE, "yn_links.json")
        json.dump({"urls": urls}, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n已写出 {out}  ({len(urls)} 条)")


if __name__ == "__main__":
    main()
