#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shengwen_pipeline.py — 智能生文 → 粘贴到飞书 → 填表（直接 API 全自动流水线）
========================================================================

v2.6.7 起改为 **直接调用 huiyouhua 后端接口**（不再模拟点击 UI），彻底规避
「UI 切公司后提交瞬间被还原成默认公司、误给错误公司批量生文」的事故。

全程只需用户本机 Chrome 调试端口(9222)已登录 huiyouhua（仅借用其登录态
cookie，通过 Playwright 的 APIRequestContext 发请求；不操作页面 DOM）。

流程：
  1. 解析 id：
     - GET /yunying/v1/corp/active                → 列出可访问公司
     - 按公司名模糊匹配候选 corp（同名多 corp 时逐个探测）
     - 对每个候选 corp：GET /yunying/v1/auth/changecorp?corp_id=X 切换
                         GET /yunying/v1/knowledge-base        → 找产品知识库 id
                         GET /yunying/v1/keyword/package      → 找词包 id
     - 选「产品+词包都命中」的 corp（中泰期货 7 个同名 corp 只有 543 有内容，自然命中）
  2. 生文：POST /yunying/v1/creation/tasks
            body={"product_kb_id":int,"keyword_package_id":int,"article_count":int}
            ⚠️ 无 corp_id 字段，公司完全由 product_kb_id 决定（绕开 UI 公司作用域坑）
  3. 轮询：GET /yunying/v1/creation/articles 筛选 keyword_package_id 匹配且
            created_at >= 任务开始时刻的新文章，直到达到期望数量
  4. 粘贴：在飞书顶层目录下建/复用「日期(M.D)」子目录（docx 节点），
            把新文章批量粘贴进去（复用方向 A 的 batch_paste）
  5. 填表：（可选）把文章链接写入指定飞书表格第二列（复用 fill_sheet.run_fill）

每步打印进度；支持 --dry-run 只解析 id + 预览将要生文，不实际生文/粘贴。

用法（CLI）：
    python shengwen_pipeline.py \
        --company "中泰期货" --pkg "中泰期货" --count 20 \
        --node ZChcwfFf5ixljKkoxSOcrheKnHf \
        --spreadsheet DUXVwK6FSiT42FkMjFgcJVeYnzh

也可被 bot 调用：
    from shengwen_pipeline import run_shengwen_pipeline
"""
import os, sys, json, time, re, argparse
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from auto_paste import ensure_chrome, PORT
from prepare_multi import load_env, get_token, get_node_space, list_nodes
from paste_utils import batch_paste, create_wiki_node_and_write
from fill_sheet import run_fill

# ── 常量 ────────────────────────────────────────────────────────────────────
HUIYOUHUA = "https://yunying.huiyouhua.com"
GEN_POLL_INTERVAL = 15        # 轮询间隔（秒）
GEN_TIMEOUT = 30 * 60         # 单次生文最长等 30 分钟（20 篇约 18 分钟）
DEFAULT_SPACE = "7630734017544981692"


# ═══════════════════════════════════════════════════════════════════════════
#  连接 + 低层请求封装
# ═══════════════════════════════════════════════════════════════════════════
def connect_cdp():
    """复用用户已登录的 Chrome 9222 调试端口，返回 (playwright, context, request)。
    request 是 APIRequestContext，自动带 context 的登录 cookie，发同源请求时
    无需手动处理鉴权。

    注意：同一进程内多次 start/stop sync playwright 可能残留 running 的
    asyncio loop，导致后续 start() 误报「inside asyncio loop」。这里捕获后
    清理 loop 重试一次。
    """
    ensure_chrome()
    from playwright.sync_api import sync_playwright
    import asyncio

    def _start():
        return sync_playwright().start()

    try:
        pw = _start()
    except Exception:
        # 清理可能残留的 event loop 后重试
        try:
            lp = asyncio.get_event_loop()
            if lp.is_running():
                lp.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass
        pw = _start()
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = b.contexts[0]
    return pw, ctx, ctx.request


def _get(req, path):
    return req.get(HUIYOUHUA + path).json()


def _post(req, path, body):
    return req.post(HUIYOUHUA + path, json=body).json()


# ═══════════════════════════════════════════════════════════════════════════
#  第 1 步：按名解析 product_kb_id / keyword_package_id
# ═══════════════════════════════════════════════════════════════════════════
def _kp_name(x):
    """词包的真实名称字段是 core_keyword（不是 name，且 name 为 None）。"""
    return (x.get("core_keyword") or "") or (x.get("description") or "")


def resolve_ids(req, company, pkg_name, log_fn=print):
    """按公司名+词包名解析出 (product_kb_id, keyword_package_id, corp_id)。

    采用「候选 corp 遍历 + 双向模糊匹配」策略：
      - 公司名匹配 corp（处理同名多 corp，如中泰期货有 7 个，仅 543 有知识库）
      - 在每个候选 corp 下查知识库/词包（接口按当前 corp 作用域过滤）
      - 选产品+词包都命中的 corp（分数最高者）
    """
    corps = _get(req, "/yunying/v1/corp/active")["data"]["corps"]
    # 快路径：公司名直接匹配 corp 名（中泰期货 / 中泰期货股份有限公司）
    cands = [c for c in corps if company in (c.get("name") or "")
             or (c.get("name") or "") in company]
    if not cands:
        # 慢路径：用户给的「公司名」可能是产品知识库名（如「长岛民宿」），
        # 而 corp 名不同（如「长岛万顺渔家中心」）。遍历所有 corp 按知识库名匹配。
        log_fn(f"⚠️ 公司名未直接匹配 corp，改按产品知识库名匹配"
               f"（遍历 {len(corps)} 个 corp）…")
        for c in corps:
            _get(req, f"/yunying/v1/auth/changecorp?corp_id={c['id']}")
            kbs = _get(req, "/yunying/v1/knowledge-base?page=1&page_size=100")["data"]["knowledge_bases"]
            if any(company in (k.get("name") or "") or (k.get("name") or "") in company
                   for k in kbs):
                cands.append(c)
    if not cands:
        raise ValueError(f"未找到匹配「{company}」的公司/产品（可访问 {len(corps)} 个）")
    log_fn(f"🔎 匹配到 {len(cands)} 个候选公司，逐个探测产品/词包…")

    best = None  # (score, kb_id, kp_id, corp_id)
    for c in cands:
        cid = c["id"]
        _get(req, f"/yunying/v1/auth/changecorp?corp_id={cid}")  # 切 corp（设 session）
        kbs = _get(req, "/yunying/v1/knowledge-base?page=1&page_size=100")["data"]["knowledge_bases"]
        kps = _get(req, "/yunying/v1/keyword/package?page=1&page_size=10000")["data"]["keyword_packages"]
        kb_match = [x for x in kbs
                    if company in (x.get("name") or "") or (x.get("name") or "") in company]
        kp_match = [x for x in kps
                    if pkg_name in _kp_name(x) or _kp_name(x) in pkg_name]
        kb_id = kb_match[0]["id"] if kb_match else None
        kp_id = kp_match[0]["id"] if kp_match else None
        log_fn(f"    corp {cid}: 产品={[x.get('name') for x in kb_match]} "
               f"词包={[_kp_name(x) for x in kp_match]}")
        score = (1 if kb_id else 0) + (1 if kp_id else 0)
        if best is None or score > best[0]:
            best = (score, kb_id, kp_id, cid)

    if best[1] is None and best[2] is None:
        raise ValueError(f"在候选公司中未找到匹配的产品/词包（公司={company!r} 词包={pkg_name!r}）")
    log_fn(f"✅ 选用 corp_id={best[3]}：product_kb_id={best[1]} keyword_package_id={best[2]}")
    return best[1], best[2], best[3]


# ═══════════════════════════════════════════════════════════════════════════
#  第 2 步：创建生文任务（直接调后端接口）
# ═══════════════════════════════════════════════════════════════════════════
def create_task(req, product_kb_id, keyword_package_id, count, log_fn=print):
    body = {"product_kb_id": int(product_kb_id),
            "keyword_package_id": int(keyword_package_id),
            "article_count": int(count)}
    log_fn(f">>> 提交生文任务: {body}")
    d = _post(req, "/yunying/v1/creation/tasks", body)
    if d.get("code") != 0:
        raise RuntimeError(f"创建生文任务失败: code={d.get('code')} msg={d.get('message')} body={body}")
    log_fn(f"✅ 任务创建成功（code={d.get('code')}，msg={d.get('message')}）")
    return d


# ═══════════════════════════════════════════════════════════════════════════
#  第 3 步：轮询等待生文完成
# ═══════════════════════════════════════════════════════════════════════════
def wait_for_articles(req, keyword_package_id, start_iso, expected,
                      log_fn=print, timeout=GEN_TIMEOUT, poll=GEN_POLL_INTERVAL):
    """轮询 creation/articles，筛选 keyword_package_id 匹配且 created_at>=start 的新文章。

    返回最新 expected 篇文章列表（按 created_at 降序）。"""
    collected = {}
    start = time.time()
    while time.time() - start < timeout:
        data = _get(req, "/yunying/v1/creation/articles?page=1&page_size=100")
        arts = (data.get("data") or {}).get("articles") or []
        for a in arts:
            ca = a.get("created_at") or ""
            if a.get("keyword_package_id") == keyword_package_id and ca >= start_iso:
                collected[a["id"]] = a
        elapsed = int(time.time() - start)
        log_fn(f"    [{elapsed}s] 已匹配新文章 {len(collected)}/{expected}")
        if len(collected) >= expected:
            lst = sorted(collected.values(), key=lambda a: a.get("created_at") or "",
                         reverse=True)
            return lst[:expected]
        time.sleep(poll)
    lst = sorted(collected.values(), key=lambda a: a.get("created_at") or "",
                 reverse=True)
    return lst[:expected]


# ═══════════════════════════════════════════════════════════════════════════
#  第 4 步：建日期子目录 + 粘贴到飞书
# ═══════════════════════════════════════════════════════════════════════════
def ensure_date_subdir(token, space_id, parent_node, log_fn=print):
    """在顶层目录下建/复用「日期(M.D)」子目录（docx 节点），返回子目录 node_token。

    约定：用户只给最上层公司名目录链接，bot 自动建当天日期子目录粘贴，
    方便按日期归档（如 7.21）。
    """
    date_label = f"{datetime.now().month}.{datetime.now().day}"
    children = list_nodes(token, space_id, parent_node)
    for ch in children:
        if (ch.get("title") or "") == date_label:
            log_fn(f"✅ 日期子目录已存在: {date_label} ({ch.get('node_token')})")
            return ch.get("node_token")
    log_fn(f">>> 建日期子目录: {date_label}")
    blocks = [{"block_type": 2,
               "text": {"elements": [{"text_run": {"content": f"{date_label} 智能生文",
                                                   "text_element_style": {"bold": False}}}],
                        "style": {"align": 1, "folded": False}}}]
    node_token, _, err = create_wiki_node_and_write(
        token, space_id, parent_node, date_label, blocks)
    if err:
        raise RuntimeError(f"建日期子目录失败: {err}")
    log_fn(f"✅ 已建日期子目录 {date_label} → {node_token}")
    return node_token


def paste_to_feishu(articles, feishu_node_token, log_fn=print):
    """把文章列表粘贴到飞书目录（含自动建日期子目录）。返回 (urls, ok, bad)。"""
    load_env()
    token = get_token()
    space_id = get_node_space(token, feishu_node_token) or DEFAULT_SPACE
    folder = ensure_date_subdir(token, space_id, feishu_node_token, log_fn)
    arts = [{"title": (a.get("title") or "").strip(),
             "content": a.get("content") or ""}
            for a in articles if (a.get("title") and a.get("content"))]
    if not arts:
        log_fn("⚠️ 没有可粘贴的有效文章")
        return [], 0, 0
    log_fn(f">>> 粘贴 {len(arts)} 篇文章到子目录 {folder} …")
    urls = batch_paste(arts, token, space_id, folder,
                       state_file=os.path.join(HERE, "shengwen_state.json"))
    ok = sum(1 for u in urls if u)
    bad = len(urls) - ok
    log_fn(f"✅ 粘贴完成：成功={ok} 失败={bad}")
    return urls, ok, bad


# ═══════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════
def run_shengwen_pipeline(company, pkg_name, count, feishu_node_token=None,
                          spreadsheet_url=None, dry_run=False, log_fn=print,
                          start_iso=None, submit=True):
    """执行完整的「智能生文 → 粘贴 → 填表」流水线（直接 API 版）。

    参数：
        company:           公司名（用于定位 corp 与产品知识库，如 "中泰期货"）
        pkg_name:          关键词包名（如 "中泰期货" / "长岛民宿推荐"）
        count:             生成数量
        feishu_node_token: 飞书 Wiki【顶层公司目录】node_token（自动建日期子目录）
        spreadsheet_url:   目标表格 URL（None 则跳过填表）
        dry_run:           若为 True，只解析 id + 预览，不实际生文/粘贴/填表

    返回 dict 含各步骤状态与结果。
    """
    report = {
        "resolved": None, "task_created": False,
        "generated_count": 0, "step4_paste": None, "step5_spreadsheet": None,
    }
    pw, ctx, req = connect_cdp()
    try:
        # ── Step 1: 解析 id ────────────────────────────────────
        log_fn(f">>> [1/4] 解析公司/词包: 公司={company!r} 词包={pkg_name!r}")
        product_kb_id, keyword_package_id, corp_id = resolve_ids(
            req, company, pkg_name, log_fn)
        report["resolved"] = {"product_kb_id": product_kb_id,
                              "keyword_package_id": keyword_package_id,
                              "corp_id": corp_id}

        if dry_run:
            log_fn(f"🏁 [DRY RUN] 解析完成 → product_kb_id={product_kb_id} "
                   f"keyword_package_id={keyword_package_id} corp_id={corp_id}，"
                   f"将生成 {count} 篇。未实际生文/粘贴/填表。")
            return report

        # ── Step 2: 创建生文任务（submit=False 时跳过，用于「继续」模式）──
        if submit:
            log_fn(f">>> [2/4] 提交生文任务（{count} 篇）…")
            create_task(req, product_kb_id, keyword_package_id, count, log_fn)
            report["task_created"] = True
            # 时间分界点：任务创建时刻往前推 2 分钟容错
            start_iso = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            log_fn(f">>> [2/4] 跳过提交（继续模式），沿用起点 {start_iso}")
            if start_iso is None:
                start_iso = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        report["start_iso"] = start_iso

        # ── Step 3: 等待生文完成 ──────────────────────────────
        log_fn(f">>> [3/4] 等待生文完成（异步，约需数分钟~数十分钟；起点 {start_iso}）…")
        articles = wait_for_articles(req, keyword_package_id, start_iso, count, log_fn)
        report["generated_count"] = len(articles)
        if not articles:
            log_fn("⚠️ 未在超时内等到新文章，流程中止（可继续模式再跑一次收尾）")
            return report

        # ── Step 4: 粘贴到飞书（自动建日期子目录）─────────────
        if feishu_node_token:
            log_fn(f">>> [4/4] 粘贴到飞书顶层目录 {feishu_node_token} …")
            urls, ok, bad = paste_to_feishu(articles, feishu_node_token, log_fn)
            report["step4_paste"] = {"urls": urls, "ok": ok, "bad": bad}
        elif spreadsheet_url:
            log_fn("⚠️ 填表需要先粘贴拿到链接，但未指定飞书目录，跳过填表")

        # ── Step 5: 填表（方向 F）──────────────────────────────
        if spreadsheet_url and report.get("step4_paste"):
            urls = report["step4_paste"].get("urls", [])
            if urls:
                # 审核表惯例：第一列合并写当天日期（如「7月21日」），第二列填链接
                date_label = f"{datetime.now().month}月{datetime.now().day}日"
                log_fn(f">>> 填表: {spreadsheet_url}（第一列合并写「{date_label}」"
                       f"，第二列填链接）")
                try:
                    token = get_token()
                    r = run_fill(spreadsheet_url, urls, col=2, token=token,
                                 date_label=date_label)
                    report["step5_spreadsheet"] = r
                    if r.get("ok"):
                        log_fn(f"✅ [5/5] 填表完成：{r.get('count', 0)} 条 → "
                               f"{r.get('title', '')} / {r.get('sheet', '')} "
                               f"链接范围 {r.get('range', '')}；"
                               f"日期合并 {r.get('merge', '')} "
                               f"({'成功' if r.get('merged') else '失败'})")
                    else:
                        log_fn(f"❌ [5/5] 填表失败: {r.get('error', 'unknown')}")
                except Exception as e:
                    log_fn(f"❌ [5/5] 填表异常: {e}")
            else:
                log_fn("⚠️ 没有可填的链接（粘贴未产生有效 URL）")

        log_fn("🎉 全部流程完成！")
        return report
    finally:
        pw.stop()


# ═══════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="huiyouhua 智能生文（直接 API）→ 粘贴飞书 → 填表 全自动流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python shengwen_pipeline.py --company "中泰期货" --pkg "中泰期货" --count 20 \\
      --node ZChcwfFf5ixljKkoxSOcrheKnHf --spreadsheet DUXVwK6FSiT42FkMjFgcJVeYnzh
  python shengwen_pipeline.py --company "中泰期货" --pkg "中泰期货" --count 5 --dry-run
        """,
    )
    parser.add_argument("--company", required=True, help="公司名（如「中泰期货」）")
    parser.add_argument("--pkg", required=True, help="关键词包名（如「中泰期货」）")
    parser.add_argument("--count", type=int, default=10, help="生成数量（默认 10）")
    parser.add_argument("--node", default=None, help="飞书 Wiki 顶层目录 node_token（粘贴目标）")
    parser.add_argument("--spreadsheet", default=None, help="表格 URL（填表目标）")
    parser.add_argument("--dry-run", action="store_true", help="只解析 id + 预览，不生文/粘贴/填表")
    args = parser.parse_args()

    print("=" * 60)
    print("  智能生文流水线（直接 API 版）")
    print(f"  公司: {args.company}")
    print(f"  词包: {args.pkg}")
    print(f"  数量: {args.count}")
    print(f"  飞书目录: {args.node or '(不粘贴)'}")
    print(f"  填表: {args.spreadsheet or '(不填表)'}")
    print(f"  DryRun: {args.dry_run}")
    print("=" * 60)

    result = run_shengwen_pipeline(
        company=args.company, pkg_name=args.pkg, count=args.count,
        feishu_node_token=args.node, spreadsheet_url=args.spreadsheet,
        dry_run=args.dry_run,
    )

    print("\n" + "=" * 60)
    print("  结果汇总")
    print("=" * 60)
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
