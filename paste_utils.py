#!/usr/bin/env python3
"""
飞书 Wiki 批量粘贴 + 链接填表工具 (v2.2.0)
===========================================
v2.2.0 新增：Wiki→CMS 反向粘贴（飞书块→HTML 转换 + CMS 文章创建/删除）

核心函数可直接复用。HTML→飞书块转换基于 Python 标准库 html.parser.HTMLParser，
飞书块→HTML 转换基于块类型字段映射，确保完整、无残留地处理内容。

用法：
    from paste_utils import convert_html_to_blocks, batch_paste, retry_failed
    from paste_utils import feishu_blocks_to_html, batch_wiki_to_cms
"""

import requests, json, time, re, os, sys
from datetime import datetime
from html.parser import HTMLParser
from prepare_multi import default_space_id, feishu_wiki_domain

# Windows 终端默认 GBK，重配置 stdout/stderr 为 utf-8，避免 emoji/中文打印报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# ─── Token ────────────────────────────────────────────────────────
def get_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10
    )
    return resp.json()["tenant_access_token"]


# ═══════════════════════════════════════════════════════════════════
# Block Helpers
# ═══════════════════════════════════════════════════════════════════

def style_block(bold=False):
    return {"bold": bold, "inline_code": False, "italic": False,
            "strikethrough": False, "underline": False}

def make_heading(level, text):
    return {
        "block_type": {2: 4, 3: 5, 4: 6}[level],
        f"heading{level}": {
            "elements": [{"text_run": {"content": text, "text_element_style": style_block()}}],
            "style": {"align": 1, "folded": False}
        }
    }

def make_text_block(text):
    """将 **bold** 标记的文本转为飞书 text 块 elements"""
    parts = re.split(r'(\*\*)', text)
    elements = []
    is_bold = False
    for part in parts:
        if part == '**':
            is_bold = not is_bold
            continue
        if not part:
            continue
        elements.append({"text_run": {"content": part, "text_element_style": style_block(bold=is_bold)}})
    return {
        "block_type": 2,
        "text": {"elements": elements, "style": {"align": 1, "folded": False}}
    }


# ═══════════════════════════════════════════════════════════════════
# HTML → Blocks (HTMLParser)
# ═══════════════════════════════════════════════════════════════════

class FeishuBlockParser(HTMLParser):
    """将 CMS HTML 转换为飞书文档块列表。

    处理：<h2>-<h4>、<p>、<ul>/<ol>、<strong>、<li>、<table>/<tr>/<td>/<th>
    注意：列表块用 block_type: 2 模拟（block_type: 14/16 会触发 field validation failed）
    表格块(block_type:31) 用 _kind='table' 标记，由 create_wiki_node_and_write
    转成飞书 descendant 结构写入（/children 端点不支持嵌套表格）。
    """
    def __init__(self):
        super().__init__()
        self.blocks = []
        self._current = []
        self._tag = None
        self._list_items = []
        self._in_list = None  # 'ul' | 'ol' | None
        # 表格解析状态
        self._in_table = False
        self._table_rows = []      # 当前表格所有行：[[(text, is_th), ...], ...]
        self._cur_row = None       # 当前行：[(text, is_th), ...]
        self._cur_cell = None      # 当前单元格字符累积
        self._in_cell = False
        self._cur_is_th = False

    def handle_starttag(self, tag, attrs):
        if tag in ('h2', 'h3', 'h4'):
            self._flush_text()
            self._tag = tag
        elif tag == 'strong':
            if self._in_cell:
                self._cur_cell.append('**')
            else:
                self._current.append('**')
        elif tag == 'ul':
            self._flush_text()
            self._in_list = 'ul'
        elif tag == 'ol':
            self._flush_text()
            self._in_list = 'ol'
        elif tag == 'li':
            self._current = []
        elif tag == 'table':
            self._flush_text()
            self._in_table = True
            self._table_rows = []
        elif tag == 'tr':
            self._cur_row = []
        elif tag in ('td', 'th'):
            self._cur_cell = []
            self._in_cell = True
            self._cur_is_th = (tag == 'th')

    def handle_endtag(self, tag):
        if tag in ('h2', 'h3', 'h4'):
            text = ''.join(self._current).strip()
            if text:
                self.blocks.append(make_heading(int(tag[1]), text))
            self._current = []
            self._tag = None
        elif tag == 'strong':
            if self._in_cell:
                self._cur_cell.append('**')
            else:
                self._current.append('**')
        elif tag == 'p':
            self._flush_text()
            self._tag = None
        elif tag == 'li':
            if self._in_list:
                self._list_items.append(''.join(self._current).strip())
            self._current = []
        elif tag == 'ul':
            self._flush_list('• ')
            self._in_list = None
        elif tag == 'ol':
            self._flush_list_numbered()
            self._in_list = None
        elif tag in ('td', 'th'):
            if self._in_cell and self._cur_row is not None:
                text = ''.join(self._cur_cell).strip()
                self._cur_row.append((text, self._cur_is_th))
            self._in_cell = False
        elif tag == 'tr':
            if self._in_table and self._cur_row is not None:
                self._table_rows.append(self._cur_row)
                self._cur_row = None
        elif tag == 'table':
            if self._in_table:
                block = _table_rows_to_block(self._table_rows)
                if block:
                    self.blocks.append(block)
                self._in_table = False
                self._table_rows = []
                self._cur_row = None

    def handle_data(self, data):
        if self._in_cell:
            self._cur_cell.append(data)
        else:
            self._current.append(data)

    def _flush_text(self):
        text = ''.join(self._current).strip()
        if text:
            self.blocks.append(make_text_block(text))
        self._current = []

    def _flush_list(self, prefix):
        for item in self._list_items:
            self.blocks.append(make_text_block(f"{prefix}{item}"))
        self._list_items = []

    def _flush_list_numbered(self):
        for i, item in enumerate(self._list_items, 1):
            self.blocks.append(make_text_block(f"{i}. {item}"))
        self._list_items = []


def _table_rows_to_block(rows):
    """将 [(text, is_th), ...] 的二维列表转为 _kind='table' 块标记。"""
    if not rows:
        return None
    col_size = max((len(r) for r in rows), default=0)
    if col_size == 0:
        return None
    norm_rows = []
    for r in rows:
        cells = [t for (t, th) in r]
        while len(cells) < col_size:
            cells.append("")
        norm_rows.append(cells)
    header_row = any(th for (t, th) in rows[0])
    return {
        "_kind": "table",
        "row_size": len(norm_rows),
        "column_size": col_size,
        "header_row": header_row,
        "rows": norm_rows,
    }


def convert_html_to_blocks(html_content):
    """入口：CMS HTML → 飞书块列表"""
    clean = html_content.replace('\\n', '\n').replace('\r', '')
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
    parser = FeishuBlockParser()
    parser.feed(clean)
    # Flush any remaining text
    parser._flush_text()
    return parser.blocks


# ═══════════════════════════════════════════════════════════════════
# Block Preview (--dry-run)
# ═══════════════════════════════════════════════════════════════════

BLOCK_TYPE_NAMES = {2: "text", 4: "h2", 5: "h3", 6: "h4",
                    12: "ul", 14: "ol", 27: "image", 31: "table"}

def preview_blocks(articles, output_file=None):
    """预览模式：展示转换结果，不写入 Wiki。

    articles: [{title, content}, ...]
    output_file: 可选，保存预览到文件
    """
    lines = []
    lines.append(f"{'='*70}")
    lines.append(f"  🧪 预览模式 — {len(articles)} 篇文章")
    lines.append(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"{'='*70}")

    total_blocks = 0
    type_counts = {}

    for i, a in enumerate(articles):
        title = a["title"]
        blocks = convert_html_to_blocks(a["content"])
        total_blocks += len(blocks)

        lines.append(f"\n── [{i+1}/{len(articles)}] {title} ({len(blocks)} blocks)")

        for j, b in enumerate(blocks):
            if b.get("_kind") == "table":
                bt_name = f"table{b.get('row_size')}x{b.get('column_size')}"
                text = f"[header_row={b.get('header_row')}]"
            else:
                bt = b.get("block_type", "?")
                bt_name = BLOCK_TYPE_NAMES.get(bt, f"type_{bt}")
                # Extract preview text
                if bt in (4, 5, 6):
                    level = {4: 2, 5: 3, 6: 4}[bt]
                    text = b.get(f"heading{level}", {}).get("elements", [{}])[0].get("text_run", {}).get("content", "")
                elif bt == 2:
                    elements = b.get("text", {}).get("elements", [])
                    text = "".join(e.get("text_run", {}).get("content", "") for e in elements)
                else:
                    text = "(block)"

            type_counts[bt_name] = type_counts.get(bt_name, 0) + 1

            # Truncate long text
            if len(text) > 80:
                text = text[:77] + "..."

            lines.append(f"  [{bt_name:5s}] {text}")

    lines.append(f"\n{'='*70}")
    lines.append(f"  📊 统计: {len(articles)} 篇, {total_blocks} 个块")
    for t, c in sorted(type_counts.items()):
        lines.append(f"      {t:5s}: {c:>4d}")
    lines.append(f"{'='*70}")

    output = "\n".join(lines)
    print(output)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\n📝 预览已保存到: {output_file}")

    return output


# ═══════════════════════════════════════════════════════════════════
# State Management (断点续传)
# ═══════════════════════════════════════════════════════════════════

def _make_state_file(state_dir="."):
    """生成状态文件名"""
    return os.path.join(state_dir, "paste_state.json")


def save_state(state_file, articles, wiki_urls, params, failed_indices, success_count):
    """保存当前进度到状态文件"""
    state = {
        "version": "2.1.0",
        "timestamp": datetime.now().isoformat(),
        "total": len(articles),
        "success": success_count,
        "failed": sorted(failed_indices),
        "articles": articles,
        "wiki_urls": wiki_urls,
        "params": params
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_state(state_file):
    """加载进度状态"""
    if not os.path.exists(state_file):
        return None
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════
# Wiki API
# ═══════════════════════════════════════════════════════════════════

def create_wiki_node_and_write(token, space_id, parent_node, title, blocks):
    """创建 Wiki 节点并写入内容块。

    返回 (node_token, obj_token, error_str)
    - 成功时 error_str 为 None
    - 失败时 node_token/obj_token 为空字符串
    """
    # 1. 创建节点
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"obj_type": "docx", "parent_node_token": parent_node,
              "node_type": "origin", "title": title}, timeout=15
    )
    r = resp.json()
    if r.get("code") != 0:
        return "", "", f"Create node failed: {r.get('code')} {r.get('msg')}"

    obj_token = r["data"]["node"]["obj_token"]
    node_token = r["data"]["node"]["node_token"]

    # 2. 含表格 → descendant 端点一次性建整棵子树
    if any(b.get("_kind") == "table" for b in blocks):
        err = _write_descendant(token, obj_token, blocks)
        return node_token, obj_token, err

    # 3. 无表格 → 原 children 平铺端点（已验证稳定）
    #    ⚠️ 飞书 children 单次请求上限 50 块：超过会报 99992402 field validation failed
    #    （2026-08-31 石家庄爱尔 6 篇 52~67 块无表格文章全失败定位到该限制）→ 分批写入
    CHUNK = 50
    r2 = {"code": -1}
    ok_all = True
    written = 0
    for ci in range(0, len(blocks), CHUNK):
        chunk = blocks[ci:ci + CHUNK]
        for attempt in range(3):
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}/children",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"children": chunk, "index": written}, timeout=30
            )
            r2 = resp.json()
            if r2.get("code") == 0:
                break
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
        if r2.get("code") != 0:
            ok_all = False
            break
        written += len(chunk)
    if ok_all:
        return node_token, obj_token, None

    return node_token, obj_token, f"Write blocks (3 retries): {r2.get('code')} {r2.get('msg')}"


def _write_descendant(token, obj_token, blocks):
    """含表格时，用 /descendant 端点一次性建整棵子树（表格+单元格+内容）。

    飞书 /children 端点不支持嵌套结构（表格块内套单元格），必须走
    /descendant：children_id 列顶层块临时 id，descendants 平铺所有块
    （段落/标题 + 表格块 + 单元格(32) + 单元格文本(2)），由飞书组装树。
    返回 None 表示成功，否则返回错误串。
    """
    children_id = []
    descendants = []
    for b in blocks:
        if b.get("_kind") == "table":
            tid = f"tbl_{len(children_id)}"
            flat_cell_ids = []
            for r in range(b["row_size"]):
                for c in range(b["column_size"]):
                    cell_text = ""
                    if r < len(b["rows"]) and c < len(b["rows"][r]):
                        cell_text = b["rows"][r][c] or ""
                    cid = f"{tid}_c_{r}_{c}"
                    ctid = f"{tid}_t_{r}_{c}"
                    content = make_text_block(cell_text)
                    content["block_id"] = ctid
                    content["children"] = []
                    descendants.append(content)
                    descendants.append({
                        "block_id": cid, "block_type": 32,
                        "table_cell": {}, "children": [ctid],
                    })
                    flat_cell_ids.append(cid)
            descendants.append({
                "block_id": tid,
                "block_type": 31,
                "children": flat_cell_ids,
                "table": {
                    "property": {
                        "row_size": b["row_size"],
                        "column_size": b["column_size"],
                        "column_width": [120] * b["column_size"],
                        "header_row": b.get("header_row", False),
                    }
                },
            })
            children_id.append(tid)
        else:
            pid = f"blk_{len(children_id)}"
            nb = dict(b)
            nb["block_id"] = pid
            nb["children"] = []
            descendants.append(nb)
            children_id.append(pid)

    for attempt in range(3):
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}/descendant",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"children_id": children_id, "descendants": descendants, "index": 0}, timeout=30
        )
        r2 = resp.json()
        if r2.get("code") == 0:
            return None
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))

    return f"Write descendant (3 retries): {r2.get('code')} {r2.get('msg')}"


def fill_spreadsheet(token, spreadsheet_token, sheet_id, urls, start_row, col_letter="B"):
    """将 URL 列表填入飞书表格。

    - col_letter: A/B/C/D... 列标
    - start_row: 起始行号
    """
    values = [[u] for u in urls if u]
    end_row = start_row + len(values) - 1
    resp = requests.put(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueRange": {
            "range": f"{sheet_id}!{col_letter}{start_row}:{col_letter}{end_row}",
            "values": values
        }}, timeout=15
    )
    return resp.json().get("code") == 0


# ═══════════════════════════════════════════════════════════════════
# Batch Main
# ═══════════════════════════════════════════════════════════════════

def batch_paste(articles, token, space_id, parent_node,
                spreadsheet_token=None, sheet_id=None, start_row=1, col_letter="B",
                dry_run=False, state_file=None, retry_failed_indices=None):
    """批量粘贴主流程。

    articles: [{title, content}, ...]
    dry_run: 仅预览，不写入 Wiki
    state_file: 断点续传状态文件路径
    retry_failed_indices: 仅重试指定索引的文章（0-based）
    """
    if dry_run:
        return preview_blocks(articles)

    # ─── 断点续传：从状态文件恢复 ───
    wiki_urls = [""] * len(articles)
    success = 0
    failed_indices = set()

    # 确定要处理的文章索引
    if retry_failed_indices is not None:
        # 重试模式：先加载已有状态
        target_indices = set(retry_failed_indices)
        if state_file and os.path.exists(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                prev = json.load(f)
            wiki_urls = prev.get("wiki_urls", [""] * len(articles))
            success = prev.get("success", 0)
            # 确保 wiki_urls 长度匹配
            if len(wiki_urls) < len(articles):
                wiki_urls.extend([""] * (len(articles) - len(wiki_urls)))
        print(f"🔄 重试模式: {len(target_indices)} 篇失败文章")
    else:
        target_indices = set(range(len(articles)))

    # ─── 逐篇处理 ───
    print(f"📄 {len(articles)} 篇")
    total = len(target_indices)
    done = 0

    for i in range(len(articles)):
        if i not in target_indices:
            continue

        a = articles[i]
        title = a["title"]
        done += 1
        print(f"\n[{done}/{total}] {title}")

        # 转换 HTML
        try:
            blocks = convert_html_to_blocks(a["content"])
        except Exception as e:
            print(f"  ❌ HTML 转换失败: {e}")
            failed_indices.add(i)
            if state_file:
                save_state(state_file, articles, wiki_urls,
                           {"space_id": space_id, "parent_node": parent_node,
                            "spreadsheet_token": spreadsheet_token,
                            "sheet_id": sheet_id, "start_row": start_row,
                            "col_letter": col_letter},
                           failed_indices, success)
            continue

        print(f"  → {len(blocks)} blocks")

        # 创建 Wiki 节点并写入
        try:
            node_token, _, error = create_wiki_node_and_write(
                token, space_id, parent_node, title, blocks
            )
        except Exception as e:
            error = str(e)

        if error:
            print(f"  ❌ {error}")
            failed_indices.add(i)
        else:
            wiki_urls[i] = f"{feishu_wiki_domain()}/{node_token}"
            print(f"  ✅ {wiki_urls[i]}")
            success += 1

        # 保存状态
        if state_file:
            save_state(state_file, articles, wiki_urls,
                       {"space_id": space_id, "parent_node": parent_node,
                        "spreadsheet_token": spreadsheet_token,
                        "sheet_id": sheet_id, "start_row": start_row,
                        "col_letter": col_letter},
                       failed_indices, success)

        time.sleep(0.3)

    # ─── 汇总 ───
    print(f"\n{'='*50}")
    print(f"  ✅ 成功: {success}/{total}")
    if failed_indices:
        print(f"  ❌ 失败: {sorted(failed_indices)}")
        print(f"  💡 重试: python3 paste_utils.py --retry-failed")
    print(f"{'='*50}")

    # ─── 填表 ───
    if spreadsheet_token and sheet_id:
        valid = [u for u in wiki_urls if u]
        if valid and fill_spreadsheet(token, spreadsheet_token, sheet_id, valid, start_row, col_letter):
            print(f"📊 填表 {col_letter}{start_row}:{col_letter}{start_row+len(valid)-1} ✅")
        else:
            print("📊 填表 ❌")

    return wiki_urls


def retry_failed(state_file=None, token=None, space_id=None):
    """从状态文件读取失败列表，仅重试失败的文章。

    返回 (wiki_urls, success_count, remaining_failed)
    """
    if state_file is None:
        state_file = _make_state_file()

    if not os.path.exists(state_file):
        print("❌ 未找到状态文件，请先运行 batch_paste")
        return [], 0, []

    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)

    articles = state["articles"]
    failed = state["failed"]
    params = state["params"]
    wiki_urls = state.get("wiki_urls", [""] * len(articles))
    success = state.get("success", 0)

    if not failed:
        print("✅ 没有失败的文章需要重试")
        return wiki_urls, success, []

    print(f"🔄 重试 {len(failed)} 篇失败文章: {failed}")

    if token is None:
        token = get_token()
    if space_id is None:
        space_id = params.get("space_id", default_space_id())

    # 修复：让 batch_paste 只处理 failed 索引
    # 但 batch_paste 需要知道哪些已经成功了，避免重复处理
    return batch_paste(
        articles, token, space_id,
        params["parent_node"],
        params.get("spreadsheet_token"),
        params.get("sheet_id"),
        params.get("start_row", 1),
        params.get("col_letter", "B"),
        state_file=state_file,
        retry_failed_indices=failed
    )


# ═══════════════════════════════════════════════════════════════════
# 🔙 方向 B：Wiki 块 → HTML（用于 CMS 导入）
# ═══════════════════════════════════════════════════════════════════

def _block_text(block):
    """提取 bt=2 文本块内容，粗体包 **（用于表格单元格还原）。"""
    if not block:
        return ""
    elements = block.get("text", {}).get("elements", [])
    parts = []
    for e in elements:
        tr = e.get("text_run", {})
        content = tr.get("content", "")
        if tr.get("text_element_style", {}).get("bold"):
            parts.append(f"**{content}**")
        else:
            parts.append(content)
    return "".join(parts)


def _build_table_html(block, idmap):
    """将飞书表格块(31)转为 HTML <table>。

    cells 为行主序 cell id 列表；配合 root with_descendants 读取，单元格(32)
    与单元格文本(2) 都在 blocks 平铺列表里，用 idmap 反查单元格文本。
    """
    table = block.get("table", {}) or {}
    prop = table.get("property", {})
    row_size = prop.get("row_size", 0)
    col_size = prop.get("column_size", 0)
    cells = table.get("cells", [])
    if not cells or row_size == 0 or col_size == 0:
        return ""
    header_row = prop.get("header_row", False)
    rows_html = []
    idx = 0
    for r in range(row_size):
        cells_html = []
        for c in range(col_size):
            cid = cells[idx] if idx < len(cells) else None
            idx += 1
            cell_text = ""
            if cid and cid in idmap:
                cell = idmap.get(cid)
                if cell and cell.get("children"):
                    cell_text = _block_text(idmap.get(cell["children"][0]))
            tag = "th" if (header_row and r == 0) else "td"
            cells_html.append(f"<{tag}>{cell_text}</{tag}>")
        rows_html.append("<tr>" + "".join(cells_html) + "</tr>")
    return "<table>\n" + "\n".join(rows_html) + "\n</table>"


def feishu_blocks_to_html(blocks):
    """将飞书文档块列表转为 CMS HTML 字符串。

    ⚠️ 重要教训：
    - block_type=1（页面块）跳过：CMS 标题字段单独存，不应出现在正文
    - block_type=4/5/6 用 heading2/3/4 字段，不是 text 字段
    - 粗体文本在 text_run.text_element_style.bold 中标记
    - block_type=31 表格：经 root with_descendants 读取时，cell(32) 与
      单元格文本(2) 都平铺在 blocks 里；用 table.cells 顺序 + idmap
      反查单元格文本，重建 HTML <table>
    """
    idmap = {b.get("block_id"): b for b in blocks if b.get("block_id")}
    cell_text_ids = set()
    for b in blocks:
        if b.get("block_type") == 31:
            for cid in (b.get("table", {}) or {}).get("cells", []):
                cell = idmap.get(cid)
                if cell and cell.get("children"):
                    cell_text_ids.add(cell["children"][0])

    html_parts = []
    for block in blocks:
        bt = block.get("block_type")

        # ⚠️ 跳过 block_type=1（页面块 = 标题，CMS 已有 title 字段）
        if bt == 1:
            continue
        # 表格块：重建 HTML <table>
        if bt == 31:
            t = _build_table_html(block, idmap)
            if t:
                html_parts.append(t)
            continue
        # 单元格块(32)：由所属表格统一处理
        if bt == 32:
            continue
        # 单元格内容文本(2)：由所属表格统一处理
        if bt == 2 and block.get("block_id") in cell_text_ids:
            continue

        # 提取文本内容
        text = ""
        if bt == 4:  # heading2
            text = "".join(
                e.get("text_run", {}).get("content", "")
                for e in block.get("heading2", {}).get("elements", [])
            )
        elif bt == 5:  # heading3
            text = "".join(
                e.get("text_run", {}).get("content", "")
                for e in block.get("heading3", {}).get("elements", [])
            )
        elif bt == 6:  # heading4
            text = "".join(
                e.get("text_run", {}).get("content", "")
                for e in block.get("heading4", {}).get("elements", [])
            )
        elif bt == 2:  # text paragraph
            elements = block.get("text", {}).get("elements", [])
            parts = []
            for e in elements:
                tr = e.get("text_run", {})
                content = tr.get("content", "")
                style = tr.get("text_element_style", {})
                if style.get("bold"):
                    parts.append(f"<strong>{content}</strong>")
                else:
                    parts.append(content)
            text = "".join(parts)
        else:
            # 其他块类型（列表、图片等）暂跳过
            continue

        if not text.strip():
            continue

        # 生成 HTML 标签
        if bt == 4:
            html_parts.append(f"<h2>{text}</h2>")
        elif bt == 5:
            html_parts.append(f"<h3>{text}</h3>")
        elif bt == 6:
            html_parts.append(f"<h4>{text}</h4>")
        else:
            html_parts.append(f"<p>{text}</p>")

    return "\n".join(html_parts)


def get_wiki_articles(token, space_id, parent_node_token, page_size=50):
    """从 Wiki 目录获取子文章列表。

    返回 [{title, url, obj_token, node_token}, ...]
    """
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
        headers={"Authorization": f"Bearer {token}"},
        params={"parent_node_token": parent_node_token, "page_size": page_size},
        timeout=10
    )
    data = resp.json()
    items = data.get("data", {}).get("items", [])
    result = []
    for item in items:
        result.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "obj_token": item.get("obj_token", ""),
            "node_token": item.get("node_token", ""),
        })
    return result


def get_doc_blocks(token, doc_token, page_size=100, with_descendants=True):
    """获取飞书文档的所有块。

    返回 [{block_type, heading2?, heading3?, text?, ...}, ...]
    with_descendants=True（默认）：平铺返回所有块，包括表格单元格(32)与
      单元格文本(2)，配合 feishu_blocks_to_html 重建 <table> 必需。
    """
    blocks = []
    page_token = None
    while True:
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if with_descendants:
            params["with_descendants"] = "true"
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_token}/blocks",
            headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=15
        )
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        blocks.extend(items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")
    return blocks


def batch_wiki_to_cms(articles, token, space_id, cms_corp_id, cms_pkg_id,
                       cms_cookies=None, dry_run=False):
    """批量：Wiki 文章 → CMS 词包。

    ⚠️ 注意：CMS 无更新 API，修正需先删后建。
    需要浏览器 CDP 环境（已登录 CMS 且切换到目标公司）。

    ⚠️⚠️ 关键坑（2026-07 实测）：写入接口 POST /yunying/v1/articles，但
    界面「词包文章」列表读的是 GET /yunying/v1/creation/articles，且该列表
    **按当前登录公司 corp_id 过滤**。若 corp_id 传错，接口仍返回 code=0、
    按 id 也能 GET 到，但界面永远看不到！
    → cms_corp_id 必须是**真实 corp_id**，不能用用户随口给的编号。
      获取正确 corp_id 的可靠方法：在界面「人工撰稿」手动建 1 篇，然后
      GET /yunying/v1/articles/{新id} 读出其 corp_id 即为当前公司真实 id。
      （本函数只做读飞书+转HTML，真正写 CMS 见调用方的浏览器 fetch 循环，
       body: {title, content, corp_id, keyword_package_id, keyword_package_name, article_type:2}）

    articles: 来自 get_wiki_articles() 的列表
    token: 飞书 tenant_access_token
    cms_corp_id: CMS 真实公司 ID（务必先用 cms_discover_ids.py 反查核对）
    cms_pkg_id: 词包 ID（可先用 cms_discover_ids.py 反查）
    dry_run: 仅读取并转换，不写入 CMS

    返回 [{title, html, error?}, ...]
    """
    results = []
    for i, a in enumerate(articles):
        title = a["title"]
        print(f"[{i+1}/{len(articles)}] {title[:50]}...", end=" ", flush=True)
        try:
            blocks = get_doc_blocks(token, a["obj_token"])
            html = feishu_blocks_to_html(blocks)
            h2_count = sum(1 for b in blocks if b.get("block_type") == 4)
            h3_count = sum(1 for b in blocks if b.get("block_type") == 5)
            print(f"OK (h2={h2_count} h3={h3_count} blocks={len(blocks)})")
            results.append({"title": title, "html": html, "blocks": blocks})
        except Exception as e:
            print(f"FAIL: {e}")
            results.append({"title": title, "error": str(e)})

    return results

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="飞书 Wiki 批量粘贴 v2.1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 正常模式
  python3 paste_utils.py articles.json NODE_TOKEN SHEET_TOKEN SHEET_ID 21

  # 预览模式（不写入）
  python3 paste_utils.py articles.json NODE_TOKEN --dry-run

  # 预览并保存到文件
  python3 paste_utils.py articles.json NODE_TOKEN --dry-run -o preview.txt

  # 断点续传（自动保存状态）
  python3 paste_utils.py articles.json NODE_TOKEN SHEET_TOKEN SHEET_ID 21 --state

  # 重试失败的文章
  python3 paste_utils.py --retry-failed
        """
    )
    parser.add_argument("articles_json", nargs="?", help="文章 JSON 文件路径")
    parser.add_argument("parent_node", nargs="?", help="Wiki 父节点 node_token")
    parser.add_argument("spreadsheet_token", nargs="?", default=None, help="表格 token")
    parser.add_argument("sheet_id", nargs="?", default=None, help="表格 sheet_id")
    parser.add_argument("start_row", nargs="?", type=int, default=1, help="填表起始行")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入 Wiki")
    parser.add_argument("-o", "--output", help="预览输出文件")
    parser.add_argument("--state", action="store_true", help="启用断点续传（保存状态文件）")
    parser.add_argument("--state-file", default="paste_state.json", help="状态文件路径")
    parser.add_argument("--retry-failed", action="store_true", help="仅重试失败的文章")
    parser.add_argument("--col-letter", default="B", help="填表列标 (默认 B)")

    args = parser.parse_args()

    # ─── 重试模式 ───
    if args.retry_failed:
        retry_failed(state_file=args.state_file)
        sys.exit(0)

    # ─── 正常/预览模式 ───
    if not args.articles_json:
        parser.error("需要 articles.json 文件路径")

    if args.dry_run and not args.parent_node:
        # 预览模式允许不传 parent_node
        args.parent_node = "dry-run"

    if not args.parent_node:
        parser.error("需要 parent_node_token")

    with open(args.articles_json, encoding="utf-8") as f:
        articles = json.load(f)

    token = None if args.dry_run else get_token()
    space_id = default_space_id()

    if args.dry_run:
        preview_blocks(articles, output_file=args.output)
    else:
        state_file = args.state_file if args.state else None
        batch_paste(
            articles, token, space_id, args.parent_node,
            args.spreadsheet_token, args.sheet_id, args.start_row,
            col_letter=args.col_letter,
            state_file=state_file
        )