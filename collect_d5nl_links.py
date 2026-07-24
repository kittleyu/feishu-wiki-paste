#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收集 D5nL 目录（云南约牛 7.20）下全部子文章节点链接，
并查看 PI5 审核表(sheet) 的工作表结构，供 fill_sheet.py 回填第二列。

用法：
  python collect_d5nl_links.py            # 收集 + 打印 PI5 工作表
  python collect_d5nl_links.py --save     # 额外写出 yn_links.json（urls 字段）
"""
import argparse, json, os, sys
import requests
from prepare_multi import load_env, get_token, get_node_space, list_nodes

HERE = os.path.dirname(os.path.abspath(__file__))
D5NL = "D5nLwcbp9itV2ykAhM9cIx9Xnvd"
PI5 = "PI5owcGzii2mnSkFvjSc01H2nvg"
DOMAIN = "https://vcnd134o0gra.feishu.cn/wiki"


def get_node(token, node):
    r = requests.get("https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"token": node, "token_type": "wiki"}, timeout=10)
    return r.json().get("data", {}).get("node", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    load_env()
    token = get_token()

    # ---- D5nL 子节点 ----
    space = get_node_space(token, D5NL) or "7630734017544981692"
    print(f"D5nL space_id = {space}")
    items = list_nodes(token, space, D5NL)
    print(f"D5nL 子节点总数: {len(items)}")
    docs = [it for it in items if (it.get("obj_type") or "") == "docx"]
    print(f"  其中 docx(文章) 数: {len(docs)}")
    urls = [f"{DOMAIN}/{it['node_token']}" for it in docs]
    print("前3条:", *urls[:3], sep="\n  ")
    print("后3条:", *urls[-3:], sep="\n  ")

    # ---- PI5 工作表结构 ----
    nd = get_node(token, PI5)
    print(f"\nPI5 节点类型: {nd.get('obj_type')}  标题: {nd.get('title')!r}  obj_token: {nd.get('obj_token')}")
    if nd.get("obj_type") == "sheet":
        spt = nd["obj_token"]
        r = requests.get(f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spt}/metainfo",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        sheets = (r.get("data") or {}).get("sheets") or []
        print(f"PI5 工作表({len(sheets)}个):")
        for s in sheets:
            print(f"   - {s.get('title')!r}  sheetId={s.get('sheetId')}")

    if args.save:
        out = os.path.join(HERE, "yn_links.json")
        json.dump({"urls": urls}, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n已写出 {out}  ({len(urls)} 条)")


if __name__ == "__main__":
    main()
