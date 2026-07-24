#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_multi.py — 从飞书父目录取「指定子目录」下的全部文章并转 CMS HTML。

适用于：父目录下是一组按日期命名的子目录（如 6/29、6/30、7.1、7/2），
文章分散在这些子目录里。自动逐层收集文档（跳过 folder 递归），合并去重。

仅飞书 API，不需要 Chrome。输出合并后的文章 JSON 供 auto_paste.py 写入 CMS。
"""
import os, sys, json, re, requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # 工作区根目录


def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_token():
    return requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": os.environ["FEISHU_APP_ID"],
              "app_secret": os.environ["FEISHU_APP_SECRET"]},
        timeout=10).json()["tenant_access_token"]


def get_node_space(token, node_token):
    try:
        r = requests.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {token}"},
            params={"token": node_token, "token_type": "wiki"}, timeout=10)
        d = r.json()
        return d.get("data", {}).get("space_id")
    except Exception:
        return None


def list_nodes(token, space_id, parent):
    items, pt = [], None
    while True:
        params = {"parent_node_token": parent, "page_size": 50}
        if pt:
            params["page_token"] = pt
        r = requests.get(
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
            headers={"Authorization": f"Bearer {token}"}, params=params, timeout=10)
        d = r.json()
        if d.get("code", 0) != 0:
            print("  list_nodes 错误:", d.get("msg"), "code=", d.get("code"))
            break
        items.extend(d.get("data", {}).get("items", []))
        if not d.get("data", {}).get("has_more"):
            break
        pt = d.get("data", {}).get("page_token")
    return items


def collect_docs(token, space_id, node_token, depth=0, seen=None):
    if seen is None:
        seen = set()
    if node_token in seen:
        return []
    seen.add(node_token)
    docs = []
    for it in list_nodes(token, space_id, node_token):
        ot = it.get("obj_type", "")
        title = it.get("title", "")
        obj = it.get("obj_token", "")
        node = it.get("node_token", "")
        if ot in ("doc", "docx"):
            if obj:
                docs.append({"title": title, "obj_token": obj})
        elif depth < 4 and (ot in ("folder", "wiki", "mindnote") or not ot):
            docs += collect_docs(token, space_id, node, depth + 1, seen)
    return docs


def norm(s):
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", s or "")


def feishu_collect(node_token, subdirs=None, out_path=None, space_id=None):
    """读取飞书节点（单目录，或父目录+指定子目录）下全部文档，转 CMS HTML。
    返回 [{title, content}] 列表；若 out_path 给定则同时落盘。供 bot 与 CLI 共用。"""
    load_env()
    token = get_token()
    space_id = space_id or get_node_space(token, node_token) or "7630734017544981692"
    print("space_id =", space_id)

    docs = []
    if subdirs:
        subs = [s.strip() for s in subdirs.split(",") if s.strip()]
        children = list_nodes(token, space_id, node_token)
        matched = []
        for c in children:
            t = c.get("title", "")
            for s in subs:
                if norm(s) and (norm(s) in norm(t) or norm(t) in norm(s)):
                    matched.append(c)
                    break
        print(f"匹配到子目录 ({len(matched)}):")
        for m in matched:
            print("   -", repr(m.get("title")), "|", m.get("node_token"))
        for m in matched:
            docs += collect_docs(token, space_id, m.get("node_token"))
    else:
        docs = collect_docs(token, space_id, node_token)

    sys.path.insert(0, HERE)
    import paste_utils as skill
    results = []
    for a in docs:
        try:
            blks = skill.get_doc_blocks(token, a["obj_token"])
            html = skill.feishu_blocks_to_html(blks)
            if html.strip():
                results.append({"title": a["title"], "content": html.strip()})
        except Exception as e:
            print("FAIL", a.get("title"), e)

    seen, uniq = set(), []
    for r in results:
        if r["title"] in seen:
            continue
        seen.add(r["title"])
        uniq.append(r)

    if out_path:
        json.dump(uniq, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✅ 转换并去重后 {len(uniq)} 篇，已存 {out_path}")
    return uniq


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-token", required=True, help="飞书节点 token（单目录或父目录）")
    ap.add_argument("--subdirs", default=None,
                    help="可选，逗号分隔的子目录名，如 6/29,6/30,7.1,7/2")
    ap.add_argument("--out", default="multi_articles.json")
    ap.add_argument("--space-id", default=None)
    args = ap.parse_args()
    uniq = feishu_collect(args.parent_token, args.subdirs,
                          os.path.join(HERE, args.out), args.space_id)
    print(f"共 {len(uniq)} 篇")


if __name__ == "__main__":
    main()
