#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
direction_b_cms_write.py — 方向 B 的 CMS 写入阶段（浏览器 CDP）
================================================================================
把「飞书块→HTML」转换好的文章(由 skill 的 batch_wiki_to_cms 产出)写回
huiyouhua CMS 词包。这是 SKILL.md 步骤 B6 的可复用实现。

⚠️ 关键前提：
   1) Chrome 必须带 --remote-debugging-port 启动（用 launch_chrome_debug.py）。
   2) 该 Chrome 必须已登录 huiyouhua，且页面在 huiyouhua 域名下
      （fetch('/yunying/v1/articles') 才能带登录态 Cookie）。
   3) corp_id 必须是真实公司 ID！传错接口会返回成功，但界面永远看不到。
      不确定就用 --auto-corp 或 --verify-corp 先查。

用法：
    # 1) 先确认 corp_id 对不对（读当前公司一篇样本，不写任何东西）
    python direction_b_cms_write.py --articles skill_converted.json \\
        --pkg-id 1331 --verify-corp

    # 2) 先写 1 篇探路，确认它出现在词包列表里
    python direction_b_cms_write.py --articles skill_converted.json \\
        --corp-id 1155 --pkg-id 1331 --preflight

    # 3) 正式写全部，并逐篇校验落库
    python direction_b_cms_write.py --articles skill_converted.json \\
        --corp-id 1155 --pkg-id 1331

    # 自动用当前公司 corp_id（从列表第一篇反查）
    python direction_b_cms_write.py --articles skill_converted.json \\
        --pkg-id 1331 --auto-corp

    # 删除之前写错的文章（逗号分隔 id 列表）
    python direction_b_cms_write.py --delete 235619,235620

article JSON 格式(由 batch_wiki_to_cms 产出):
    [{"title": "...", "content": "<h2>..</h2><p>..</p>"}, ...]
"""
import os
import sys
import json
import time
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CDP_URL = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222")


def _connect_cdp(cdp_url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 需要 Playwright: pip install playwright")
        sys.exit(1)
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    if "huiyouhua" not in page.url:
        print(">>> 导航到 CMS 文章页 ...")
        page.goto("https://yunying.huiyouhua.com/cms-yunying.html?tab=articles",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    return p, browser, page


def _fetch_eval(page, method, path, body=None):
    """在页面上下文里发一次 fetch，返回解析后的 dict。"""
    import json as _json
    expr = (
        "(async()=>{"
        f"  var method={_json.dumps(method)};"
        f"  var path={_json.dumps(path)};"
        f"  var body={_json.dumps(body) if body is not None else 'null'};"
        "  try {"
        "    var ctrl=new AbortController();"
        "    var t=setTimeout(function(){ctrl.abort();},15000);"
        "    var r=await fetch(path,{method:method,"
        "      headers:{'Content-Type':'application/json'},"
        "      body: body?JSON.stringify(body):undefined, signal:ctrl.signal});"
        "    clearTimeout(t);"
        "    var d=await r.json();"
        "    return JSON.stringify({ok:true, status:r.status, data:d});"
        "  } catch(e){"
        "    return JSON.stringify({ok:false, error:String(e)});"
        "  }"
        "})()"
    )
    out = page.evaluate(expr)
    return _json.loads(out)


def verify_corp(page):
    """读当前公司词包列表第一篇，反查真实 corp_id（不写任何东西）。"""
    print(">>> [verify-corp] 读取当前公司词包文章列表(前两篇) ...")
    res = _fetch_eval(page, "GET",
                      "/yunying/v1/creation/articles?page=1&page_size=2")
    if not res.get("ok"):
        print("    ❌ 读取失败:", res.get("error"))
        return None
    data = (res.get("data") or {}).get("data") or {}
    arts = data.get("articles") or []
    if not arts:
        print("    ⚠️ 当前公司词包列表为空，无法反查 corp_id；请用 --corp-id 手动指定。")
        return None
    a0 = arts[0]
    print(f"    当前公司 corp_id = {a0.get('corp_id')}")
    print(f"    样本文章: id={a0.get('id')} title={a0.get('title')} "
          f"pkg={a0.get('keyword_package_id')}")
    return a0.get("corp_id")


def write_articles(page, articles, corp_id, pkg_id, limit=None):
    """在页面上下文里循环 POST /yunying/v1/articles。返回结果列表。"""
    calls = [{"title": a["title"], "content": a["content"],
              "corp_id": corp_id, "keyword_package_id": pkg_id}
             for a in articles[:limit] if a.get("content", "").strip()]
    expr = (
        "(async()=>{"
        "  var calls=" + json.dumps(calls, ensure_ascii=False) + ";"
        "  var results=[];"
        "  for (var i=0;i<calls.length;i++){"
        "    try {"
        "      var ctrl=new AbortController();"
        "      var t=setTimeout(function(){ctrl.abort();},15000);"
        "      var r=await fetch('/yunying/v1/articles',{"
        "        method:'POST', headers:{'Content-Type':'application/json'},"
        "        body:JSON.stringify(calls[i]), signal:ctrl.signal});"
        "      clearTimeout(t);"
        "      var d=await r.json();"
        "      results.push({index:i,code:d.code,msg:(d.msg||d.message||''),"
        "        id:(d.data&&d.data.id),title:calls[i].title.substring(0,40),"
        "        corp_id:calls[i].corp_id,pkg:calls[i].keyword_package_id});"
        "    } catch(e){"
        "      results.push({index:i,code:-1,error:String(e),"
        "        title:calls[i].title.substring(0,40)});"
        "    }"
        "  }"
        "  return JSON.stringify(results);"
        "})()"
    )
    out = page.evaluate(expr)
    return json.loads(out)


def verify_written(page, results, corp_id, pkg_id):
    """逐篇 GET /yunying/v1/articles/{id}，确认落在正确公司+词包。"""
    print(">>> 逐篇校验落库 ...")
    good, bad = [], []
    for x in results:
        aid = x.get("id")
        if not aid or x.get("code") != 0:
            bad.append(x)
            continue
        res = _fetch_eval(page, "GET", f"/yunying/v1/articles/{aid}")
        if not res.get("ok"):
            bad.append({**x, "verify": "fetch失败 " + str(res.get("error"))})
            continue
        art = (res.get("data") or {}).get("data") or {}
        real_corp = art.get("corp_id")
        real_pkg = art.get("keyword_package_id")
        ok_corp = (real_corp == corp_id) if corp_id is not None else True
        ok_pkg = (real_pkg == pkg_id) if pkg_id is not None else True
        if ok_corp and ok_pkg:
            good.append({**x, "verify_corp": real_corp, "verify_pkg": real_pkg})
        else:
            bad.append({**x, "verify_corp": real_corp, "verify_pkg": real_pkg,
                        "mismatch": True})
    return good, bad


def delete_articles(page, ids):
    print(f">>> 删除 {len(ids)} 篇 ...")
    for aid in ids:
        res = _fetch_eval(page, "DELETE", f"/yunying/v1/articles/{aid}")
        ok = res.get("ok") and ((res.get("data") or {}).get("code") == 0)
        print(f"    id={aid}: {'✅' if ok else '❌ ' + str(res)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", help="转换好的文章 JSON(标题+HTML)")
    ap.add_argument("--corp-id", type=int, default=None, help="真实公司 ID")
    ap.add_argument("--pkg-id", type=int, default=None, help="词包 ID")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--preflight", action="store_true", help="只写 1 篇探路")
    ap.add_argument("--verify-corp", action="store_true",
                    help="只查当前公司 corp_id，不写")
    ap.add_argument("--auto-corp", action="store_true",
                    help="从列表反查 corp_id 并自动使用")
    ap.add_argument("--delete", help="逗号分隔的文章 id 列表，删除之")
    ap.add_argument("--out", default="cms_write_results.json")
    args = ap.parse_args()

    cdp_url = f"http://127.0.0.1:{args.port}"
    p, browser, page = _connect_cdp(cdp_url)
    try:
        if args.verify_corp:
            verify_corp(page)
            return
        if args.delete:
            ids = [int(x) for x in args.delete.split(",") if x.strip()]
            delete_articles(page, ids)
            return

        if not args.articles or not os.path.isfile(args.articles):
            print("❌ 需要 --articles 指定转换好的 JSON 文件")
            sys.exit(1)
        articles = json.load(open(args.articles, encoding="utf-8"))

        corp_id = args.corp_id
        if args.auto_corp or corp_id is None:
            cid = verify_corp(page)
            if cid is None:
                print("❌ 无法自动识别 corp_id，请用 --corp-id 指定。")
                sys.exit(1)
            corp_id = cid
            print(f">>> 使用自动识别的 corp_id={corp_id}")

        if args.preflight:
            print(">>> [preflight] 写 1 篇探路 ...")
            results = write_articles(page, articles, corp_id, args.pkg_id, limit=1)
            good, bad = verify_written(page, results, corp_id, args.pkg_id)
            print(f"    成功 {len(good)} 篇, 校验异常 {len(bad)} 篇")
            for x in bad:
                print("    异常:", x)
            json.dump(results, open(args.out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            print(f"    结果已存 {args.out}")
            if bad:
                print("⚠️ 校验有异常，先别铺开！检查 corp_id/pkg_id。")
            else:
                print("✅ 探路成功，可去掉 --preflight 正式铺开。")
            return

        print(f">>> 正式写入 {len(articles)} 篇 (corp_id={corp_id}, pkg_id={args.pkg_id}) ...")
        results = write_articles(page, articles, corp_id, args.pkg_id)
        ok_w = [x for x in results if x.get("code") == 0]
        bad_w = [x for x in results if x.get("code") != 0]
        print(f"    接口返回: 成功 {len(ok_w)} 篇, 失败 {len(bad_w)} 篇")
        good, bad = verify_written(page, results, corp_id, args.pkg_id)
        print(f"    逐篇校验: 正确落库 {len(good)} 篇, 异常 {len(bad)} 篇")
        for x in bad:
            print("    异常:", x)
        json.dump(results, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"    结果已存 {args.out}")
        if bad:
            print("⚠️ 有异常篇目，检查 corp_id/pkg_id 后用 --delete 清理错发的再补。")
    finally:
        browser.close()
        p.stop()


if __name__ == "__main__":
    main()
