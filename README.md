# 飞书 Claude Code 机器人

把 Claude Code 的 agent 能力接到飞书,后端用智谱 GLM-5.1。
你在飞书里像跟人聊天一样跟它对话,它会读文件、改代码、跑命令、上网搜索、做定时任务。

## 这是什么

- **入口**:飞书企业自建应用机器人(私聊)
- **大脑**:Claude Agent SDK(等同于 Claude Code 的核心引擎)
- **模型**:智谱 GLM-5.1(通过 Anthropic 兼容端点)
- **能力**:Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch/Agent/TodoWrite + 自定义定时任务工具
- **部署**:Railway(容器持久运行,带 Volume 持久化)

## 你能做什么

- "帮我写一个 Python 爬虫,抓 hacker news 首页前 30 条" → 它真的会写代码、跑、给你结果
- "我刚 clone 了 my-app 这个仓库,你帮我看看登录逻辑有没有 bug" → 它会读源码、分析、报告
- "每天早上 8 点检查 GitHub 我那个仓库有没有新 issue,有的话总结推给我" → 它会注册一个定时任务,到点自动跑并推送给你

## 你需要准备的东西

1. **一台能上 GitHub 的电脑**(用来推代码,不需要会写代码)
2. **Railway 账号**(你已经有了)
3. **GitHub 账号**(用来托管这份代码,Railway 从 GitHub 拉)
4. **飞书企业管理员权限**(创建自建应用要)
5. **智谱开放平台账号**(获取 GLM API key)
6. **20 分钟时间**

---

## 第一步:获取智谱 GLM API Key

1. 打开 https://open.bigmodel.cn,注册并完成实名
2. 进入控制台 → API Keys → 创建新密钥
3. 复制保存(只显示一次),格式类似 `xxxxxxxxxxxxxxx.xxxxxxxxxxxx`
4. **充值一点余额**(GLM 是按 token 收费的,自用一个月几块到几十块)
5. 顺便看一眼"模型 → GLM-5.1"页面,确认这个模型对你的账号开放

> 💡 如果未来 GLM 改了型号名,只需要在 Railway 后台改 `ANTHROPIC_DEFAULT_OPUS_MODEL` 这个环境变量,代码不用动。

---

## 第二步:在飞书开放平台创建企业自建应用

### 2.1 创建应用

1. 用企业管理员账号登录 https://open.feishu.cn
2. 左侧菜单 → "开发者后台" → "创建企业自建应用"
3. 填:
   - 应用名称:`Claude Code`(随便起)
   - 应用描述:任意
   - 应用图标:任意
4. 创建后,记下 **App ID** 和 **App Secret**(在"凭证与基础信息"里)

### 2.2 开通能力 → 机器人

1. 进入应用 → 左侧"添加应用能力" → "机器人"打勾,确认

### 2.3 配置权限

进入 "权限管理",勾选下面这些权限,然后**点页面顶部"申请发布版本"**:

- `im:message`(读取与发送消息)
- `im:message.group_at_msg`(可选,如果你以后要群聊)
- `im:message.p2p_msg`(接收用户私聊)
- `im:message:send_as_bot`(以机器人身份发消息 — **定时任务主动推送必须**)

### 2.4 配置事件订阅

进入 "事件与回调":

1. **请求地址**:暂时先空着,等部署完 Railway 拿到 URL 再填
2. **加密策略**(可选但推荐开):
   - 点"生成 Encrypt Key",复制保存(后面要填到环境变量)
   - 点"生成 Verification Token",复制保存
3. **添加事件**:点"添加事件" → 搜索 `im.message.receive_v1`(接收消息) → 添加

### 2.5 应用版本发布(企业内可见)

进入 "版本管理与发布" → "创建版本" → 填版本号(如 `1.0.0`) → 提交审核(企业自建应用通常自审批,管理员确认后秒过)

发布成功后,**在飞书里就能搜到这个机器人,加它为好友**。但是现在它还不会回话,因为还没部署后端。

---

## 第三步:把代码推到 GitHub

### 3.1 在 GitHub 创建一个**私有**仓库

仓库名随便,比如 `my-feishu-cc`。**一定要选 Private**(里面会有你的 API key 等敏感信息的影子,虽然 .gitignore 已经排除了 `.env`,但 Private 是双保险)。

### 3.2 推代码

用你电脑上的终端(Mac 自带 Terminal):

```bash
cd ~/Desktop/feishu-cc
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin git@github.com:你的用户名/my-feishu-cc.git
git push -u origin main
```

如果遇到问题,可以让 Claude 帮你解决——就发它 git 命令的报错就行。

---

## 第四步:在 Railway 部署

### 4.1 新建项目

1. 登录 Railway → "New Project" → "Deploy from GitHub repo"
2. 选你刚才创建的 `my-feishu-cc` 仓库
3. Railway 会自动识别 Dockerfile 并开始构建

### 4.1.1 可选:新增 browser service

如果你想让 agent 调用真实浏览器,需要在同一个 Railway 项目里再加一个 service:

1. 再次点击 "New Service"
2. 仍然选择当前仓库
3. 把 **Root Directory** 留空
4. 把 **Dockerfile Path** 改成 `browser/Dockerfile`
5. 给这个 service 单独加一个 Volume,挂载到 `/data`

这个 browser service 需要至少这些环境变量:

```env
BROWSER_SERVICE_TOKEN=一串随机长字符串
BROWSER_PUBLIC_BASE_URL=https://你的-browser-service 域名
DATA_DIR=/data
```

主 bot service 还要补这两个变量,这样 agent/bot 才能调用 browser service:

```env
BROWSER_SERVICE_BASE_URL=https://你的-browser-service 域名
BROWSER_SERVICE_TOKEN=和 browser service 保持一致
```

### 4.2 加 Volume(必须!不加重启就丢数据)

1. 项目页面 → 选中你的服务 → "Settings" → "Volumes"
2. "New Volume":
   - **Mount Path**:`/data`
   - **Size**:1 GB(够了,不够再加)
3. 创建后等 Railway 自动重启

### 4.3 填环境变量

在服务的 "Variables" 标签 → "Raw Editor" → 粘贴下面内容(把所有 `your_xxx` 替换成你的真实值):

```env
ANTHROPIC_AUTH_TOKEN=你的智谱_api_key
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.1
ANTHROPIC_DEFAULT_SONNET_MODEL=glm-5-turbo
ANTHROPIC_DEFAULT_HAIKU_MODEL=glm-4.5-air
GLM_VISION_MODEL=glm-5v-turbo
GLM_VISION_BASE_URL=https://api.z.ai/api/paas/v4/chat/completions
API_TIMEOUT_MS=3000000
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_ENCRYPT_KEY=你的_encrypt_key_可空
FEISHU_VERIFICATION_TOKEN=你的_verification_token_可空

# 把你自己的 open_id 填成管理员。审批通知会主动发到你这里。
FEISHU_ADMIN_OPEN_IDS=ou_your_open_id_here

# 可选:历史兼容。这里的用户会在启动时直接预置为已开通。
FEISHU_ALLOWED_OPEN_IDS=

DATA_DIR=/data
AGENT_MAX_DURATION_SECONDS=1800
SCHEDULE_DAILY_TRIGGER_LIMIT=50
LOG_LEVEL=INFO
```

保存后,Railway 会自动重新部署。

### 4.4 拿到 webhook URL

部署成功后,在服务的 "Settings" → "Networking" → "Public Networking" → "Generate Domain"。

Railway 会给你一个域名,比如 `my-feishu-cc-production.up.railway.app`。

**你的飞书 webhook URL 是**:`https://my-feishu-cc-production.up.railway.app/feishu/webhook`

测试一下:打开 `https://my-feishu-cc-production.up.railway.app/health`,应该看到 `{"ok": true}`。看不到说明没部署成功,去 "Deployments" 标签看日志。

---

## 第五步:回飞书填 webhook URL

1. 回到飞书开放平台 → 你的应用 → "事件与回调"
2. **请求地址**:填上面拿到的 URL
3. 点"保存",飞书会发一次 `url_verification` 请求过去验证。**看到"验证成功"才算 OK**

如果验证失败,八成是这两个原因:
- 域名写错(检查有没有 `https://` 和 `/feishu/webhook` 后缀)
- 加密 Encrypt Key 配错(要么飞书后台和环境变量都不填,要么两边一致)

---

## 第六步:把你自己设成管理员

1. 先把 `FEISHU_ADMIN_OPEN_IDS` 临时填成你自己的 open_id
2. 如果你还不知道自己的 open_id,有两个办法:
   - 用飞书开放平台的调试工具/通讯录接口查
   - 或者先沿用旧方式,把自己的 open_id 临时写进 `FEISHU_ALLOWED_OPEN_IDS`,部署后在飞书里发 `/whoami`,拿到真实值后再改回 `FEISHU_ADMIN_OPEN_IDS`
3. 保存环境变量并等 Railway 自动重部署
4. 在飞书里给 bot 发 `/help`,确认你能看到管理员命令:
   - `/approve <open_id>`
   - `/reject <open_id> [原因]`

> 一旦你自己是管理员,后面其他同事就不需要再改环境变量了,他们直接 `/apply` 即可。

---

## 第七步:玩起来

现在你可以:

- `/help` —— 看完整命令列表
- `/status` —— 看自己的开通状态
- `/project current` —— 看当前在哪个项目
- `/project new mywebsite` —— 新建项目
- `/project clone https://github.com/foo/bar.git` —— 克隆 git 仓库
- `帮我写一个 hello world Python 脚本并跑一下` —— 直接对话
- `每天早上 9 点检查我 GitHub 上 my-app 仓库的新 PR` —— 创建定时任务
- `/cron list` —— 看所有定时任务
- `/stop` —— 中断当前正在跑的长任务

---

## 常见问题

### 机器人不回话

按这个顺序排查:
1. Railway 后台 → "Deployments" → 看最新部署是否 success
2. Railway 后台 → 服务 → "Logs" 标签 → 实时日志,看你发消息后有没有打印
3. 如果普通用户看到的是权限提示,让他发 `/apply`;如果你收不到审批通知,检查 `FEISHU_ADMIN_OPEN_IDS` 是否填对
4. 如果日志显示 `decrypt failed`,飞书后台的 Encrypt Key 跟环境变量对不上
5. 如果日志显示 `create message failed`,飞书 App Secret 错了或者权限没批

### 机器人回得很慢

正常的——GLM-5.1 在长 agent 任务下单次响应可能 30 秒到几分钟。机器人会先回一句"🤔 思考中…",然后边干活边推送进度。

如果完全没动静超过 5 分钟,发 `/stop` 中断。

### Bash 命令被拦了

机器人内置了一份危险命令黑名单(rm -rf 根目录、curl 内网、写系统文件等)。被拦时机器人会提示原因,Claude 通常会自己换种方式重试。

如果你确定某条命令应该放行,可以编辑 `security/bash_blocklist.py` 调整规则,推送代码,Railway 会自动重新部署。

### 数据存哪了

所有数据都在 Railway Volume 的 `/data` 目录:
- `/data/sandbox/users/<你的open_id>/<项目名>/` — 项目工作目录
- `/data/sessions/` — Claude 会话历史
- `/data/feishu-cc.db` — 项目状态、定时任务元数据
- `/data/scheduler.db` — APScheduler 的 job store
- `/data/audit.log` — 所有 Bash 命令的审计日志

Volume 在你删除 Railway 服务前不会丢。

### 钱怎么烧的

两块成本:
1. **Railway 容器运行**:按 CPU/RAM/网络计费,自用每月大概 $3-8
2. **GLM API**:按 token 计费,长聊天月十几到几十块人民币

GLM API 比 Anthropic 便宜十倍以上,真正的大头其实是 Railway 的容器时间。

### 我想加同事用

现在已经支持审批式多人私聊:
1. 同事先私聊 bot,发送 `/apply`
2. bot 会主动私聊你一条审批通知
3. 你在和 bot 的私聊里回复 `/approve <open_id>` 或 `/reject <open_id> [原因]`
4. 审批结果会自动通知对方
5. 每个用户的数据仍按 open_id 隔离,互不可见

未来如果想支持群聊,要去 `feishu/events.py` 把 `is_allowed` 里"chat_type != p2p 直接拒绝"那段去掉,然后设计群聊的权限策略。这个目前没做。

---

## 文件结构(供你或未来的 Claude 修改时参考)

```
feishu-cc/
├── app.py                    # FastAPI 入口、命令路由
├── auth/
│   └── store.py              # 访问审批状态(SQLite)
├── config.py                 # 环境变量加载
├── feishu/
│   ├── client.py             # 飞书 API 封装(基于 lark-oapi)
│   └── events.py             # 事件解密、解析、去重、私聊过滤
├── agent/
│   ├── runner.py             # Claude Agent SDK 集成核心
│   ├── hooks.py              # 工具调用拦截(Bash 黑名单等)
│   └── tools_schedule.py     # 自定义 schedule MCP 工具
├── project/
│   ├── manager.py            # /project 命令实现
│   └── state.py              # 项目状态、session_id 持久化
├── scheduler/
│   ├── store.py              # 定时任务元数据 + APScheduler 实例
│   └── runner.py             # 定时任务触发逻辑
├── security/
│   └── bash_blocklist.py     # Bash 命令安全黑名单
├── Dockerfile
├── railway.json
├── requirements.txt
├── .env.example
└── README.md
```

---

## 如何让 Claude 自己改这份代码

你可以把这个仓库 clone 到本地,然后用 Claude Code 打开它。Claude Code 能读懂自己写的代码,你可以让它:

- "把 Bash 黑名单里的 sudo 拦截放开,我有些场景需要"
- "把图片/视频分析提示词调得更偏产品拆解一点"
- "把消息发送从纯文本改成飞书卡片,让工具进度更好看"

代码改完后:
```bash
git add .
git commit -m "改动说明"
git push
```

Railway 会自动重新部署。

---

## 已知限制

1. **当前支持文本、图片和常见视频文件**——语音和其他未识别文件仍不会自动分析
2. **定时任务用的是用户当前对话 session**——可能污染对话上下文,以后可以独立 session
3. **没有成本上限保护**——如果 Claude 进入死循环狂跑工具,token 会烧很多。Railway 那边可以设月度预算上限作为兜底
4. **没有跨容器持久化的客户端池**——容器重启后,所有正在跑的 agent 任务会丢(但 session 历史和定时任务不会丢,下次说话就接着原来的上下文了)
5. **群聊未启用**——只私聊
6. **视频分析依赖容器内 `ffmpeg`**——本仓库的 Dockerfile 已安装,如果你改基础镜像别漏掉它
