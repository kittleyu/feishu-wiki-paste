#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
direction_a_cms_to_feishu.py — 方向 A：CMS 文章 → 飞书 Wiki 目录（全自动）
================================================================================
把某公司在 huiyouhua CMS 里「最新 N 篇」文章，批量创建到飞书知识库指定目录下。

⚠️ 实战踩坑（2026-07 中泰期货）：
  - 用户给的「公司编号」(如 83966) 不是真实 corp_id，且同名公司可能有多个
    （中泰期货在 CMS 里竟有 7 个同名 corp！其中 6 个是空的，只有 543 有 1728 篇）。
    → 按公司名匹配到所有候选，逐个探一下文章数，选「有文章且最多」的那个。
  - creation/articles 已含完整 content（HTML），可直接转飞书块，无需再逐篇 GET。
  - 排序用 created_at（ISO8601，+08:00），降序取最新 N 篇。

用法（CLI）：
    python direction_a_cms_to_feishu.py \
        --company "中泰期货股份有限公司" \
        --node Zi5CwEPMKivP2UkpWFucjDrDnDd \
        --limit 10

也可被 bot 调用：
    from direction_a_cms_to_feishu import run_pipeline_a, find_corp_by_name
"""
import os, sys, json, time, re
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from prepare_multi import load_env, get_token, get_node_space, list_nodes
from paste_utils import batch_paste, create_wiki_node_and_write
# 复用 auto_paste 的 Chrome 登录/连接工具（仅导入，不触发 playwright 顶层加载）
from auto_paste import (ensure_chrome, check_logged_in, do_login,
                        safe_eval, goto_stable)

PORT = 9222
TARGET = "https://yunying.huiyouhua.com/cms-yunying.html?tab=articles"


def _parse_time(s):
    """created_at ISO8601 → datetime；失败返回 None（调用方可退回字符串排序）。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def list_corps(page):
    return safe_eval(page,
        "(async()=>{var r=await fetch('/yunying/v1/corp/active',{method:'GET'});"
        "var d=await r.json();return d.data.corps;})()")


def changecorp(page, corp_id):
    safe_eval(page,
        "(async(c)=>{await fetch('/yunying/v1/auth/changecorp?corp_id='+c,"
        "{method:'GET'});return 'ok';})(%d)" % int(corp_id))
    page.wait_for_timeout(600)


def article_count(page, corp_id):
    changecorp(page, corp_id)
    r = safe_eval(page,
        "(async()=>{var rr=await fetch('/yunying/v1/creation/articles?page=1&page_size=1',"
        "{method:'GET'});var d=await rr.json();"
        "return (d.data&&d.data.total)||0;})()")
    return r or 0


def fetch_latest(page, corp_id, limit, page_size=None):
    """拉取该公司最新 limit 篇（含完整 content），按 created_at 降序。"""
    changecorp(page, corp_id)
    n = page_size or max(limit, 50)
    url = f"/yunying/v1/creation/articles?page=1&page_size={int(n)}"
    data = safe_eval(page,
        "(async()=>{var r=await fetch(" + json.dumps(url) +
        ",{method:'GET'});var d=await r.json();return d;})()")
    arts = (data.get("data") or {}).get("articles") or []
    # 排序：优先按 created_at 解析成 datetime，否则按字符串（ISO 同格式同区可字典序比）
    arts.sort(key=lambda a: (_parse_time(a.get("created_at")) or a.get("created_at") or ""),
              reverse=True)
    top = arts[:limit]
    out = []
    for a in top:
        content = a.get("content") or ""
        if not content.strip():
            continue
        out.append({
            "title": (a.get("title") or "").strip(),
            "content": content,
            "id": a.get("id"),
            "created_at": a.get("created_at") or "",
        })
    return out


def find_corp_by_name(page, company, log_fn=print):
    """按公司名匹配所有候选，选「有文章且文章最多」的 corp。

    返回 (corp_id, name) 或 (None, None)。
    """
    corps = list_corps(page)
    cands = [c for c in corps if company in (c.get("name") or "")]
    if not cands:
        log_fn(f"❌ 未找到匹配「{company}」的公司（可访问 {len(corps)} 个）")
        return None, None
    log_fn(f"🔎 匹配到 {len(cands)} 个同名公司，逐个探测文章数…")
    best = None
    for c in cands:
        cnt = article_count(page, c["id"])
        log_fn(f"    corp_id={c['id']} 文章数={cnt}")
        if cnt and (best is None or cnt > best[1]):
            best = (c["id"], cnt, c.get("name"))
    if best is None:
        log_fn("❌ 所有同名公司都没有文章，无法继续")
        return None, None
    log_fn(f"✅ 选用 corp_id={best[0]}（{best[2]}，{best[1]} 篇）")
    return best[0], best[2]


def run_pipeline_a(company, node_token, limit=10, log_fn=print,
                   space_id=None, dry_run=False, out_file=None, subdir=None):
    """方向 A 主流程：CMS 最新 limit 篇 → 飞书 Wiki 目录。

    company: 公司名（模糊匹配；同名多 corp 时自动选有文章且最多的）
    node_token: 目标飞书 Wiki 目录 node_token
    limit: 取最新几篇
    subdir: 若指定，在该目录下新建/复用名为 subdir 的子目录并贴入（如 '7.22'）
    返回 {total, ok, bad, urls, corp_id, space_id}
    """
    load_env()  # 确保 FEISHU_APP_ID/SECRET 等凭证注入环境变量（run_client.py 路径必经）
    out_file = out_file or os.path.join(HERE, "direction_a_cms_results.json")
    ensure_chrome()
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = b.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        goto_stable(page, TARGET)
        if not check_logged_in(page):
            log_fn(">>> 未登录 huiyouhua，尝试登录 …")
            if not do_login(page):
                log_fn("❌ 登录失败，需人工介入")
                return None
            goto_stable(page, TARGET)
            if not check_logged_in(page):
                log_fn("❌ 登录后仍无法访问接口")
                return None
        else:
            log_fn("✅ huiyouhua 已是登录态")

        # 1) 解析真实 corp_id
        corp_id, corp_name = find_corp_by_name(page, company, log_fn)
        if corp_id is None:
            return None

        # 2) 拉最新 limit 篇
        log_fn(f">>> 拉取 {corp_name}({corp_id}) 最新 {limit} 篇 …")
        articles = fetch_latest(page, corp_id, limit)
        if not articles:
            log_fn("⚠️ 没有可读的文章")
            return None
        log_fn(f"📄 取出 {len(articles)} 篇（按 created_at 降序）")
        for i, a in enumerate(articles):
            log_fn(f"    {i+1}. {a['title'][:40]!r}  ({a['created_at']})")

        # 3) 飞书写入
        token = get_token()
        space_id = space_id or get_node_space(token, node_token) or "7630734017544981692"
        # 若指定子目录，先建/复用子目录，贴入子目录而非父目录
        if subdir:
            children = list_nodes(token, space_id, node_token)
            sub_node = None
            for ch in children:
                if (ch.get("title") or "") == subdir:
                    sub_node = ch.get("node_token")
                    log_fn(f"✅ 子目录已存在: {subdir} ({sub_node})")
                    break
            if not sub_node:
                log_fn(f">>> 新建子目录: {subdir}")
                blocks = [{"block_type": 2,
                           "text": {"elements": [{"text_run": {"content": subdir,
                                                               "text_element_style": {"bold": False}}}],
                                    "style": {"align": 1, "folded": False}}}]
                sub_node, _, err = create_wiki_node_and_write(
                    token, space_id, node_token, subdir, blocks)
                if err:
                    log_fn(f"❌ 建子目录失败: {err}")
                    return None
                log_fn(f"✅ 已建子目录 {subdir} → {sub_node}")
            node_token = sub_node
        log_fn(f">>> 写入飞书 Wiki（space_id={space_id}, 父节点={node_token}）…")

        if dry_run:
            from paste_utils import preview_blocks
            preview_blocks(articles)
            return {"total": len(articles), "ok": 0, "bad": 0,
                    "urls": [], "corp_id": corp_id, "space_id": space_id,
                    "dry_run": True}

        urls = batch_paste(articles, token, space_id, node_token,
                           state_file=os.path.join(HERE, "direction_a_state.json"))
        ok = sum(1 for u in urls if u)
        bad = len(urls) - ok
        log_fn(f"✅ 方向 A 完成：成功 {ok} / 失败 {bad}")
        json.dump({"corp_id": corp_id, "corp_name": corp_name,
                   "node_token": node_token, "space_id": space_id,
                   "articles": articles, "urls": urls},
                  open(out_file, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        return {"total": len(articles), "ok": ok, "bad": bad,
                "urls": urls, "corp_id": corp_id, "space_id": space_id}
    finally:
        b.close()
        p.stop()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True, help="公司名，如 中泰期货股份有限公司")
    ap.add_argument("--node", required=True, help="目标飞书 Wiki 目录 node_token")
    ap.add_argument("--limit", type=int, default=10, help="最新几篇（默认 10）")
    ap.add_argument("--space-id", default=None)
    ap.add_argument("--dry-run", action="store_true", help="仅预览转换，不写入")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    load_env()
    res = run_pipeline_a(args.company, args.node, args.limit,
                         log_fn=print, space_id=args.space_id,
                         dry_run=args.dry_run, out_file=args.out)
    if res:
        print("汇总:", json.dumps({k: v for k, v in res.items()
                                   if k != "urls"}, ensure_ascii=False))
        for u in res.get("urls", []):
            if u:
                print("  ", u)


if __name__ == "__main__":
    main()
