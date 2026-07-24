#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一组飞书文档链接追加填写到「电子表格(sheet)」的某一列（默认第二列=B列），
从已填单元格的下一行开始。

用法（命令行）：
  python fill_sheet.py --node <wiki节点token> --links <direction_a结果json> [--col 2] [--sheet 6月内容] [--dry-run]

程序内调用（机器人用）：
  from fill_sheet import run_fill
  run_fill(node, urls, col=2, sheet_hint=None, token=None, dry_run=False)
  → {"ok": True/False, "count": N, "range": "xxx!B362:B391", "error": "..."}

说明：
- 节点必须是电子表格（obj_type=sheet）。先用 wiki get_node 解析出 spreadsheetToken。
- 通过 /sheets/v2/spreadsheets/{token}/metainfo 拿到各工作表 sheetId（默认选标题含「7月」或第一个）。
- 读目标列找到最后一个非空行，从 last+1 起用 PUT /values 写入链接。
- 链接写成纯 URL 字符串（飞书表格会自动可点击）。
"""
import argparse, json, os, sys, urllib.request, re
from prepare_multi import load_env, get_token

HERE = os.path.dirname(os.path.abspath(__file__))


def api(method, path, token, body=None):
    url = "https://open.feishu.cn" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        method=method)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def col_letter(idx):
    # 1->A, 2->B ...
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def col_letter_inv(letters):
    # A->1, B->2 ...
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def last_used_row(token, sptoken, sheet_id, cols=(1, 2), scan=2000):
    """考虑合并单元格，返回最后有内容的行号（取 A/B 列与合并范围的最大值）。"""
    last = 0
    for col in cols:
        cl = col_letter(col)
        d = api("GET",
                f"/open-apis/sheets/v2/spreadsheets/{sptoken}/values/"
                f"{sheet_id}!{cl}1:{cl}{scan}?valueRenderOption=ToString",
                token)
        vals = (d.get("data") or {}).get("valueRange", {}).get("values") or []
        for i, row in enumerate(vals, 1):
            if row and str(row[0]).strip() not in ("", "None"):
                last = max(last, i)
    # 合并单元格会把下方行清空，需从 metainfo 的 mergedCells 取真实占用行
    try:
        mi = api("GET", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/metainfo", token)
        for s in (mi.get("data") or {}).get("sheets") or []:
            if s.get("sheetId") == sheet_id:
                for mc in s.get("mergedCells") or []:
                    rng = mc.get("range") or ""
                    m = re.search(r"!?([A-Z]+)(\d+):[A-Z]+(\d+)", rng)
                    if m and col_letter_inv(m.group(1)) in cols:
                        last = max(last, int(m.group(3)))
                break
    except Exception:
        pass
    return last


def merge_range(token, sptoken, rng, merge_type="MERGE_ALL"):
    """合并飞书表格单元格。rng 形如 'sheetId!A1:A20'。"""
    body = {"range": rng, "mergeType": merge_type}
    return api("POST", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/merge_cells",
               token, body)


def run_fill_dated(node, urls, date_label, col=2, date_col=1, token=None,
                   dry_run=False):
    """把 links 写到 col 列（默认 B），并把 date_col 列（默认 A）对应块合并、写日期。

    用于审核表惯例：第一列合并写日期（如「7月21日」），第二列填文章链接。
    自动从「最后占用行 + 1」开始（考虑合并单元格）。
    """
    res = {"ok": False, "count": 0, "range": "", "error": "", "title": "",
           "sheet": "", "merge": "", "date_cell": ""}
    if not urls:
        res["error"] = "urls 为空"
        return res
    if token is None:
        load_env()
        token = get_token()
    sptoken, title = resolve_spreadsheet_token(token, node)
    res["title"] = title
    sheet = find_sheet(token, sptoken)
    sid = sheet["sheetId"]
    res["sheet"] = sheet.get("title", "")
    last = last_used_row(token, sptoken, sid, cols=(col, date_col))
    start = last + 1
    n = len(urls)
    end = start + n - 1
    cl = col_letter(col)
    dl = col_letter(date_col)
    link_rng = f"{sid}!{cl}{start}:{cl}{end}"
    date_rng = f"{sid}!{dl}{start}"
    merge_rng = f"{sid}!{dl}{start}:{dl}{end}"
    res["range"] = link_rng
    res["merge"] = merge_rng
    res["date_cell"] = date_rng
    res["count"] = n
    if dry_run:
        res["ok"] = True
        res["dry"] = True
        return res
    # 1) 先写日期到 A 列起点
    api("PUT", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/values", token,
        {"valueRange": {"range": date_rng, "values": [[date_label]]}})
    # 2) 合并 A 列块
    md = merge_range(token, sptoken, merge_rng, "MERGE_ALL")
    res["merged"] = (md.get("code") == 0)
    # 3) 写链接到目标列
    bd = api("PUT", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/values", token,
             {"valueRange": {"range": link_rng,
                             "values": [[u] for u in urls]}})
    if bd.get("code") == 0:
        res["ok"] = True
        res["range"] = (bd.get("data") or {}).get("updatedRange") or link_rng
    else:
        res["error"] = json.dumps(bd, ensure_ascii=False)[:500]
    return res


def resolve_spreadsheet_token(token, node):
    d = api("GET", f"/open-apis/wiki/v2/spaces/get_node?token={node}&token_type=wiki", token)
    node_info = (d.get("data") or {}).get("node") or {}
    if node_info.get("obj_type") != "sheet":
        raise RuntimeError(f"节点不是电子表格(sheet)，而是 {node_info.get('obj_type')}；本脚本只处理电子表格")
    return node_info["obj_token"], node_info.get("title", "")


def find_sheet(token, sptoken, sheet_hint=None):
    d = api("GET", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/metainfo", token)
    sheets = (d.get("data") or {}).get("sheets") or []
    if not sheets:
        raise RuntimeError("该表格没有任何工作表")
    if sheet_hint:
        for s in sheets:
            if sheet_hint in (s.get("title") or ""):
                return s
    # 默认：标题含「7月」或第一个
    for s in sheets:
        if "7月" in (s.get("title") or ""):
            return s
    return sheets[0]


def last_filled_row(token, sptoken, sheet_id, col):
    cl = col_letter(col)
    d = api("GET", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/values/{sheet_id}!{cl}1:{cl}1000?valueRenderOption=ToString", token)
    vals = (d.get("data") or {}).get("valueRange", {}).get("values") or []
    last = 0
    for i, row in enumerate(vals, 1):
        if row and str(row[0]).strip() not in ("", "None"):
            last = i
    return last


def run_fill(node, urls, col=2, sheet_hint=None, token=None, dry_run=False,
             date_label=None):
    """把 urls 追加写入电子表格 node 的 col 列（默认第二列）。

    若给定 date_label，则采用审核表惯例：第一列（date_col，默认 A）对应块合并并
    写入 date_label（如「7月21日」），第二列（col，默认 B）填链接。

    返回 dict: {"ok", "count", "range", "error", "title", "sheet"}
    """
    if date_label:
        return run_fill_dated(node, urls, date_label, col=col, token=token,
                              dry_run=dry_run)
    res = {"ok": False, "count": 0, "range": "", "error": "", "title": "", "sheet": ""}
    if not urls:
        res["error"] = "urls 为空"
        return res
    if token is None:
        load_env()
        token = get_token()

    sptoken, title = resolve_spreadsheet_token(token, node)
    res["title"] = title
    sheet = find_sheet(token, sptoken, sheet_hint)
    sid = sheet["sheetId"]
    res["sheet"] = sheet.get("title", "")
    last = last_filled_row(token, sptoken, sid, col)
    cl = col_letter(col)
    start = last + 1
    end = start + len(urls) - 1
    rng = f"{sid}!{cl}{start}:{cl}{end}"
    res["range"] = rng
    res["count"] = len(urls)

    if dry_run:
        res["ok"] = True
        res["dry"] = True
        return res

    body = {"valueRange": {"range": rng, "values": [[u] for u in urls]}}
    d = api("PUT", f"/open-apis/sheets/v2/spreadsheets/{sptoken}/values", token, body)
    if d.get("code") == 0:
        upd = (d.get("data") or {})
        res["range"] = upd.get("updatedRange") or rng
        res["ok"] = True
    else:
        res["error"] = json.dumps(d, ensure_ascii=False)[:500]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True, help="飞书 wiki 节点 token（表格所在节点）")
    ap.add_argument("--links", required=True, help="含 urls 字段的 json（direction_a 结果或自己拼的列表）")
    ap.add_argument("--col", type=int, default=2, help="目标列（1=A,2=B...）默认第二列")
    ap.add_argument("--sheet", default=None, help="工作表标题包含的关键字，默认选含「7月」或第一个")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    res_links = json.load(open(args.links, encoding="utf-8"))
    urls = res_links.get("urls") or res_links if isinstance(res_links, list) else []
    if not urls:
        print("❌ 结果文件里没有 urls 字段/为空")
        sys.exit(1)
    print(f"待填链接数: {len(urls)}")

    r = run_fill(args.node, urls, col=args.col, sheet_hint=args.sheet, dry_run=args.dry_run)
    if not r["ok"] and not args.dry_run:
        print("❌ 写入失败:", r.get("error"))
        sys.exit(1)
    print(f"电子表格: {r['title']}  工作表: {r['sheet']!r}")
    print(f"写入范围: {r['range']}  (共 {r['count']} 条)"
          + ("  [dry-run]" if args.dry_run else ""))


if __name__ == "__main__":
    main()
