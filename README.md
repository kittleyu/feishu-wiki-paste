# 飞书 Wiki 批量粘贴 (feishu-wiki-paste)

> OpenClaw Skill — CMS → 飞书知识库 Wiki 批量粘贴 + 链接填表一条龙

## 功能

1. 从 CMS 获取文章（浏览器自动化 / API）
2. 批量创建到飞书知识库 Wiki 指定目录
3. HTML → 飞书文档块自动转换（标题、粗体、列表）
4. 文章链接批量填入飞书多维表格

## 快速开始

### 1. 环境变量

```bash
export FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 2. 命令行使用

```bash
python3 paste_utils.py articles.json PARENT_NODE_TOKEN SPREADSHEET_TOKEN SHEET_ID START_ROW
```

### 3. 作为库使用

```python
from paste_utils import convert_html_to_blocks, batch_paste

# 转换 HTML → 飞书块
blocks = convert_html_to_blocks(html_content)

# 批量粘贴
urls = batch_paste(articles, token, space_id, parent_node,
                   spreadsheet_token, sheet_id, start_row)
```

## 依赖

```bash
pip install requests
```

## 配套 Skill

完整 SOP 见 [SKILL.md](./SKILL.md)

## License

MIT
