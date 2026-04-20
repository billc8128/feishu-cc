# 飞书云文档集成设计

> 给 agent 加四个飞书文档工具,让它能在用户的个人飞书云空间里读/写/搜文档。Agent 自主判断该写文档还是该聊天回复。

- 日期:2026-04-20
- 作者:Claude + @superlion8
- 状态:已设计,待实现

---

## 1. 目标与非目标

### 目标

- Agent 能在用户**个人飞书云空间**的"AI 助手"文件夹下创建、追加、读取、搜索云文档。
- Agent 自己判断:内容足够长或结构化(≥300 字 / 含多级标题、表格、代码块)时写成文档,简单问答仍走聊天。用户可用自然语言覆盖("直接告诉我就行")。
- 支持用户把任意飞书文档链接丢给机器人,agent 能读取内容(含 wiki 文档)。
- 写文档时支持 markdown 中的图片(agent 在 sandbox 内生成 PNG,自动上传并插入文档)。
- 单用户体验:一次 OAuth 授权,30 天内无感。

### 非目标

- 不支持企业知识库落点(只落个人空间)。
- 不读取文档内图片(image block → alt/链接占位,不调用视觉模型)。
- 不支持飞书表格(sheets)、多维表格(bitable)、脑图——只做云文档(docx)。
- 不支持 markdown 里的公式、脚注、HTML 标签、嵌套超过 3 层的列表(降级成纯文本)。
- 不支持企业管理员代授权——授权由用户本人完成。

---

## 2. 架构总览

### 新增文件

```
feishu/
  oauth.py         OAuth 授权流(构造 URL、处理回调、token 刷新)
  docs_client.py   飞书云文档 API 客户端 + markdown↔blocks 转换
agent/
  tools_docs.py    暴露给 Claude Agent SDK 的 4 个工具
```

### 改动文件

- `app.py`:新增 2 个路由 `/feishu/oauth/start` 和 `/feishu/oauth/callback`。
- `agent/runner.py`:把 tools_docs 注册进 MCP server 列表;system prompt 追加文档工具策略段;动态注入授权状态行。
- `feishu/events.py`:拦截 `/auth-docs` 命令(不走 agent,直接走 oauth 模块)。

### 新增数据表

```sql
CREATE TABLE feishu_oauth_tokens (
  open_id            TEXT PRIMARY KEY,
  access_token       TEXT NOT NULL,
  refresh_token      TEXT NOT NULL,
  access_expires_at  INTEGER NOT NULL,   -- unix seconds
  refresh_expires_at INTEGER NOT NULL,
  docs_folder_token  TEXT,               -- 用户"AI 助手"文件夹的飞书 token(懒加载后缓存)
  docs_folder_name   TEXT DEFAULT 'AI 助手',
  updated_at         INTEGER NOT NULL
);

CREATE TABLE feishu_oauth_states (
  state      TEXT PRIMARY KEY,
  open_id    TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX idx_oauth_states_expires ON feishu_oauth_states(expires_at);
```

Token 明文存储。理由:数据库文件位于 Railway Volume,不对外暴露;加密需要引入独立密钥管理,与项目其他敏感数据(app_secret 在环境变量明文)的安全级别不一致,是过度设计。如果后续要加密,应当单独立项统一处理。

### 数据流:"帮我写周报"

```
用户飞书消息 "帮我写周报"
    ↓
feishu/events.py 解析为 IM 消息 → 走 agent runner
    ↓
runner 读 feishu_oauth_tokens → 授权状态为"已授权"
    ↓ 注入 prompt:"[已授权]"
Claude(Kimi)判断:内容会超 300 字、有结构 → 调 feishu_doc_create
    ↓
tools_docs.py:通过 token_provider 拿 valid access_token
    ↓
docs_client.py:
  1. ensure_ai_folder() → 返回 folder_token(命中缓存)
  2. POST /documents(title, folder_token)→ doc_id
  3. markdown_to_blocks(content)→ blocks[]
  4. 图片节点:upload_image(path)→ image_token;image block 先占位再填充
  5. POST /documents/{id}/blocks/.../children → 批量插入
    ↓
返回 doc_url 给 agent
    ↓
Claude 生成回复:"写好了:<url>,主要讲了...(一句话)"
    ↓
机器人发回飞书
```

---

## 3. OAuth 授权流

### 用户体验

1. 用户在飞书私聊机器人发 `/auth-docs`。
2. `feishu/events.py` 拦截,调 `oauth.build_authorize_url(open_id)`,返回一条消息:

   > 🔐 授权访问你的飞书文档
   > [点击授权](https://accounts.feishu.cn/open-apis/authen/v1/authorize?...)
   > 授权有效期 30 天。

3. 用户点链接 → 飞书授权页 → 同意。
4. 飞书重定向到 `https://feishu-cc-production.up.railway.app/feishu/oauth/callback?code=...&state=...`。
5. 后端校验 state → 用 code 换 token → 存表 → 返回一段纯文字页面("授权成功,回到飞书继续聊天就行")。
6. 后端额外通过 `feishu_client.send_text(open_id, "✅ 授权成功")` 在飞书里推一条确认。

### 关键设计

**State 防 CSRF**

- 生成:`state = secrets.token_urlsafe(32)`。
- 存:`feishu_oauth_states(state, open_id, expires_at=now+600)`。
- 回调时:查 state → 得 open_id;查不到或过期就返回 400。
- 用后立即删除(一次性)。

**Scope**(授权 URL 里放的)

```
docx:document drive:drive drive:file wiki:wiki contact:user.base:readonly offline_access
```

其中 `offline_access` 让飞书下发 `refresh_token`——飞书开放平台"权限管理"页面没有这一项,必须靠 scope 传入(OAuth 2.0 标准约定)。

**Token 生命周期**

- access_token:2 小时。
- refresh_token:30 天,每次刷新后自动续期。
- 访问策略:`get_valid_token(open_id)`——剩余 <5 分钟就提前刷新;refresh 过期抛 `NotAuthorized`。
- 过期前 3 天的预警:runner 构造 prompt 时检查 refresh_expires_at,若 <3 天,prompt 标为"[即将过期]",指示 agent 在当轮回复末尾附一句温和提醒。

### 回调接口契约

```
GET /feishu/oauth/callback?code=<code>&state=<state>

成功:
  200 OK, Content-Type: text/plain; charset=utf-8
  Body: "授权成功,回到飞书继续聊天就行"

state 无效/过期:
  400 Bad Request
  Body: "授权链接已失效,请回飞书重新发送 /auth-docs"

code 换 token 失败:
  502 Bad Gateway
  Body: "授权遇到问题,请重试或联系管理员"
```

### 失败路径

| 情况 | 行为 |
|---|---|
| 用户从未授权,agent 尝试调文档工具 | 工具抛 `NotAuthorized` → prompt 已指示 agent 回复"需要先 /auth-docs" |
| access_token 过期,refresh 还活着 | 透明刷新,agent 无感 |
| refresh_token 也过期(30 天没用) | 同上第一种,提示重新授权 |
| 授权页关闭,从未回调 | state 10 分钟后过期,DB 定期清理 |
| 飞书 API 5xx | 指数退避重试(0.5s/1s/2s),仍失败抛给 agent |

### 前置条件(用户操作)

1. ✅ 在飞书开放平台"权限管理"勾选:`docx:document`、`drive:drive`、`drive:file`、`wiki:wiki`、`contact:user.base:readonly`,并发布版本。(已完成)
2. ⏳ 在飞书开放平台"应用功能 → 网页 → 重定向 URL"白名单加一条:
   `https://feishu-cc-production.up.railway.app/feishu/oauth/callback`

---

## 4. 飞书云文档客户端(`feishu/docs_client.py`)

### 要调用的飞书 API

| 业务动作 | 飞书 API | 方法 |
|---|---|---|
| 创建空文档 | `/open-apis/docx/v1/documents` | POST |
| 追加 blocks | `/open-apis/docx/v1/documents/{id}/blocks/{block_id}/children` | POST |
| 读 blocks | `/open-apis/docx/v1/documents/{id}/blocks` | GET |
| 搜文档(文件夹内) | `/open-apis/drive/v1/files?folder_token=...` | GET |
| 创建文件夹 | `/open-apis/drive/v1/files/create_folder` | POST |
| 上传图片 | `/open-apis/drive/v1/medias/upload_all` | POST multipart |
| Wiki 节点 → docx token | `/open-apis/wiki/v2/spaces/get_node` | GET |

### 类接口

```python
class FeishuDocsClient:
    def __init__(self, token_provider: Callable[[], Awaitable[str]]):
        """token_provider: 每次调用现取一个 valid access_token,解耦 OAuth 逻辑"""

    async def create_doc(self, title: str, parent_folder_token: str) -> str:
        """返回 doc_id"""

    async def append_markdown(self, doc_id: str, markdown: str, sandbox_root: Path) -> None:
        """sandbox_root 用于图片路径校验"""

    async def read_doc(self, doc_id_or_url: str) -> str:
        """返回 markdown 形式的文档内容"""

    async def search_docs(self, query: str, folder_token: str) -> list[dict]:
        """返回 [{title, doc_id, url, updated_at}, ...]"""

    async def ensure_ai_folder(self, open_id: str) -> str:
        """懒加载:先查 feishu_oauth_tokens 缓存,没有就创建并写回"""

    async def upload_image(self, local_path: Path) -> str:
        """返回 image_token"""
```

**为什么用 token_provider 而不是直接传 token**:
- 客户端内部可能多次调用(比如创建+插入 blocks),每次都能拿到当前最新的有效 token,避免长调用链中途过期。
- 让 OAuth 刷新逻辑全部留在 oauth.py,客户端纯粹。
- 测试友好(mock token_provider)。

### Markdown → Blocks 转换器

**支持的 9 种语法**:

| Markdown | 飞书 block 类型(block_type) |
|---|---|
| `# / ## / ###` | heading1/2/3 (3/4/5) |
| 普通段落 | text (2) |
| `**粗**` `*斜*` `` `code` `` | text_element_style(bold / italic / inline_code) |
| `- item` / `1. item` | bullet (12) / ordered (13) |
| ` ``` ` 代码块 | code (14) |
| `![alt](path)` | image (27) |
| `| a | b |` 表格 | table (31) |
| `[text](url)` | text_element_style.link |
| `---` | divider (22) |

**不支持**(降级为纯文本段落,记 warning):
- 脚注、LaTeX 公式、原始 HTML、嵌套超过 3 层的列表。

**实现选择**:手写 ~200 行的小解析器,不引入第三方 markdown 库。
- 第三方库(如 `lark-docs-converter`)更新滞后、依赖重、跟不上飞书 API 改动。
- 支持范围只需 9 种语法,自己写可控,边界 bug 好修。

**图片处理(最复杂的一步)**:

1. 转换器遇到 `![alt](path)`:
2. `path` 解析为绝对路径(相对于 agent 当前 cwd)。
3. 校验:`is_inside(path, sandbox_root)`——不在 sandbox 内直接 raise。
4. `upload_image(path)` → 拿到 `file_token`。
5. 先 POST 一个空的 image block(拿 block_id)。
6. 再 PATCH 把 file_token 绑到 block 上(飞书 docx 要求的两步流程)。

封装在 `_emit_image_block(doc_id, local_path)` 里,对外只暴露 markdown 字符串。

### Blocks → Markdown(读文档时)

用于 `read_doc`。规则对称,图片节点按如下规则输出:

- 有 `image.alt_text` → `![alt](飞书链接)`
- 无 alt → `![图片](飞书链接)`

这样 agent 能看到图片的结构位置和语义标签(如果有),但不触发视觉模型。

### 链接解析(`parse_doc_url`)

支持的链接形态:

- `https://xxx.feishu.cn/docx/<token>` → 直接是 doc_id
- `https://xxx.feishu.cn/wiki/<token>` → 先调 wiki get_node 换成 obj_token(= doc_id)
- 纯 token 字符串(agent 传 doc_id 时)→ 直接用

### 错误处理

| HTTP | 行为 |
|---|---|
| 401 | 触发一次 refresh,重试 1 次;仍失败 raise NotAuthorized |
| 403 | raise PermissionDenied("可能需要重新 /auth-docs 授权") |
| 404 | raise DocNotFound(原 URL) |
| 429 | 指数退避重试 3 次(0.5s/1s/2s) |
| 5xx | 重试 2 次 |
| Markdown 解析失败 | 不崩,失败片段作为纯文本 block;日志记 warning |

---

## 5. Agent 工具定义(`agent/tools_docs.py`)

### 4 个工具

```python
@tool(
  name="feishu_doc_create",
  description="""Create a new Feishu doc under the user's "AI 助手" folder
and return its URL. Use this when the user asks you to write a report,
meeting notes, plan, long-form answer, or anything structured enough
(≥300 words OR contains sections/tables/code blocks) that reading it in
chat would be painful. Title should be concise (≤30 chars).""",
  input_schema={
    "title":    {"type": "string"},
    "markdown": {"type": "string",
                 "description": "Full doc body in markdown. Images use "
                                "![alt](path) with path inside your sandbox."}
  }
)

@tool(
  name="feishu_doc_append",
  description="""Append markdown content to an existing Feishu doc.
Use this when the user asks you to add/extend/update a doc you wrote
earlier in this conversation, or a doc they linked.""",
  input_schema={
    "doc_id_or_url": {"type": "string"},
    "markdown":      {"type": "string"}
  }
)

@tool(
  name="feishu_doc_read",
  description="""Read a Feishu doc and return its content as markdown.
Use when the user shares a Feishu doc link and asks you to read/summarize/
reference it, or when you need content from a doc you previously wrote.""",
  input_schema={
    "doc_id_or_url": {"type": "string"}
  }
)

@tool(
  name="feishu_doc_search",
  description="""Search docs in the user's "AI 助手" folder by title/content.
Use when the user references a past doc without giving a link
(e.g. "that Q1 report I asked you to write last week").""",
  input_schema={
    "query": {"type": "string"}
  }
)
```

### 构造方式(闭包绑定 open_id)

沿用 `tools_deliver.py` 的 `build_deliver_mcp(open_id)` 模式:每次会话构造时,open_id 和 token_provider 都绑进闭包,agent 本身无法看到或操纵这些参数。工具 input_schema 里**只有业务参数**。

---

## 6. System Prompt 改动

在 `agent/runner.py` 现有 system prompt 后追加:

```markdown
## 飞书文档工具使用策略

你有 4 个飞书文档工具:feishu_doc_{create,append,read,search}。

**何时写文档(用 create)**:
- 用户明确要"写周报/方案/纪要/报告/文档"
- 或你的回复会超过 300 字、包含多级标题/表格/长代码块
- 或结构化内容读者需要反复查阅、分享给他人

**何时不写文档,直接在聊天里回**:
- 用户的问题只要一两句话就能答
- 问答型(解释概念、debug、简短建议)
- 用户明确说"直接告诉我就行""别写文档""就在聊天里回"—— 严格遵守
- 闲聊、打招呼、澄清需求

**写完文档后的聊天回复风格**:
- 一句话摘要 + 飞书链接,例如:
  "写好了:<url>。主要讲了 3 点:A、B、C"
- 不要把文档内容再在聊天里复述一遍

**修改/追加**:
- 用户让你"改一下那个文档"时,优先 feishu_doc_append 或 read 后重写
- 用户丢文档链接让你读,用 feishu_doc_read
- 用户提及过去写过的文档但没给链接,用 feishu_doc_search

**授权失败**:
- 工具可能抛 NotAuthorized,意味着用户未授权或授权过期
- 此时不要反复重试,直接告诉用户"需要先发 /auth-docs 授权一下"
```

### 动态授权状态注入

runner 构造 prompt 时读 `feishu_oauth_tokens`,在 prompt 末尾插一行:

- `[已授权]` → 正常
- `[未授权]` → agent 看到会主动劝用户 /auth-docs,不调用工具
- `[即将过期,refresh 剩 N 天]` → agent 照常用工具,但在回复末尾附一句温和提醒

### "直接回我"指令的遵从

只做 prompt 层服从(上面策略段已明写)。**不做工具层硬兜底**——Kimi 对清晰指令服从度应当够用;若后续发现真翻车,再加硬兜底。先避免过度设计。

---

## 7. 分步交付计划

### PR 1:OAuth 基建(~1 天)

范围:
- `feishu/oauth.py`(~180 行)
- `app.py` 新增 2 个路由(~60 行)
- `feishu/events.py` 拦截 `/auth-docs`(~30 行)
- 数据库迁移:新增 `feishu_oauth_tokens`、`feishu_oauth_states`(~40 行)

验收:
- [ ] 飞书开放平台已在"重定向 URL 白名单"加 callback URL
- [ ] `/auth-docs` 能走完完整授权流
- [ ] DB 里能看到 token 落盘
- [ ] 授权成功后飞书收到确认消息
- [ ] 过期 state 被自动清理

**不涉及**:任何 agent 工具、任何文档 API。

### PR 2:文档客户端 + create/read(~1 天)

范围:
- `feishu/docs_client.py`(~300 行,先不做 append/search/image)
- `agent/tools_docs.py`:只注册 `feishu_doc_create`、`feishu_doc_read`
- `agent/runner.py`:挂工具 + prompt 追加
- Markdown↔blocks 转换(不含图片)
- 30 份 markdown golden test

验收:
- [ ] 说"写个 Python 教程" → 飞书看到新文档
- [ ] 丢文档链接让 agent 读 → 能复述
- [ ] wiki 链接也能读
- [ ] markdown 边界 case(混合格式、表格)排版可接受

### PR 3:append + search + 图片(~1 天)

范围:
- `feishu_doc_append`、`feishu_doc_search`
- `upload_image` + image block 两步流程
- 图片路径 sandbox 校验 + 测试
- Prompt 里补充 append/search 的使用指引

验收:
- [ ] 让 agent 写带 mermaid 架构图的方案(agent 自己调 CLI 渲染为 PNG 再引用)
- [ ] 基于上次周报再改一版(append)
- [ ] 用自然语言引用过去文档 → search 命中
- [ ] 恶意路径(`../../etc/passwd`)被拦截

---

## 8. 风险清单

| # | 风险 | 谁处理 | 预案 |
|---|---|---|---|
| 1 | Kimi K2.5 的 tool_use 协议不完整,工具调用失败 | 我 | PR 1 部署后用极简工具先验工具调用能否通;不通则切回第三方 Anthropic→OpenAI 代理 |
| 2 | 回调 URL 白名单没配 → 授权 404 | 用户(按指引) | PR 1 启动前再提醒 |
| 3 | Markdown 转 blocks 边界 bug(代码特殊字符、表格换行) | 我 | 30 份真实 markdown golden test |
| 4 | Agent 过度写文档或漏写 | 我 | prompt 阈值约束;翻车后调 prompt |
| 5 | 飞书 docx API 字段漂移 | 我 | 按当前 v1 稳定版实现,出事即修 |
| 6 | 图片上传:sandbox 路径校验漏洞 | 我 | 复用 `tools_deliver.py` 的 `_is_inside`,另加专项测试 |

---

## 9. 决策记录(本次对话中敲定)

| # | 问题 | 结论 |
|---|---|---|
| 1 | 文档落点 | 用户个人飞书云空间,自动创建"AI 助手"文件夹 |
| 2 | 读取范围 | 默认读 AI 助手文件夹 + 用户丢链接进来能临时读 |
| 3 | 写/读/搜 | 全做,图片只做"写入" |
| 4 | 触发判断 | 模型智能判断 + 用户自然语言可覆盖 |
| 5 | 命令名 | `/auth-docs` |
| 6 | 授权成功页 | 纯文字 |
| 7 | 飞书内确认消息 | 推 |
| 8 | 读文档图片 | 不识别,用 alt/链接占位 |
| 9 | Blocks → markdown 格式 | A:转回 markdown |
| 10 | "直接回我"遵从机制 | 只做 prompt 层,不做硬兜底 |
| 11 | 写完后聊天回复风格 | 一句话摘要 + 链接 |
