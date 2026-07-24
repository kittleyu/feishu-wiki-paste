---
name: feishu-wiki-paste
description: |
  飞书 Wiki 批量粘贴文章 + 链接填表一条龙。
  支持双向：CMS→Wiki（HTML→飞书块）和 Wiki→CMS（飞书块→HTML）。
  当用户要求将文章粘贴到飞书知识库 Wiki 目录、或需要把文章链接填到飞书多维表格时使用。

  触发词：粘贴到飞书、发布到 Wiki、挂到知识库、填链接到表、批量发布、粘贴到CMS、导入词包
---

# 飞书 Wiki 批量粘贴 Skill v2.7.5

## 🎯 功能

1. 从 CMS 获取文章（浏览器自动化 / API）
2. 将多篇文章批量创建到飞书知识库 Wiki 指定目录下
3. 自动处理 CMS HTML → 飞书文档块格式转换（标题、粗体、列表等）
4. 将文章 Wiki 链接批量填入飞书多维表格
5. 🆕 **预览模式（`--dry-run`）**：写入前先查看块转换效果
6. 🆕 **失败重试 + 断点续传**：单篇失败不影响后续，支持 `--retry-failed` 重跑失败文章

---

## 🚀 最简用法（client_map 简写，v2.7.0+，日常 90% 场景）

把每个客户的「顶层目录 + 审核表」配进 `client_map.json` 一次后，**用户只需说公司名 + 篇数**，机器人自动：
① 找到该客户顶层飞书目录 → ② 在下面**新建/复用「当日日期 M.D」子目录** → ③ 拉 CMS 最新 N 篇贴进去 → ④ 把链接回填审核表（B 列链接、A 列合并写日期）。

**群内一句话（方向 A）：**
```
@机器人 永安期货 10篇
@机器人 中泰期货 20篇
@机器人 约牛最新十五篇          # 中文数字也认
@机器人 永安期货 5篇 自建个7.22目录   # 显式子目录，覆盖「当日日期」
```
> `client_map.json` 当前已配：永安期货 / 中泰期货 / 约牛 / 西安东大肛肠。加新客户只需告诉我
> 「公司名 → 目录链接 + 表格链接」，我补一行即可。**审核表是可选的**——配了才会自动填。

**也可在对话里/本地直接驱动**（不依赖 @机器人）：
```bash
python run_client.py 永安期货 10            # 默认建当日日期子目录
python run_client.py 中泰期货 20 --subdir 7.22   # 指定子目录
python run_client.py 约牛 10 --dry-run       # 只预览转换，不写不填
```

**其它方向的完整指令格式仍可用**（见下方「群内指令格式」）：方向 B（飞书→CMS 词包）、方向 F（目录链接→填表）、方向 G（AI 生文→粘贴→填表）。`client_map` 仅简化方向 A。

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
| 🔴 **接口 code=0 但 CMS 界面看不到** | corp_id 传错 | 列表按当前公司 corp_id 过滤，错公司名下不显示 | 用 `--verify-corp`/`--auto-corp` 反查真实 corp_id；先 `--preflight` 探路确认可见再铺开 |
| 🔴 **Chrome 起不来/调试端口不通** | 用默认 User Data 开远程调试 | Chrome 禁止默认配置+调试端口同用 | 复制 profile 到临时目录，`--user-data-dir=临时目录 --remote-debugging-port=9222`（已封装 `launch_chrome_debug.py`） |
| 🔴 **用户给的"公司编号"不是 corp_id** | 把内部编号当 corp_id | 84347≠真实 1155，界面筛不出 | 永远用脚本反查，不信任手写数字 |

---

## 🛠️ 可复用脚本

脚本路径：`skills/feishu-wiki-paste/`

### 核心模块 `paste_utils.py`（飞书侧）

核心函数：
- `get_token()` — 获取飞书 tenant_access_token
- `get_wiki_articles(token, space_id, node_token, page_size)` — 列出 Wiki 目录文章
- `get_doc_blocks(token, obj_token)` — 读飞书文档块
- `feishu_blocks_to_html(blocks)` — 飞书块 → CMS HTML（h2/h3/h4/p + 粗体）
- `batch_wiki_to_cms(articles, token, space_id, corp_id, pkg_id, dry_run)` — 方向 B 读+转 HTML（**只做读+转，CMS 写入见下方脚本**）
- `create_wiki_node_and_write(...)` / `batch_paste(...)` / `preview_blocks(...)` / `retry_failed(...)` — 方向 A（CMS→飞书）

### 方向 B 浏览器自动化脚本（CMS 写入侧，需 Playwright + Chrome 调试端口）

- **`launch_chrome_debug.py`** — 复制 Chrome 配置+启动带调试端口的 Chrome（解决坑 2）。`python launch_chrome_debug.py`
- **`direction_b_cms_write.py`** — 写 CMS + 校验落库。支持 `--verify-corp`(反查 corp_id)、`--preflight`(写1篇探路)、`--auto-corp`、`--delete`。
- **`cms_discover_ids.py`** — 通过 API 自动查公司/词包 ID：`python cms_discover_ids.py --company '公司全名' --pkg '词包名'` → 打印 corp_id/pkg_id（底层用 `GET /yunying/v1/corp/active` 列公司 + `GET /yunying/v1/auth/changecorp?corp_id=X` 切换 + `GET /yunying/v1/keyword/package` 取词包）。无需盲猜、无需点界面。
- **`run_direction_b.py`** — 方向 B 通用一键驱动（复用 feishu_collect + auto_paste.run_pipeline）：`python run_direction_b.py --node <飞书node> --company <公司> --pkg <词包>`。
- **`prepare_multi.py`** — 🆕 父目录取「指定子目录」文章：解析父目录子节点 → 模糊匹配子目录名（如 `6/29,6/30,7.1,7/2`，自动归一化分隔符）→ 逐层收集文档（跳过 folder 递归）→ `feishu_blocks_to_html` 转换 → 合并去重。`python prepare_multi.py --parent-token <父目录node> --subdirs "6/29,6/30,7.1,7/2" --out gui_articles.json`（仅飞书 API，不需 Chrome）。
- **`auto_paste.py`** — 🆕 全自动一条龙（方向 B，需账号密码）：启动/复用调试 Chrome（全新临时 profile，**不杀用户正常 Chrome**）→ 用 `运营账号` 登录 huiyouhua（GEO 选项卡）→ API 反查 corp_id/pkg_id → 先写 1 篇探路校验 → 全量写入并逐篇校验。账号密码经环境变量 `HYH_USER`/`HYH_PWD` 传入（**不落盘、不提交**）。**已参数化**：`HYH_USER=xxx HYH_PWD=yyy python auto_paste.py --company '公司全名' --pkg '词包名' --in gui_articles.json`（不再硬编码 NODE_TOKEN/COMPANY/PKG；`--user-corp-id` 可选仅交叉核对，强烈建议留空以 API 反查为准）。
- **`direction_a_cms_to_feishu.py`** — 🆕 **方向 A 全自动（CMS→飞书）**：登录 huiyouhua → 按公司名解析真实 corp_id（同名多公司时自动选「有文章且最多」的那个）→ `changecorp` → 拉 `creation/articles` 最新 N 篇（含完整 content，按 `created_at` 降序）→ 复用 `batch_paste` 写入目标飞书 Wiki 目录。`python direction_a_cms_to_feishu.py --company '中泰期货股份有限公司' --node <目录node> --limit 10`（支持 `--dry-run` 预览转换、`--space-id` 指定空间）。`find_corp_by_name()` / `run_pipeline_a()` 均可被 bot 直接调用。`direction_a` 结果 json 含 `urls`（飞书文档链接列表）与 `articles`（标题+正文）。
- **`fill_sheet.py`** — 🆕 **把链接回填到飞书电子表格（sheet）**：把 `direction_a` 结果里的 `urls` 追加写到指定「电子表格」某列（默认第二列=B），从已填单元格下一行开始。`python fill_sheet.py --node <表格wiki节点token> --links direction_a_cms_results.json --col 2`（支持 `--sheet '7月内容'` 指定工作表、`--dry-run` 先确认范围再写入）。⚠️ 注意：**电子表格(sheet, obj_type=sheet)** 与**多维表格(bitable)** 是两套 API——本脚本走 `sheets/v2`（metainfo 取 sheetId → values 读/写）；填多维表格请用 bitable API。飞书应用需开通 `sheets:spreadsheet` 权限并把表格分享给机器人应用。**同时暴露 `run_fill(node, urls, col, sheet_hint, token, dry_run)` 供 bot 方向 F 直接调用。**
- **`collect_d5nl_links.py`** — 🆕 **收集某飞书目录全部文章子页链接**：`list_nodes` 列目录子节点（只取 docx）→ 生成带 `urls` 字段的 json，供 `fill_sheet.py` 回填；也可单独用来核对目录下文章数。`python collect_d5nl_links.py --save`（写出 `yn_links.json`）/ 不带 `--save` 仅打印。
- **`feishu_bot.py`** — 🆕 飞书群聊机器人（**四向**：方向 A + 方向 B + 方向 F 填表 + 方向 G 生文）：基于飞书事件「长连接模式」(WebSocket) 本机常驻，群里 @它 发指令即完成粘贴。方向 B 复用 `feishu_collect()`+`run_pipeline()`；方向 A 复用 `run_pipeline_a()`（**支持 `client_map.json` 简写**：只给「公司名+篇数」即自动匹配目录 + 建当日日期子目录 + 填审核表）；方向 F 复用 `list_nodes()`+`fill_sheet.run_fill()`（把源目录文章链接填进表格指定列）。进度/结果回发群里。运行 `python feishu_bot.py`（保持窗口开着即在线），或双击 `start_bot.bat`。前置：飞书开放平台开启事件订阅「长连接模式」+ 添加 `im.message.receive_v1` + 权限 `im:message`/`im:message.group`/`im:message:send_as_bot`；`.env` 需含 `HYH_USER`/`HYH_PWD`。
- **`run_client.py`** — 🆕 方向 A 通用驱动（基于 `client_map.json`）：`python run_client.py <公司名> [数量] [--subdir 7.22] [--dry-run]`，等价于机器人方向 A 的完整流程（建日期子目录 + 贴文 + 填表），用于「在对话里/本地」直接驱动，无需 @机器人。

> 依赖：`pip install requests playwright`；CMS 写入需先 `python launch_chrome_debug.py` 让 Chrome 带调试端口且已登录 huiyouhua。

## 🤖 飞书群聊机器人（方向 B 自动执行）

让机器人常驻在群里，**直接 @它 发指令就完成粘贴**。基于飞书事件「长连接模式」(WebSocket)，**本机运行、无需公网地址/内网穿透**，飞书 SDK 自带加密鉴权。

### 飞书开放平台前置配置（一次性）
1. 应用 → **事件订阅** → 开启，订阅方式选 **「长连接模式」**
2. **添加事件** `im.message.receive_v1`（接收消息）
3. **权限管理**开通：`im:message`、`im:message.group`、`im:message:send_as_bot`
4. 机器人已加入目标群，且群已开**知识库权限**（读取 wiki 用）

### 本机运行
- 依赖：`lark-oapi`（`pip install lark-oapi`）+ 已有的 `requests`/`playwright`
- 凭证：`.env` 需含 `FEISHU_APP_ID/SECRET` 与 `HYH_USER/HYH_PWD`（huiyouhua 自动登录）
- 启动（推荐）：双击 `start_bot.bat` → 用 `pythonw` **无黑窗口常驻**，日志写 `bot_console.log`，彻底脱离终端/本会话（关掉终端也不掉线，最适合长期运行）。
- 启动（调试）：`python -u feishu_bot.py`（终端保持开着即在线，stdout 实时可见）。
- ⚠️ 只保持**一个**常驻实例即可；不要让多个终端/后台任务同时拉起 bot（重复启动会被单实例保护拒掉，见下行）。
- 🔁 **开机自启（推荐长期运行方式）**：把 `start_bot.bat` 的「镜像副本」`FeishuWikiPasteBot.bat`（绝对路径版、本机专用、不进 git）放进 Windows「启动」文件夹（`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`），用户**登录 Windows 时自动以 `pythonw` 无窗口拉起**，彻底脱离本会话/WorkBuddy，断网重连后也会自愈。无需管理员权限、不用任务计划程序。

### 群内指令格式（自然语言解析，双向均支持）
```
# 方向 B：飞书 → CMS 词包
@机器人 粘贴 <飞书目录链接> 到 <公司全名> <词包名>
@机器人 粘贴 <父目录链接> 下面 6/29 6/30 7.1 7/2 到 <公司全名> <词包名>

# 方向 A：CMS → 飞书 Wiki（最新 N 篇）
@机器人 把 <公司全名>(id可选) 粘贴到 <飞书目录链接> 最新10条

# 方向 A（client_map 简写，已配客户无需给链接，自动建当日日期子目录+填表）
@机器人 永安期货 10篇
@机器人 中泰期货 20篇
@机器人 约牛最新十五篇            # 中文数字也认
@机器人 永安期货 5篇 自建个7.22目录   # 显式子目录，覆盖「当日日期」

# 方向 F：把飞书目录的文章链接填进电子表格某列（默认第二列）
@机器人 把 <源目录链接> 的文章链接填到 <表格链接> 第二列
@机器人 把 <源目录链接> 的文章链接填到 <表格链接> 第二列 工作表 6月内容
```
- 方向 B：读飞书 → 反查公司/词包真实 ID → 登录 huiyouhua → 探路 1 篇 → 全量写入并逐篇校验 → 回发。
- 方向 A：登录 huiyouhua → 按公司名选有文章的 corp → 拉最新 N 篇 → 转飞书块 → 写入指定目录 → 回发链接。
- 方向 F：列源目录全部 docx 子页 → 取其飞书链接 → 追加填进目标电子表格（sheet）指定列（默认 B，从已填单元格下一行起）。自动去重无害（覆盖写同一格不会重复）。源必须是文件夹/docx 目录，目标必须是 sheet（飞书应用需 `sheets:spreadsheet` 权限+分享表格）。可选「工作表 X」指定具体 worksheet，否则默认选含「7月」或第一个。
- 方向 G（智能生文→粘贴→填表，全自动）：`@机器人 生文 <公司/产品> <关键词包> <数量> 粘贴到 <飞书目录链接> 填到 <表格链接>`，机器人**直接调 huiyouhua 后端接口**生文（不再模拟点击 UI，根治「切公司后提交瞬间被还原成默认公司、误给错公司批量生文」的事故）。流程：`corp/active` 按名定位 corp（同名多 corp 逐个探测）→ `auth/changecorp` 切换 → `knowledge-base` 取产品知识库 id → `keyword/package` 取词包 id（词包名称字段是 `core_keyword`，非 `name`）→ `POST /creation/tasks`(`product_kb_id`+`keyword_package_id`+`article_count`，**无 corp_id**，公司由 product_kb_id 决定）→ 轮询 `creation/articles` 筛新文章 → 在飞书**顶层目录**下自动建「当天日期 M.D」子目录并粘贴 → 可选填表。每步回群一句。`粘贴到`/`填到` 可省略（只生文就写 `@机器人 生文 长岛民宿 长岛民宿推荐 10`）；**约定：只给最上层公司名目录链接，日期子目录由 bot 自动建**。
- **更新机器人（零手动）**：群里发 `@机器人 更新` → bot 自动 `git pull` 最新代码并以新进程替换自己（自重启用 `--wait-exit <旧pid>` 等旧实例退出再抢单实例锁，避免误判重复）。配合 `start_bot.bat` / 启动文件夹镜像 在启动前先 `git pull`，保证任何一次启动都跑最新版。
- 方向 A 的公司名后的 `(id：83966)` 等标注会被自动忽略（按名反查 + 探文章数选真实 corp）。
- **指令解析对口语较宽容**（不是死板模板）：①数量「条/篇/篇文章」都识别，中文数字（十/二十）也认，且**不写数量时默认 10 篇**（方向 A）；②公司名后的 `(id:83966)` 标注自动去除；③消息里贴的飞书链接自动抓取（位置不严格，按出现顺序取源/目标）；④解析不出会回群 `⚠️ 没看懂指令`+ 格式示例，**不会静默失败**。硬要求只有：群里必须 **@机器人**。**方向 A 触发有两种**：(a) 含「粘贴」关键字 + 飞书目录链接（任意公司）；(b) **client_map 里已配的公司名 + 篇数**（如 `永安期货 10篇`，无需给链接，自动建当日日期子目录并填表，v2.7.0+）。方向 B 要「粘贴」、方向 F 要「填」+「链接/表」。
- 数量写法兼容「最新10条 / 最新10篇 / 最新10篇文章」（`parse_command` 用 `最新\s*(\d+)` 提取，不再漏掉带「篇」字的说法）。
- ⚠️ **目标链接必须是文件夹/docx 文档**，不能是电子表格(sheet)/多维表(bitable)。bot 在写之前会先查节点 `obj_type`，若是 `sheet/bitable/mindnote` 会直接回群提示「不能贴文章页，请给文件夹链接」，避免像早期那样误把文章贴到表格节点下导致整批失败。
- 同一时刻只处理一条任务（其余排队提示「处理中」）；方向 A/B/F 共用 `_paste_lock`。**进程级单实例保护**：`feishu_bot.py` 启动时扫描系统进程，若已存在另一个 `feishu_bot.py` 主实例（排除自身及自身 fork/spawn 的 worker，并排除调试/验证辅助脚本 `_verify_bot`/`_debug_bot`）则拒绝重复启动——**v2.6.5 起由「原子锁文件 `.bot.lock`」改为「进程扫描」**，根治 lark ws 库 fork/spawn worker 模型下锁文件被误删/误判导致保护失效的问题；避免多实例抢同一个 Chrome 调试端口 9222 互相打架（早期「每次只粘几篇 + 后续报错」的元凶之一）。
- **自愈重连**：`main()` 的 `cli.start()` 包在 `while True` 重连循环里——一旦网络抖动或服务端踢线导致 `cli.start()` 返回或抛异常，会打日志并 `sleep(5)` 后自动重连，进程**永不因瞬时断线而退出**（常驻/开机自启场景必需；否则断网一次 bot 就掉线需手动拉起）。

### 实现要点
- `feishu_bot.py`：`lark.ws.Client` 长连接收消息 → `parse_command` 解析 → 后台线程调 `feishu_collect()`+`run_pipeline()` → `send_text()` 回发。
- 复用 `prepare_multi.feishu_collect`（读+转 HTML）与 `auto_paste.run_pipeline`（登录+写+校验），零重复代码。
- 事件回调须在 3 秒内返回 → 重活在后台线程跑，先 ACK 再回报。

---

## 📝 用户提示词模板

### 方向 A（CMS→飞书Wiki）：

**已配 client_map 的客户（推荐，最简）**——只需说公司名 + 篇数，机器人自动匹配目录、建当日日期子目录、填表：
```
@机器人 永安期货 10篇
@机器人 中泰期货 20篇
```
> 加新客户：告诉我「公司名 → 目录链接 + 表格链接」，我补进 `client_map.json` 即可。

**未配 client_map 的临时任务**——给完整链接：
```
用户每次任务只需提供：
1. 客户名（公司全名，同名多公司会按文章数自动选）
2. 目标 Wiki 目录 URL
3. （可选）表格 URL + 填表列
```
示例：
> 把永安期货最新 20 篇文章粘贴到 https://xxx.feishu.cn/wiki/ABCD 的 7.10 目录下，
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

已知词包 ID：
| 公司 | corp_id | 词包名 | keyword_package_id | 状态 |
|------|---------|--------|-------------------|------|
| 龙马潭新时代口腔诊所 | **1155** | 泸州口腔门诊医院 | 1331 | ✅ 已实测验证(2026-07) |
| 成都川蜀血管病医院有限公司 | **3041** | 成都静脉曲张医院 | 5538 | ✅ 已实测验证(2026-07) |
| 云南约牛软件技术有限公司 | **2732** | 天龙博弈 | 4111 | ✅ 已实测验证(2026-07，30篇落库) |
| 云南约牛 | 2732 | 约牛软件 | 4103 | ⚠️ 旧编号，写前务必 verify-corp |
| 上海利多星 | 3726 | 智能股票软件推荐 | 6407 | ⚠️ 旧编号，写前务必 verify-corp |
| 永安期货股份有限公司 | **3258** | 新手期货公司推荐 | 7502 | ✅ 已实测验证(2026-07) |
| 贵阳脉通血管医院有限公司 | **3049** | 贵阳静脉曲张医院推荐 | 5539 | ✅ 已实测验证(2026-07) |
| 中泰期货股份有限公司 | **543** | （方向 A：CMS→飞书，按文章数自动选） | — | ✅ 实测 1728 篇，方向 A 批量写入飞书(2026-07) |

### 飞书节点速查（方向 A / 填表用）
| 用途 | node_token | 类型 | 说明 |
|------|-----------|------|------|
| 云南约牛 文章目录 | **D5nLwcbp9itV2ykAhM9cIx9Xnvd**（标题「7.20」） | docx（可贴文章页） | 方向 A 把文章页贴这里 |
| 云南约牛 审核表 | **PI5owcGzii2mnSkFvjSc01H2nvd**（「约牛内容审核表」） | **sheet（表格）** | ⚠️ 只能填链接，**不能贴文章页**；父节点 QucCwtrytiWzxvkroPEcc35FnVh |
| 永安期货 文章目录 | **GKETw73PPi9qvUkgI8BcEVBOnih** | docx | 方向 A 文章页 |
| 永安期货 审核表 | **YQAywVgkUiXIhNkdJvTcVw0xnIc**（「永安期货内容审核表」） | **sheet** | 链接填 B 列（实测 B108:B137） |
| 中泰期货 文章目录 | **Zi5CwEPMKivP2UkpWFucjDrDnDd** | docx | 方向 A 文章页 |

> ⚠️ **corp_id 是最大坑**：用户口头给的"公司编号"几乎都不是真实 `corp_id`
> （实测用户给 84347，真实是 1155；用户给中泰期货"83966"，真实是 543）。真实 corp_id 必须用下面「步骤 B7」的
> `--verify-corp` / `--auto-corp` 反查，**不要相信任何手写数字**。
> 更省事：直接用 `cms_discover_ids.py --company '公司全名' --pkg '词包名'`
> 通过 API 自动查出 corp_id/pkg_id（见「可复用脚本」）。
>
> 🔴 **同名多公司（双胞胎坑）**：实测「中泰期货股份有限公司」在 CMS 里竟有 **7 个**
> 同名 corp（id 2414 / 2260 / 1613 / 1612 / 1611 / 1599 / 543），其中 6 个是空的，
> **只有 543 有 1728 篇**。方向 A 的 `find_corp_by_name()` 按公司名匹配到所有候选后，
> 逐个探文章数、自动选「有文章且最多」的那个，彻底规避选错空壳公司。

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

### 步骤 B7：⚠️ 实战踩坑与一键脚本（2026-07 实测）

本次真实跑通一套流程后，把最致命的坑和可复用脚本固化如下。**这些坑原 SOP 没写，第一次必踩。**

#### 🔴 坑 1：corp_id 传错 → 接口成功但界面永远看不到

- `POST /yunying/v1/articles` 只要字段格式对就返回 `code=0`，按 id 也能 `GET` 到；
- 但界面「词包文章」列表（`GET /yunying/v1/creation/articles`）**按当前登录公司 `corp_id` 过滤**；
- 一旦 `corp_id` 错了，文章挂在错误公司名下，界面永远筛不出来 —— 看起来像"没写进去"。
- **正确 corp_id 不能用用户手写数字**，必须实测反查（见步骤 B6 脚本的 `--verify-corp`）。

#### 🔴 坑 2：Chrome 远程调试端口必须用「非默认配置」

- 直接 `chrome.exe --remote-debugging-port=9222`（默认 User Data）会启动失败；
- 必须复制一份配置到临时目录，用 `--user-data-dir=临时目录` 启动（登录态会带过去）；
- 已封装成 `launch_chrome_debug.py`，一行搞定（见下）。

#### ✅ 推荐的一键流程（用本 skill 自带脚本）

```bash
# 0) 准备带调试端口的 Chrome（复制 profile + 启动，原 Chrome 不动）
python launch_chrome_debug.py
#    → 启动后保持窗口打开，确认 huiyouhua 已登录

# 1) 飞书读取 + 转 HTML（skill 原生函数）
python run_direction_b.py --node OfcbwcdeyicTKlkgS6fczBYhn4e \
    --pkg-id 1331 --dry-convert          # 仅转换，产出 converted.json 预览

# 2) 写 CMS 前，先自动核对 corp_id（读当前公司一篇样本反查，不写任何东西）
python direction_b_cms_write.py --articles converted.json --pkg-id 1331 --verify-corp

# 3) 写 1 篇探路，确认它出现在词包列表
python direction_b_cms_write.py --articles converted.json \
    --corp-id 1155 --pkg-id 1331 --preflight

# 4) 正式铺开，并逐篇校验落库
python direction_b_cms_write.py --articles converted.json \
    --corp-id 1155 --pkg-id 1331
#    → 结果存 cms_write_results.json；异常篇会自动标记 mismatch

# （可选）删掉之前写错的文章
python direction_b_cms_write.py --delete 235619,235620
```

> 嫌分步麻烦可用 `run_direction_b.py`：读+转+写+校验一条龙（`--node <飞书node> --company <公司> --pkg <词包>`），
> corp_id 自动从列表反查。**但首次务必先 `--preflight` 探路**，确认界面能看到再铺开。

#### 已知正确 corp_id（实测）

- **龙马潭新时代口腔诊所 = 1155**（泸州口腔门诊医院词包 1331 已实测 20 篇成功落库）
- **成都川蜀血管病医院有限公司 = 3041**（成都静脉曲张医院词包 5538 已实测 20 篇成功落库）
- **永安期货股份有限公司 = 3258**（新手期货公司推荐词包 7502 已实测 19 篇成功落库）
- **贵阳脉通血管医院有限公司 = 3049**（贵阳静脉曲张医院推荐词包 5539 已实测 59 篇成功落库，含 6/29·6/30·7.1·7/2 四子目录）
- **云南约牛软件技术有限公司 = 2732**（天龙博弈词包 4111 已实测 30 篇成功落库）

---

## 🔜 方向 A：CMS → 飞书 Wiki（自动批量，已实战验证）

### 概述

将某公司在 huiyouhua CMS 里「最新 N 篇」文章，批量创建到飞书知识库指定目录下。
**核心流程**：
1. 浏览器自动化登录 huiyouhua（复用 `auto_paste` 的 `ensure_chrome`/`check_logged_in`/`do_login`）
2. 按公司名定位真实 corp_id（`find_corp_by_name`：同名多公司时自动选有文章且最多的）
3. `changecorp` 切到该公司 → 拉 `GET /yunying/v1/creation/articles?page=1&page_size=N`
4. 按 `created_at` 降序取最新 N 篇（该接口**已含完整 content HTML**，无需再逐篇 GET）
5. `convert_html_to_blocks` 转飞书块 → `create_wiki_node_and_write` 逐篇写入目标目录

### 关键要点 / 实战教训

| 要点 | 详情 |
|------|------|
| **`creation/articles` 已含完整 `content`** | 返回每篇的 `content` 即完整 HTML，直接转块即可，不必再 `GET /articles/{id}` |
| **排序字段 `created_at`** | ISO8601（如 `2026-07-17T16:58:26.677+08:00`），降序取最新；解析失败退回字符串排序 |
| **同名多公司陷阱** | 中泰期货在 CMS 有 7 个同名 corp，只有 543 有文章。`find_corp_by_name` 自动探文章数选最多的 |
| **用户给的"编号"不是 corp_id** | 中泰期货用户给 83966，真实 543；永远按名反查 + 探文章数 |
| **一次拉取足够** | `page_size` 取 `max(limit, 50)` 一次性拉回再内存排序，避免翻页 |

### 步骤 A1：定位公司（同名多公司自动选）

```python
from direction_a_cms_to_feishu import find_corp_by_name
# 在已登录且连上 Chrome 的 page 上下文里：
corp_id, corp_name = find_corp_by_name(page, "中泰期货股份有限公司")
# → 打印 7 个候选的文章数，自动选 543（1728 篇）
```

### 步骤 A2：拉最新 N 篇 + 写入飞书

```python
from direction_a_cms_to_feishu import run_pipeline_a
res = run_pipeline_a(
    company="中泰期货股份有限公司",
    node_token="Zi5CwEPMKivP2UkpWFucjDrDnDd",  # 目标飞书 Wiki 目录
    limit=10,                                  # 最新 10 篇
    log_fn=print,
)
# → res = {"total":10,"ok":10,"bad":0,"urls":[...],"corp_id":543,"space_id":...}
```

### CLI 一行跑通

```bash
python direction_a_cms_to_feishu.py \
    --company "中泰期货股份有限公司" \
    --node Zi5CwEPMKivP2UkpWFucjDrDnDd \
    --limit 10
# 加 --dry-run 仅预览转换（不写飞书）；加 --space-id 指定知识库空间
```

### 已知正确 corp_id（方向 A 实测）

- **中泰期货股份有限公司 = 543**（1728 篇，方向 A 批量写入飞书 2026-07 实测 10/10 成功）

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
| v2.7.5 | 2026-07-22 | 🐞 **修 run_client.py 路径飞书凭证未注入**：`run_pipeline_a` 内部漏调 `load_env()`（仅 CLI `main()` 调了），经 `run_client.py` 端到端跑方向 A 时 `KeyError: FEISHU_APP_ID`；已在 `run_pipeline_a` 开头补 `load_env()`。🆕 新增客户「西安东大肛肠」到 client_map.json（dir=GIdfwVWk / sheet=GBGpw6dQ），实战 30/30 写入飞书子目录 `7.22` 并回填审核表 |
| v2.7.4 | 2026-07-21 | 🐞 **修方向 B 登录静默失败**：`auto_paste.do_login()` 自动点击「GEO账号登录」选项卡，把表单切到 GEO 模式导致运营账号凭据静默失败；移除该点击，默认运营账号模式登录成功。🆕 新增 `run_direction_b.py` 通用驱动，实战把约牛飞书目录 29 篇粘贴回「约牛软件」词包（corp_id=2732, pkg_id=4103，29/29） |
| v2.7.3 | 2026-07-21 | 🧹 **整理脚本**：删除 4 个一次性 `run_*.py`，统一用 `run_client.py`（方向A）/ `run_direction_b.py`（方向B）通用驱动；同步 SKILL.md |
| v2.7.2 | 2026-07-21 | 🐞 **修 process_a 自动填表 `run_fill` 未定义（NameError）**：补 import，client_map 简写流程的「自动填表」恢复可用；新增 `run_client.py` 通用方向 A 驱动（基于 client_map，替代分散的 run_*.py 一次性脚本） |
| v2.7.1 | 2026-07-21 | 🐞 **修简写格式被预过滤误拒**：`中泰期货 20篇` 这类「无动作关键字」指令因预过滤要求含「粘贴/填/生文」被回「没识别动作」；改为 client_map 命中公司名即放行方向 A |
| v2.7.0 | 2026-07-21 | 🚀 **client_map 简写流程（日常 90% 场景）**：新增 `client_map.json`，用户只需 `@机器人 <公司名> <篇数>`（如 `永安期货 10篇`）即自动匹配顶层目录 + 建当日日期 M.D 子目录 + 粘贴 + 回填审核表，无需每次给链接；中文数字、公司名清洗一并支持。配套 `run_client.py` 可在对话/本地直接驱动 |
| v2.6.13 | 2026-07-21 | 🔧 **修方向 A 解析**：① 数量支持中文数字（十/二十），`永安期货的最新十篇` 正确拆为 company=永安期货、limit=10（旧正则只认阿拉伯数字把整串当公司名 → 查不到 corp）；② 支持指令里「自建/新建/下面新建 X 目录」自动建子目录并贴入 |
| v2.6.12 | 2026-07-21 | 🔧 **start_bot 改可见窗口 python.exe**：绕开 venv 的 `pythonw` 在用户机 `0xc0000142` 闪退（连报错都看不到）；启动前先清僵尸进程，避免被单实例保护误拒 |
| v2.6.11 | 2026-07-21 | 🐞 **修 start_bot.bat 闪退**：改用基础 Python 的 `pythonw.exe`（非 venv）+ 给基础 Python 装齐依赖（lark_oapi/requests/psutil/playwright）；新增 `start_bot_debug.bat` 诊断版；启动文件夹镜像同步 |
| v2.6.10 | 2026-07-21 | 🔧 **自启脚本用绝对路径 git**，根治开机 PATH 缺失导致自更新静默失败（机器人重启后起不来的根因之一） |
| v2.5.0 | 2026-07-17 | 🆕 新增 `feishu_bot.py`：飞书群聊机器人（方向 B 自动执行），基于飞书事件「长连接模式」(WebSocket) 本机常驻，@指令即粘贴；复用 `feishu_collect()`+`run_pipeline()`，进度/结果回发群；`prepare_multi.feishu_collect` 与 `auto_paste.run_pipeline` 重构为可导入函数；新增 `start_bot.bat` 启动器；`.env` 增加 `HYH_USER`/`HYH_PWD`（huiyouhua 自动登录） |
| v2.6.0 | 2026-07-17 | 🆕 **方向 A 全自动跑通（CMS→飞书）**：新增 `direction_a_cms_to_feishu.py`（`find_corp_by_name` 同名多公司自动选有文章的 + `run_pipeline_a` 拉最新 N 篇写入飞书）；固化中泰期货=543（7 个同名 corp 中唯一有 1728 篇）；`feishu_bot.py` 升级为**双向**，新增方向 A 指令解析（`把 <公司> 粘贴到 <飞书目录> 最新N条`）；实战写入中泰期货最新 10 篇到飞书目录 Zi5CwEPMKivP2UkpWFucjDrDnDd（10/10 成功） |
| v2.6.1 | 2026-07-20 | 🆕 **链接回填飞书电子表格**：新增 `fill_sheet.py`（走 `sheets/v2`：metainfo 取 sheetId → 读列找末行 → PUT /values 追加链接到指定列），实战把永安期货最新 30 篇飞书链接追加到「【GEO服务版-金融】永安期货内容审核表 / 7月内容」B108:B137（30/30）；明确 **电子表格(sheet) ≠ 多维表格(bitable)** 两套 API，应用需 `sheets:spreadsheet` 权限+分享表格给机器人 |
| v2.6.2 | 2026-07-20 | 🐛 **修 bot 方向 A 两个根因**：① `parse_command` 数量正则只认「最新N条」，漏掉「最新N篇/最新N篇文章」→ 退回默认 10（这就是「每次只粘 10 条」的真因）；现已用 `最新\s*(\d+)` 提取，且 company 自动去尾部「的最新N篇…」；② **进程级单实例锁** `.bot.lock`（含 pid，存活则拒绝重复启动），杜绝多实例各持一把锁抢 Chrome 9222 打架；③ `process_a` 写前校验目标节点 `obj_type`，若是 sheet/bitable/mindnote 直接回群提示「不能贴文章页，请给文件夹链接」（避免误贴表格节点整批失败）。新增 `paste_yn_missing.py`（按标题去重补贴缺失篇章，实战把云南约牛最新 30 篇补满到 D5nLwcbp9itV2ykAhM9cIx9Xnvd，30/30）；新增「飞书节点速查」表 |
| v2.6.3 | 2026-07-20 | 🆕 **机器人新增方向 F（目录文章链接→填表）**：`@机器人 把 <源目录链接> 的文章链接填到 <表格链接> 第二列 [工作表 X]` 即可把源目录全部 docx 子页链接追加写进目标电子表格指定列（默认 B，从已填下一行起）。`fill_sheet.py` 抽出可复用 `run_fill()`；新增 `collect_d5nl_links.py` 收集目录文章链接。实战：云南约牛 D5nL 目录 30 篇链接填进「约牛内容审核表 / 6月内容」B362:B391（30/30，与已有 361 行 0 重复）。同时修 `process_a` 中 `prepare_multi.get_token()` 的模块名引用 bug（改为直接 `get_token()`） |
| v2.6.4 | 2026-07-20 | 🔧 **启动与单实例加固**：①新增 `start_bot.bat`（双击即用 `pythonw` **无黑窗口常驻**，日志写 `bot_console.log`，彻底脱离终端/本会话）；② `feishu_bot.py` 加 `_setup_logging()` 把 stdout/stderr 重定向到 `bot_console.log`（append），`pythonw` 无窗口运行时也不丢日志；③单实例锁由普通 `open("w")` 改为 `os.open(O_CREAT|O_EXCL)` **原子创建**，杜绝竞态窗口下双实例同时通过（之前因 WorkBuddy 后台任务自动重试 + 旧锁非原子，反复出现"幽灵双实例"误判，现根治）；④文档补齐启动方式、`(id)` 标注自动去除、数量"条/篇/篇文章"兼容、默认 10 篇、链接自动抓取、解析失败回群 HELP 等容错说明 |
| v2.6.5 | 2026-07-20 | 🔧 **单实例保护与常驻健壮性**：①单实例保护由「原子锁文件 `.bot.lock`」改为「**进程扫描**」`_acquire_bot_lock()`（扫描系统进程里其它 `feishu_bot.py` 主实例，排除自身/自身 worker/调试脚本），根治 lark ws 库 fork/spawn worker 模型下锁文件被 worker/atexit 误删或父进程误判导致保护失效（v2.6.4 的锁方案在该模型下仍会双开或漏保护）；②`main()` 的 `cli.start()` 包进 `while True` **自愈重连循环**，网络抖动/服务端踢线后自动重连，进程不再因瞬时断线退出；③`start_bot.bat` 改为「双 cmd detached」写法（`start "" cmd.exe /c "bat detached"` → `pythonw`），确保 `pythonw` 不被外层命令的进程树回收；④新增 Windows「启动」文件夹镜像副本 `FeishuWikiPasteBot.bat`，实现**开机自启、彻底脱离 WorkBuddy**。已推送 `ba33f2d` |
| v2.6.6 | 2026-07-20 | 🆕 **方向 G｜智能生文全自动**：`@机器人 生文 <公司> <词包> <数量> 粘贴到<飞书目录> 填到<表格>` → 机器人自己点后台「智能生文」→选词包→填数量→等生成完→粘贴飞书（方向A）→填表（方向F），每步回群一句。新增 `shengwen_pipeline.py`（5步流水线，提交时自动拦截 fetch 记录真实生文接口到 `shengwen_api_capture.json`）；`parse_command` 加 `parse_g`（优先于 A/B/F，避免误判），`do_message` 加方向G分发与更新指令分发，`process_g`/`process_update` 实现；`start_bot.bat` 与启动文件夹镜像均在启动前 `git pull` 保最新；新增 `@机器人 更新` 指令（git pull + `--wait-exit` 自重启用新进程，免手动）。配套 `run_shengwen.bat` 双击交互式启动器 |
| v2.6.7 | 2026-07-20 | 🔧 **方向 G 改为直接 API 生文（根治公司作用域事故）**：`shengwen_pipeline.py` 由「模拟点击 UI」重构为**直接调 huiyouhua 后端接口**——`corp/active`+`auth/changecorp` 按名定位 corp（同名多 corp 逐个探测，中泰期货 7 个同名 corp 仅 543 有内容自然命中）→ `knowledge-base` 取 `product_kb_id` → `keyword/package` 取 `keyword_package_id`（词包名称字段是 `core_keyword`，非 `name`）→ `POST /creation/tasks`(`product_kb_id`+`keyword_package_id`+`article_count`，**无 corp_id**，公司由 product_kb_id 决定）→ 轮询 `creation/articles` 筛新文章 → 飞书顶层目录自动建「当天日期 M.D」子目录并 `batch_paste` 粘贴 → 可选 `run_fill` 填表。已验证：中泰期货=2198/1773/543、长岛民宿=4187/5024/2692 解析正确；`connect_cdp` 加 asyncio 重试防护（同进程连续两条 G 指令不崩）；`feishu_bot.py` 的 `process_g` 回发逻辑同步更新 |
| v2.6.8 | 2026-07-20 | 🐞 **根治「僵尸进程占坑死锁」+ 静默不回**：根因是 `@机器人 更新` 自更新时旧进程退出、新进程启动却发现「还有另一个 feishu_bot 实例」（开机自启那份也被计入）→ 新进程被单实例保护拒掉，只剩一个连不上飞书也不响应消息的僵尸进程占坑，后续所有启动全被拒。修复：① 新增**心跳自愈**——活实例每轮重连 + 收到消息时写 `.bot.heartbeat`，新实例发现挡路实例时若其心跳超 90s（卡死/连不上）或心跳不一致，则 `kill` 后接管，不再死锁；② `process_update` 拉起新实例改传 `--replace`，自更新必定强制替换；③ `do_message` 对「@ 但不含动作关键字」的消息从静默跳过改为回一句「我在，但没识别到动作」，避免被误认为死机。`.gitignore` 加 `.bot.heartbeat` |
| v2.6.9 | 2026-07-21 | 🔧 **方向 A 粘贴改为对话内全自动 + 修子目录建块 bug**：① 实测沙箱可**自起调试 Chrome + 自动登录 huiyouhua**（`HYH_PWD` 在 `.env`，`ensure_chrome()` 用临时 profile 拉起并登录），故方向 A（CMS 文章→飞书）可在对话里直接跑完，**不必用户双击**；填表只用飞书 token，更不依赖 Chrome。② 修 `ensure_date_subdir` 建日期子目录的块格式 bug：`block_type:2` 必须用 `"text"` 键（含 `text_element_style`），误用 `"paragraph"` 会被飞书拒 `1770001 invalid param`（永安期货这次复现失败，中泰期货那次侥幸成功）。③ `fill_sheet.run_fill` 新增 `date_label`：第二列填链接、第一列整块合并写日期（如「7月21日」），已用于云南约牛/永安期货审核表。④ 新增 `run_yongan_paste.py`/`run_yunan.py` 一键驱动模板（方向A 粘贴最新 N 篇 + 建日期子目录 + 填表）。实测：永安期货 corp=3258，最新 20 篇粘贴进 `KXcXwjr...` 下 `7.21` 子目录，填 `YQAywVgk...`（B138:B157 链接 / A138:A157 合并「7月21日」）全 20/20 成功 |
| v2.4.2 | 2026-07-17 | 🆕 `auto_paste.py` 参数化（去掉永安期货硬编码，改 `--company/--pkg/--in` 参数，支持任意公司/词包）；🆕 新增 `prepare_multi.py`：父目录取「指定日期子目录」文章并合并去重（自动归一化分隔符、逐层收集 doc）；固化 贵阳脉通血管医院有限公司=3049 / 贵阳静脉曲张医院推荐=5539（59 篇实测落库，含 6/29·6/30·7.1·7/2 四子目录）；`.gitignore` 增加 `*_articles.json`/`*_cms_write_results.json` 通用忽略 |
| v2.4.0 | 2026-07-15 | 🆕 新增 `cms_discover_ids.py`：通过 API 自动反查公司 corp_id + 词包 pkg_id（corp/active 列公司 + auth/changecorp 切换 + keyword/package 取词包），彻底免盲猜；实测成都川蜀血管病医院有限公司=3041 / 成都静脉曲张医院=5538 |
| v2.3.0 | 2026-07-15 | 🆕 方向 B 实战踩坑固化：新增 `launch_chrome_debug.py`/`direction_b_cms_write.py`/`direction_b_run.py`；补充 corp_id 陷阱(corp_id 错→界面看不见)、Chrome 调试端口必须非默认配置两大坑；新增 `--verify-corp`/`--auto-corp`/`--preflight` 防错机制；实测 corp_id 龙马潭新时代口腔诊所=1155 |
| v2.2.0 | 2026-07-14 | 🆕 方向 B：Wiki→CMS 完整 SOP（飞书块→HTML 转换、CMS 创建/删除 API）；新增 5 条错误速查（标题丢失、重复、更新 API 404、批量超时）；新增 paste_utils 反向函数 |
| v2.1.0 | 2026-07-10 | 🆕 预览模式 (--dry-run)、失败重试 + 断点续传 (--state + --retry-failed) |
| v2.0.0 | 2026-07-10 | 核心修复：HTMLParser 替代正则；CMS 获取步骤；列表/粗体规范化 |
| v1.0.0 | 2026-07-09 | 初始版本：CMS→Wiki SOP + 块格式速查 |