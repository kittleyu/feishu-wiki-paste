---
name: feishu-wiki-paste
description: |
  飞书 Wiki 批量粘贴文章 + 链接填表一条龙。
  支持双向：CMS→Wiki（HTML→飞书块）和 Wiki→CMS（飞书块→HTML）。
  当用户要求将文章粘贴到飞书知识库 Wiki 目录、或需要把文章链接填到飞书多维表格时使用。

  触发词：粘贴到飞书、发布到 Wiki、挂到知识库、填链接到表、批量发布、粘贴到CMS、导入词包
---

# 飞书 Wiki 批量粘贴 Skill v2.2.0

## 🎯 功能

1. 从 CMS 获取文章（浏览器自动化 / API）
2. 将多篇文章批量创建到飞书知识库 Wiki 指定目录下
3. 自动处理 CMS HTML → 飞书文档块格式转换（标题、粗体、列表等）
4. 将文章 Wiki 链接批量填入飞书多维表格
5. 🆕 **预览模式（`--dry-run`）**：写入前先查看块转换效果
6. 🆕 **失败重试 + 断点续传**：单篇失败不影响后续，支持 `--retry-failed` 重跑失败文章

---

## 📋 前置条件

- [ ] **Wiki 权限**：机器人能访问目标知识库（群权限绕路）
- [ ] **API 权限**：`docx:document`、`wiki:wiki`、`wiki:node:create`、`drive:drive`、`sheets:spreadsheet`
- [ ] **目标目录**：Wiki 目录的 `node_token`（或可搜索到）
- [ ] **表格位置**：如需填表，表格 URL 和行列范围

---

## 🔄 完整 SOP（5 步）

### 步骤 0：从 CMS 获取文章

#### 0.1 yunying.huiyouhua.com CMS（GEO 运营后台）

**登录**：浏览器导航到 `https://yunying.huiyouhua.com/?tab=customer-opt`，输入运营账号密码。

**定位客户**：
```js
// 搜索客户
fetch('/yunying/v1/corp?keyword=永安期货&page=1&page_size=2')
// 返回 data.corp_list[0].corp.id → corp_id（永安期货=3258）
```

**切换到 CMS 文章页**：
```
https://yunying.huiyouhua.com/cms-yunying.html?tab=articles
```

**先切换到目标公司**（点击页面左上角公司选择器，搜索并选中目标公司），再拉取文章：
```js
// 拉取文章列表（含完整 HTML 内容）
fetch('/yunying/v1/creation/articles?page=1&page_size=20')
// 返回 data.articles[] ，每篇含 {id, title, content}
```

**⚠️ 大体积数据提取**：当 evaluate 返回结果过大被截断时，用 Node.js CDP WebSocket 绕过：
```js
// 用 Node.js 的 ws 库连接 CDP → Runtime.evaluate → 写到本地文件
// 详见步骤 4 的 paste_utils.py
```

**保存到文件**：建议保存为 `/tmp/{company}_articles.json`，格式：
```json
[{"title": "...", "content": "<h2>...</h2>\n<p>...</p>"}, ...]
```

#### 0.2 其他 CMS 来源

- 用户直接提供 JSON 文件 → 读取
- 用户提供 URL → 用 `web_fetch` 或 `browser` 抓取

---

### 步骤 1：获取飞书 Token

```python
resp = requests.post(
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
    json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10
)
token = resp.json()["tenant_access_token"]
```

---

### 步骤 2：确认目标目录

```python
# 搜索目录
resp = requests.get(
    f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{SPACE_ID}/nodes",
    headers={"Authorization": f"Bearer {token}"},
    params={"parent_node_token": PARENT_TOKEN}
)
```

---

### 步骤 3：创建 Wiki 节点 + 写入内容块

**关键原则**：
- 创建 Wiki 节点时 `obj_type` 必须用 `"docx"`（**不是** `"doc"`，`"doc"` 已废弃，报错 `131010`）
- 创建后立即写入内容块，否则文档为空

```python
# 1. 创建 Wiki 节点
resp = requests.post(
    f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{SPACE_ID}/nodes",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={
        "obj_type": "docx",          # ⚠️ 必须是 docx，不是 doc
        "parent_node_token": TARGET_NODE_TOKEN,
        "node_type": "origin",
        "title": "文章标题"
    }
)
r = resp.json()
obj_token = r["data"]["node"]["obj_token"]   # 底层文档 ID
node_token = r["data"]["node"]["node_token"]  # Wiki 节点 token

# 2. 写入内容块
resp = requests.post(
    f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}/children",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={"children": blocks, "index": 0}
)
```

---

### 步骤 4：CMS HTML → 飞书块转换（⚠️ 核心难点）

#### 4.1 必须使用 HTMLParser，禁止正则

**❌ 错误做法**：用 `re.split(r'(<h2>|<p>|...)', html)` 正则切割 HTML
- 标签属性导致匹配失败（如 `<h2 id="heading">`）
- 嵌套标签内容丢失（如 `<li><strong>xxx</strong>yyy</li>`）
- 残留标签文本污染（如 `h2` 字面量出现在内容中）

**✅ 正确做法**：使用 Python 标准库 `html.parser.HTMLParser`

```python
from html.parser import HTMLParser

class FeishuBlockParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self._current = []
        self._tag = None
        self._bold = False
        self._list_items = []
        self._in_list = None  # 'ul' or 'ol'

    def handle_starttag(self, tag, attrs):
        if tag in ('h2', 'h3', 'h4'):
            self._flush()
            self._tag = tag
        elif tag == 'strong':
            self._bold = True
        elif tag == 'ul':
            self._flush()
            self._in_list = 'ul'
        elif tag == 'ol':
            self._flush()
            self._in_list = 'ol'
        elif tag == 'li':
            self._current = []

    def handle_endtag(self, tag):
        if tag in ('h2', 'h3', 'h4'):
            self._flush_heading(int(tag[1]))
            self._tag = None
        elif tag == 'strong':
            self._bold = False
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
        elif tag == 'p':
            self._flush_text()
            self._tag = None

    def handle_data(self, data):
        self._current.append(data)

    def _flush(self):
        if self._tag == 'p':
            self._flush_text()
        elif self._tag and self._tag.startswith('h'):
            pass  # waiting for end tag
        self._tag = None

    def _flush_text(self):
        text = ''.join(self._current).strip()
        if text:
            self.blocks.append(make_text_block(text))
        self._current = []

    def _flush_heading(self, level):
        text = ''.join(self._current).strip()
        if text:
            self.blocks.append(make_heading_block(level, text))
        self._current = []

    def _flush_list(self, prefix):
        for item in self._list_items:
            self.blocks.append(make_text_block(f"{prefix}{item}"))
        self._list_items = []

    def _flush_list_numbered(self):
        for i, item in enumerate(self._list_items, 1):
            self.blocks.append(make_text_block(f"{i}. {item}"))
        self._list_items = []
```

#### 4.2 块类型速查表

| 来源 | 飞书 block_type | 字段名 | 备注 |
|------|-----------------|--------|------|
| `<h2>` | 4 | `heading2` | |
| `<h3>` | 5 | `heading3` | |
| `<h4>` | 6 | `heading4` | |
| `<p>` 段落 | 2 | `text` | |
| `<strong>` 粗体 | — | 在 text_run 中设 `bold: True` | 不是独立块 |
| `<ul>/<ol>` 列表 | **2**（非 14/16） | `text` | ⚠️ 见下文 |

#### 4.3 ⚠️ 列表块：禁止使用 block_type 14/16

**飞行验证的教训**：`block_type: 14`（bullet）和 `block_type: 16`（ordered）在实际写入时触发 `field validation failed` 错误。

**唯一可行方案**：用 `block_type: 2`（text）纯文本块，手动加前缀：
- 无序列表：`"• 列表项内容"`
- 有序列表：`"1. 列表项内容"`

```python
# ✅ 正确：用 text 块模拟列表
{
    "block_type": 2,
    "text": {
        "elements": [{"text_run": {
            "content": "• 持牌资质：这是选择期货服务商的首要前提...",
            "text_element_style": style_block()
        }}],
        "style": {"align": 1, "folded": False}
    }
}
```

#### 4.4 标准块格式

```python
def style_block(bold=False):
    return {"bold": bold, "inline_code": False, "italic": False,
            "strikethrough": False, "underline": False}

def make_heading_block(level, text):
    return {
        "block_type": {2: 4, 3: 5, 4: 6}[level],
        f"heading{level}": {
            "elements": [{"text_run": {"content": text, "text_element_style": style_block()}}],
            "style": {"align": 1, "folded": False}
        }
    }

def make_text_block(text):
    """支持 <strong> 粗体解析"""
    elements = []
    # 按 ** 分割（预先将 HTML <strong> 转为 ** 标记）
    for part in parse_bold_text(text):
        elements.append({"text_run": part})
    return {
        "block_type": 2,
        "text": {"elements": elements, "style": {"align": 1, "folded": False}}
    }
```

**⚠️ 每个 text_run 的 `text_element_style` 必须包含全部 5 个布尔字段**，缺一不可，否则报 `1770001 invalid param`。

#### 4.5 粗体处理

HTML `<strong>xxx</strong>` → 转为 `**xxx**`，然后按 `**` 分割成 elements：
```python
# 在 HTMLParser.handle_starttag('strong') 时追加 "**" 到 _current
# 在 handle_endtag('strong') 时追加 "**" 到 _current
# 在 make_text_block 时按 ** 分割，交替设置 bold=True/False
```

#### 4.6 内容清洗

```python
# 去除 HTML 实体
html_content = html_content.replace('\\n', '\n')
# 去除不可见控制字符
html_content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', html_content)
```

---

### 🆕 步骤 4.5：预览模式（--dry-run）⚠️ 强烈建议

**在正式写入 Wiki 之前，先用预览模式检查转换效果。**

```bash
# 预览模式（仅打印到终端）
python3 paste_utils.py articles.json NODE_TOKEN --dry-run

# 预览并保存到文件
python3 paste_utils.py articles.json NODE_TOKEN --dry-run -o preview.txt
```

预览输出示例：
```
======================================================================
  🧪 预览模式 — 20 篇文章
  时间: 2026-07-10 18:30:00
======================================================================

── [1/20] 新手选择期货服务商常见核心痛点 (15 blocks)
  [h2   ] 新手选择期货服务商常见核心痛点
  [text  ] 在期货市场，选择一家合适的期货服务商是...
  [h3   ] 核心痛点一：担心服务商不正规
  [text  ] 1. 期货服务商是否正规可以通过证监会官网查询
  [text  ] 2. 正规期货公司名称中必含"期货"二字
  ...

── [2/20] 期货交易入门指南 (12 blocks)
  ...

======================================================================
  📊 统计: 20 篇, 287 个块
      h2   :   20
      h3   :   45
      text :  222
======================================================================
```

**⚠️ 预览时重点检查**：
- 是否有 `h2` / `h3` 标签残留文字
- 列表项是否完整（`•` 或 `1.` 开头）
- 粗体 `**` 标记是否成对出现
- 内容是否有明显缺失（对比 CMS 原文）

---

### 🆕 步骤 4.6：失败重试 + 断点续传

**背景**：批量写入 20 篇时，偶尔第 N 篇失败（网络抖动、飞书 API 瞬时错误等），v2.1.0 后自动跳过继续写后续篇，最后汇总失败列表。

#### 启用断点续传

```bash
# 加 --state 参数，每篇写入后自动保存进度
python3 paste_utils.py articles.json NODE_TOKEN SHEET_TOKEN SHEET_ID 21 --state
```

成功输出：
```
📄 20 篇

[1/20] 文章A
  → 15 blocks
  ✅ https://vcnd134o0gra.feishu.cn/wiki/xxx

[2/20] 文章B
  → 12 blocks
  ❌ Write blocks: 99992402 field validation

[3/20] 文章C
  → 10 blocks
  ✅ https://vcnd134o0gra.feishu.cn/wiki/yyy

==================================================
  ✅ 成功: 18/20
  ❌ 失败: [1, 5]
  💡 重试: python3 paste_utils.py --retry-failed
==================================================
```

#### 重试失败文章

```bash
# 自动从 paste_state.json 读取，只重试失败的
python3 paste_utils.py --retry-failed
```

也可以指定状态文件：
```bash
python3 paste_utils.py --retry-failed --state-file my_state.json
```

**状态文件格式** (`paste_state.json`)：
```json
{
  "version": "2.1.0",
  "timestamp": "2026-07-10T18:30:00",
  "total": 20,
  "success": 18,
  "failed": [1, 5],
  "articles": [...],
  "wiki_urls": {...},
  "params": {...}
}
```

---

### 步骤 5：填表

```python
resp = requests.put(
    f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={
        "valueRange": {
            "range": f"{SHEET_ID}!B{start}:B{end}",
            "values": [["https://..."], ...]
        }
    }
)
```

- 表格 URL → 提取 `SPREADSHEET_TOKEN` 和 `SHEET_ID`
- 每行一个 `[url]`，共 N 行

---

## ⚠️ 常见错误速查（实战验证）

| 错误码 / 现象 | 含义 | 根因 | 解决 |
|--------------|------|------|------|
| `131010` | doc type deprecated | `obj_type: "doc"` | 改为 `obj_type: "docx"` |
| `99992402` | field validation | 缺少 `obj_type` 字段 | 添加 `"obj_type": "docx"` |
| `1770001` | invalid param | 块格式错误 | 检查 `text_element_style` 5 字段全不全；`block_type` 是否正确 |
| `field validation failed`（写块时） | 块类型不支持 | 用了 `block_type: 14/16` | 改用 `block_type: 2` + 手动前缀 |
| `##` / `h2` 文字出现在正文 | 正则解析残留 | 用 `re.split()` 切 HTML 标签不稳定 | 改用 `HTMLParser` |
| 内容缺失 | 正则丢内容 | 嵌套标签被错误切割 | 改用 `HTMLParser` |
| 列表项内容丢失 | 正则丢 `<li>` 内 `<strong>` | `<li><strong>xxx</strong></li>` 只提取到空 | 先收集 raw HTML 再 strip tags |
| 401 Unauthorized（curl） | 无 Cookie | curl 没带认证 | 用浏览器 CDP 发请求，不要用 curl |
| 权限拒绝 | 无权访问知识库 | 机器人未加入知识库 | 群权限绕路方案 |
| 节点无内容 | 创建后文档为空 | 创建 Wiki 节点时生成了空文档 | 往节点的 `obj_token` 写内容块 |
| 粗体不显示 | bold 未设置 | 整段当纯文本 | 用 `<strong>` → `**` → 分割设 bold |
| 部分文章写入失败 | 单篇 API 错误 | 网络抖动/瞬时错误 | 🆕 自动跳过 + `--retry-failed` 重试 |
| 🆕 重复创建文章 | 同名文章已存在 | 上次运行中断后重跑 | 用 `--dry-run` 预览 + 检查 paste_state.json |
| 🆕 **Wiki→CMS 小标题丢失** | 正文无 h2/h3 | 只读了 `text` 字段，标题在 `heading2`/`heading3` 字段 | 按 `block_type` 读取对应字段（4→heading2, 5→heading3） |
| 🆕 **Wiki→CMS 标题重复** | 正文出现重复标题 | `block_type=1`（页面块）被渲染到正文 | 跳过 `block_type=1`，CMS 标题字段单独存 |
| 🆕 **CMS 更新 API 404** | PUT/PATCH 不生效 | CMS 无更新接口 | `DELETE` 后 `POST` 重建 |
| 🆕 **CDP evaluate 批量请求超时** | 逐条 WebSocket 发请求超时 | 多次 evaluate 往返延迟大 | 将所有 fetch 放入单个 evaluate 表达式，在浏览器上下文内循环执行 |

---

## 🛠️ 可复用脚本

脚本路径：`skills/feishu-wiki-paste/paste_utils.py`

核心函数：
- `get_token()` — 获取飞书 tenant_access_token
- `create_wiki_node_and_write(token, parent_node, title, blocks)` — 创建节点+写内容（支持 3 次重试），返回 (node_token, obj_token, error)
- `fill_spreadsheet(token, urls, start_row, col)` — 填表，按列索引
- `convert_html_to_blocks(html_content)` — CMS HTML → 飞书块列表（基于 HTMLParser）
- `preview_blocks(articles, output_file=None)` — 🆕 预览模式，显示块转换结果
- `batch_paste(articles, token, parent_node, dry_run=False, state_file=None, retry_failed_indices=None)` — 批量主流程
- `retry_failed(state_file=None)` — 🆕 从状态文件读取失败列表，仅重试失败文章
- `save_state(state_file, articles, wiki_urls, params, failed, success)` — 🆕 保存断点续传状态

---

## 📝 用户提示词模板

### 方向 A（CMS→飞书Wiki）：

```
用户每次任务只需提供：
1. 客户名 + CMS 编号（如：永安期货 85901）
2. 目标 Wiki 目录 URL
3. 表格 URL + 行列范围（如：B21:B40）
```

示例：
> 把永安期货 85901 最新 20 篇文章粘贴到
> https://xxx.feishu.cn/wiki/ABCD 的 7.10 目录下，
> 链接填到 https://xxx.feishu.cn/wiki/SPREADSHEET 的 B21:B40

### 方向 B（飞书Wiki→CMS词包）：

```
用户每次任务只需提供：
1. 源 Wiki 目录 URL（含日期子目录）
2. 目标 CMS 公司 + 词包名
```

示例：
> 把 https://xxx.feishu.cn/wiki/ABCD 目录下 7.7 和 7.8 的文章粘贴到
> 云南约牛软件技术有限公司的约牛软件词包下

### 方向 B-2（链接填表）：

```
用户每次任务只需提供：
1. 源 Wiki 目录 URL
2. 目标表格 URL + 行列范围
```

示例：
> 把 https://xxx.feishu.cn/wiki/ABCD 目录的文章链接填到
> https://xxx.feishu.cn/wiki/SPREADSHEET 的 B列 41~70行

---

## 🔙 方向 B：Wiki → CMS（反向粘贴）⚠️ 实战验证

### 概述

将 Wiki 目录下的文章批量导入到 CMS 词包。**核心流程**：
1. 飞书 API 读取 Wiki 目录下的文章列表（`GET /wiki/v2/spaces/{id}/nodes`）
2. 读取每篇文章的飞书文档块（`GET /docx/v1/documents/{token}/blocks`）
3. **飞书块 → HTML 转换**（逆向：heading2→h2, text→p 等）
4. 通过 CMS API 创建文章（`POST /yunying/v1/articles`）

### ⚠️ 关键教训

| 教训 | 详情 |
|------|------|
| **CMS 无更新 API** | `PUT`/`PATCH` `/yunying/v1/articles/{id}` 均返回 404，需 `DELETE` 后重建 |
| **标题块字段是 heading2/heading3** | 不是 `text` 字段！`block_type=4` 用 `heading2`，`block_type=5` 用 `heading3` |
| **block_type=1 是页面标题** | 必须跳过，否则 CMS 正文出现重复标题（CMS 标题字段已单独存） |
| **CMS 创建 API 是 POST /yunying/v1/articles** | 需 `{title, content, corp_id, keyword_package_id}`，返回 `{code, data: {id}}` |

### 步骤 B1：定位 CMS 词包

先在 CMS 页面切换到目标公司，再查看词包列表获取 `keyword_package_id`。

已知词包 ID（已验证）：
| 公司 | corp_id | 词包名 | keyword_package_id |
|------|---------|--------|-------------------|
| 云南约牛 | 2732 | 约牛软件 | 4103 |
| 上海利多星 | 3726 | 智能股票软件推荐 | 6407 |
| 永安期货 | 3258 | — | — |

### 步骤 B2：读取 Wiki 文章列表

```python
resp = requests.get(
    "https://open.feishu.cn/open-apis/wiki/v2/spaces/{SPACE_ID}/nodes",
    headers={"Authorization": f"Bearer {token}"},
    params={"parent_node_token": NODE_TOKEN, "page_size": 50}
)
items = resp.json()["data"]["items"]
# 每项: {title, node_token, obj_token, url, has_child}
```

### 步骤 B3：飞书块 → HTML 转换（⚠️ 核心）

```python
def feishu_blocks_to_html(blocks):
    """将飞书文档块列表转为 CMS HTML"""
    html_parts = []
    for block in blocks:
        bt = block.get("block_type")
        
        # ⚠️ 跳过 block_type=1（页面块，本身就是标题，CMS 标题字段单独存）
        if bt == 1:
            continue
        
        # 提取文本内容
        text = ""
        if bt == 4:  # heading2
            text = "".join(e.get("text_run", {}).get("content", "")
                          for e in block.get("heading2", {}).get("elements", []))
        elif bt == 5:  # heading3
            text = "".join(e.get("text_run", {}).get("content", "")
                          for e in block.get("heading3", {}).get("elements", []))
        elif bt == 6:  # heading4
            text = "".join(e.get("text_run", {}).get("content", "")
                          for e in block.get("heading4", {}).get("elements", []))
        elif bt == 2:  # text
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
```

**块类型 ↔ 字段映射（读方向）**：

| block_type | 字段名 | HTML 标签 |
|-----------|--------|-----------|
| 1 | `page` | ⚠️ 跳过（CMS 标题字段单独存） |
| 2 | `text` | `<p>` |
| 4 | `heading2` | `<h2>` |
| 5 | `heading3` | `<h3>` |
| 6 | `heading4` | `<h4>` |

### 步骤 B4：CMS 创建文章

```python
# 通过浏览器 CDP（需要已登录 CMS 且切换到目标公司）
resp = await fetch("/yunying/v1/articles", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
        title: "文章标题",
        content: "<h2>小标题</h2>\n<p>正文内容...</p>",
        corp_id: 2732,           # 公司 ID
        keyword_package_id: 4103  # 词包 ID
    })
})
data = await resp.json()
# 返回: {code: 0, data: {id: 230648}, message: "操作成功"}
```

### 步骤 B5：CMS 删除文章（修正用）

```python
# CMS 无更新 API，修正需先删后建
resp = await fetch("/yunying/v1/articles/{article_id}", {
    method: "DELETE"
})
data = await resp.json()
# 返回: {code: 0, message: "操作成功"}
```

### 步骤 B6：批量执行策略

**⚠️ 必须用浏览器 CDP WebSocket 批量调用**，不能在单个 evaluate 中逐条发请求（会超时）。

```javascript
// ✅ 正确：将所有 calls 放入一个 evaluate，在浏览器上下文中循环 fetch
const calls = articles.map(a => ({
    title: a.title, content: a.html,
    corp_id: CORP_ID, keyword_package_id: PKG_ID
}));

const expression = `(async()=>{
    var calls = ${JSON.stringify(calls)};
    var results = [];
    for (var i = 0; i < calls.length; i++) {
        var r = await fetch("/yunying/v1/articles", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(calls[i])
        });
        var d = await r.json();
        results.push({index:i, code:d.code, id:d.data?.id, title:calls[i].title.substring(0,50)});
    }
    return JSON.stringify(results);
})()`;
```

---

## 🔄 双向链路图

```
┌──────────┐    方向 A：CMS → 飞书     ┌──────────────┐
│  CMS     │ ──────────────────────→ │  飞书 Wiki    │
│ (来源)   │                          │  (编辑/审核)  │
└──────────┘                          └──────┬───────┘
    ↑                                        │
    │        方向 B：飞书 → CMS              │
    └────────────────────────────────────────┘
```

| 阶段 | 方向 | 操作 | 工具 |
|------|------|------|------|
| 1 | A | 从 CMS 获取文章 | CMS API（浏览器 CDP） |
| 2 | A | HTML→飞书块转换 | `HTMLParser` |
| 3 | A | 创建 Wiki 节点 + 写内容 | `POST /wiki/.../nodes` + `POST /docx/.../blocks` |
| 4 | A | Wiki 链接填表 | `PUT /sheets/.../values` |
| 5 | — | 编辑/审核（人工） | 飞书 App |
| 6 | B | 读取飞书文档 | `GET /docx/.../raw_content` |
| 7 | B | 清洗 + 发布到 CMS | CMS API / browser |
| 8 | B | CMS 链接回填 | `PUT /sheets/.../values` |

---

## 🔧 环境配置

| 参数 | 值 | 说明 |
|------|-----|------|
| APP_ID | `APP_ID_PLACEHOLDER` | 飞书应用 ID |
| APP_SECRET | `APP_SECRET_PLACEHOLDER` | 飞书应用密钥 |
| SPACE_ID | `7630734017544981692` | 知识库空间 ID |
| Token 端点 | `POST /open-apis/auth/v3/tenant_access_token/internal` | 获取 tenant access token |
| 速率限制 | ≥ 0.3s / 次 | 避免 429 |
| CMS 地址 | `https://yunying.huiyouhua.com` | GEO 运营后台 |
| CMS 文章 API | `POST /yunying/v1/articles` | 需 {title, content, corp_id, keyword_package_id} |
| CMS 文章列表 | `GET /yunying/v1/creation/articles?page=1&page_size=20` | 需先在页面切换公司 |
| CMS 删除文章 | `DELETE /yunying/v1/articles/{id}` | 用于修正（无更新 API） |

---

## 📋 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v2.2.0 | 2026-07-14 | 🆕 方向 B：Wiki→CMS 完整 SOP（飞书块→HTML 转换、CMS 创建/删除 API）；新增 5 条错误速查（标题丢失、重复、更新 API 404、批量超时）；新增 paste_utils 反向函数 |
| v2.1.0 | 2026-07-10 | 🆕 预览模式 (--dry-run)、失败重试 + 断点续传 (--state + --retry-failed) |
| v2.0.0 | 2026-07-10 | 核心修复：HTMLParser 替代正则；CMS 获取步骤；列表/粗体规范化 |
| v1.0.0 | 2026-07-09 | 初始版本：CMS→Wiki SOP + 块格式速查 |