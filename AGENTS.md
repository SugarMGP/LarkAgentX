# LarkX Agent Guide

## 这是什么

飞书**真实个人账号**网页版协议的 Python 实现（库，不是服务）：扫码/Cookie 登录、长连接收消息、发消息/回复/撤回/编辑、标已读、群聊与单聊、云空间与云文档。

唯一使用者是同机的 `feishu_connector`（另一个仓库），它通过 `pyproject.toml` 的 `larkx = { git = "…/LarkX.git", branch = "master" }` 依赖本库。**协议能力都放在这里，不要复制或 vendoring 到下游。**

本仓库是公开仓库：不要提交 cookie、凭证、二维码、真实聊天原文。

## 模块地图

| 文件 | 内容 |
|---|---|
| `auth.py` | 扫码登录（`QrLogin`）、Cookie 导入、票据、凭证读写（`~/.larkx/credentials.json`，可用 `LARKX_HOME` 改目录） |
| `client.py` | IM 网关客户端：protobuf over HTTP + WebSocket 长连接，`api(cmd, data)` 统一收发 |
| `region.py` | 国内版 feishu.cn 与国际版 larksuite.com 的域名差异（同一套协议，只换域名/语言） |
| `space.py` | 云空间与云文档：JSON over HTTP，和 IM 网关**不是一套协议** |
| `media.py` | 图片/文件上传（`upload_image`/`upload_file`）、消息资源下载（`download_message_resource`） |
| `proto/` | 协议资产：`lark_all_pb2.py`（消息定义）、`cmd_map.json`（命令号↔类型）、`gateway.py`（按名字解析命令） |

## 两套协议，别混

| | IM 网关 | 云空间 / 云文档 |
|---|---|---|
| 形态 | protobuf over HTTP + WebSocket | JSON over HTTP |
| 主机 | `internal-api-lark-api.<domain>` | `internal-api.<domain>`（explorer）/ `internal-api-space.<domain>`（box、docx、权限） |
| 鉴权 | 登录 cookie + ticket | 同一套登录 cookie |
| 写操作 | 不需要 CSRF | explorer/suite 接口要 `X-CSRFToken` |

文档站自己的源是 `poizon.feishu.cn`（云空间/文档请求实际发到这里；`internal-api-space` 同样可用）。

**origin 和 referer 必须一致**（都填同一个域），不一致会报 `referer check fail`。
**`X-CSRFToken` 大小写敏感**（`x-csrf-token` 会被拒 403）：值就是 `_csrf_token` cookie，任意一次 space/api 读接口的响应就会下发它；一次读接口可以同时拿到多个接口要用的 token。

## 已验证的能力

IM 网关：

| 命令 | cmd | 用途 |
|---|---|---|
| `users.PullUserProfileRequest` | 5017 | 拉单个用户资料（name/email/enName/departmentName） |
| `calendarevents.SearchUserByEmailCalendarRequest` | 1011020 | **邮箱 → user id 索引**，`ContactContents` 可重复（批量），返回按邮箱分键，`entityType=1` 是人 |
| `chats.PullChatsByIdsRequest` | 64 | 按 chat_id 取会话对象（`name`/`type`/`key`） |
| `chats.PullChatChattersByIDsRequest` | 5023 | **批量拿人名**：`chatterIds` 可重复，`chatId` 传 0，一次能拿几十个人 |
| `feed.PullFeedCardsRequest` | 1000 | 收件箱（最近会话 + 消息），`pullType=1` 第一页、`pullType=2`+cursor 翻页 |
| `chats.PullChatChattersRequest` | 44 | 群成员（单个会话） |

`LarkClient` 已经封好的通用方法（下游直接用，不要自己拼 `client.api`）：

| 方法 | 用途 |
|---|---|
| `pull_chats(chat_ids)` | 按 chat_id 批量取会话信息，返回 `{chat_id: 会话}` |
| `pull_feed(count, cursor)` | 收件箱：最近会话 + 消息；cursor 传上一页的 next_cursor |
| `pull_chatter_names(user_ids)` | 批量取昵称 `{user_id: 昵称}`（`chatId` 传 0，不限会话） |
| `get_user_profile(user_id)` | 单个用户资料（姓名/邮箱/英文名/部门），查不到返回 None |
| `search_user_ids_by_email(email)` | 按完整邮箱精确查人，返回命中的数字 user id 列表 |
| `client.space` | 同一份凭证下的 `SpaceClient`（云空间/文档） |

协议层的错误语义都在库里：`api()` 遇到 `status != 0` 抛 `ProtocolError`；空间接口的 HTTP 错误统一抛 `SpaceError`；`validate()` 里网络异常向上抛，不会被当成"凭证过期"。

云空间/文档：

| 接口 | 用途 |
|---|---|
| `GET /space/api/explorer/v3/children/list/` | 我的云空间列表，**根目录节点 = `type==4` 且 `token == obj_token` 的那个** |
| `GET /space/api/explorer/folder/children/` | 某个目录下的文件 |
| `GET /space/api/wiki/v2/tree/get_node/` | wiki 节点 → 真实文档 token、空间 id、标题 |
| `GET /space/api/docx/pages/client_vars` | 文档正文的所有块（`block_map` + `block_sequence`） |
| `POST /space/api/box/upload/prepare\|finish` + `box/stream/upload/merge_block` | 上传字节，换 `file_token` |
| `POST /space/api/import/create` + `GET import/result/{ticket}` | 把上传的文件转成云文档；`point.mount_type` 1=云空间文件夹、2=wiki 节点 |
| `GET /space/api/search/permission_candidates/?query=` | 云空间搜人/搜群，返回 `owner_id`/`owner_name`/`owner_type` |
| `POST /space/api/suite/permission/members/create/` | **给文档加协作者** |
| `GET /space/api/suite/permission/members/?token=&type=22` | 读文档的协作者列表 |
| `POST /space/api/suite/permission/members/delete/` | 移除协作者 |
| `POST /space/api/explorer/obj/delete/` | 删文档（参数 `obj_token` + `obj_type`） |

加协作者的细节（`SpaceClient.add_member`）：

- 表单编码，`owners` 是 JSON 数组 `[{"owner_id": "<飞书数字 user id>", "owner_type": 1, "permission": 4}]`
- `owner_type`：1=人、2=群
- `permission` 是位掩码：1=可阅读、2=可评论、4=可编辑、8=可分享、1024=完全权限；加"可编辑"后成员表里是 7（读+评论+编辑）
- 必须带 `X-CSRFToken`；`notify_lark=0` 表示不通知对方（链接要自己发）
- 混入无效 user id 会整个请求失败（`code=2`）；全是无效 id 反而返回成功但不生效——**要自己校验 `success_count`**

单聊没有群名（`Chat.name` 和 `i18nNames` 都是空的），"标题"就是对方昵称：

- 对方 id 从 `Chat.key` 取（格式 `"id1:id2"`，**顺序不固定**，取不是自己的那个）；`ownerId` 不可靠（自己建的会话里 ownerId 是自己）
- 昵称：`chats.PullChatChattersByIDsRequest` 一次批量拿（响应里**没有邮箱**），或用 users 表缓存

## 走不通的路（别再试）

- `users.SearchUsersByContactPointRequest`（cmd 5019，按联系方式搜人）→ **只回空包**，userProfiles 永远是空。cmd 表里 5019 还兼作 PUSH_RESOURCE。
- `users.BatchPullUserProfileRequest`（cmd 5026，批量拉资料）→ 同样**只回空包**。所以批量拉资料没有接口，只能一个个 `PullUserProfileRequest`。
- `contact.v1.*` 那批类型在协议资产里**不存在**；`contact.v1.SearchUserByContactPointRequest` 在 cmd 表里也没有命令号，构造不出来。
- `chats.PullContactsRequest(inChatId=…)` → 返回的是公共联系人/群，不是单聊对方，**不能拿来当会话标题**。
- `GET /space/api/explorer/root/` → 返回 `{"code":0,"data":{}}`，空的，拿不到根目录 token（要从 children/list 里找）。
- `GET /space/api/explorer/root/get/`、`/explorer/v2/root_folder/meta/` → 不是 404 页面就是 HTML，没有这个接口。
- `chats.PullChatChattersByIDsRequest` 只按 `chatterIds` 批量，`chatId` 传 0 就行，不用给真实会话。
- **改已有文档正文不用 WebSocket**：编辑器的实时协作通道（box-stream）只在浏览器里用，保存走 HTTP `POST https://poizon.feishu.cn/space/api/docx/blocks/user_change`（见下）。导入只能**新建**文档，不能覆盖已有文档。
- 要按 lark-cli 的形式改文档（块锚定的增删改），用 `lark-cli docs +update`（开放平台身份）不要自己造；但我们是真账号，只能走上面的 user_change 自己改。
- `users.BatchPullUserProfileRequest` 和 `users.SearchUsersByContactPointRequest` 一样只回空包；`users.PullUserProfileRequest`（单个）和 `calendarevents.SearchUserByEmailCalendarRequest`（按邮箱索引）能用。

## 改文档正文：已跑通的 user_change 协议

编辑器保存正文走一个 HTTP 接口（**不需要 WebSocket**），已用真实账号验证能改、能删、能整段替换。

```text
POST https://poizon.feishu.cn/space/api/docx/blocks/user_change
头：X-CSRFToken(=_csrf_token cookie)、Content-Type: application/json;charset=UTF-8、Referer: <docx url>
体：{"member_id":"10000000000000", "uuid":<uuid4>, "page_id":<文档 token>,
     "change_map":{"<块 id>":{"id":<块 id>, "version":<块当前版本>,
       "payload":{"ops":[{"p":["text"],"subType":{"t":"easysync","o":{
          "zone_changesets":{"0":"<changeset>"},
          "apool":{"numToAttrib":{"0":["author","<我的 user id>"]},"nextNum":1,
                    "attribToNum":{"author,<我的 user id>":0}}}}]}}}}
```

changeset 串格式（数字是 **base36 小写**，`0-9a-z`，`k`=20、`j`=19）：

```text
<itemType><zoneId>:<oldLen><长度差><ops>[#<charBank 长度>]$<插入的文字>
```

- `itemType`：`Z`=正文块（`R`/`C` 是别的类型）
- `zoneId`：默认 `"0"`，为 0 时省略
- `oldLen`：块当前正文长度
- `长度差`：变长写 `>N`、变短写 `<N`（N 是新旧长度差）
- `ops`：Etherpad 风格操作串，`=N` 保留 N 字、`-N` 删 N 字、`*N` 设样式 N
- `$` 后面是本次插入的文字

例子（块原文 20 个字符）：

| 动作 | changeset | 结果 |
|---|---|---|
| 末尾追加 ABC | `Z:k>3=k*0+3$ABC` | 原文+ABC |
| 删掉最后一个字 | `Z:k<1=j-1$` | 少最后一个字 |
| 整段替换 | `Z:k<j=0-k*0+j$新文字` | 只剩新文字 |

坑：

- 插入的正文要带样式：`*0+N`（0 号样式=author，N=插入字数）；**删掉的字不要带样式**（`*0-1` 会被服务端拒）。
- `member_id` 用常量 `10000000000000` 即可，随机 14 位数字也能过网关，但计算阶段会失败。
- `version` 用块的当前版本（`client_vars` 里每个块都有 `version`），版本不对会被拒。
- 块的正文结构：`data.text.initialAttributedTexts = {"attribs": {"0": "*0+23"}, "text": {"0": "正文"}}`。
- 服务端失败时返回 `{"code":4000028,"msg":"algorithm process fail"}`，多半是 changeset 语法或 version 不对。

## 不要留没人用的封装

`LarkClient` 的方法如果只有自己那半个调用方在用，就是负债：会烂掉，还会被下游当成可用能力直接调。删掉比留着好。判断"没人用"要 grep 整个仓库（含 tests）和下游仓库。

## 真账号验证

- 凭证默认在 `~/.larkx/credentials.json`；真账号验证用同机其它项目里的凭证文件，`LARKX_HOME=<目录>` 指过去即可。**不要把 cookie 写进仓库。**
- 测写操作前先确认有账号在跑；`LarkAuth.validate()` 返回 (ok, msg)。
- 涉及别人账号的副作用（加协作者、@别人）用完要清理：加了就用 `members/delete` 移掉，建的文档用 `explorer/obj/delete` 删掉。
- 单测不要连真实账号：用 monkeypatch 换 `larkx.space.requests.get/post`（见 `tests/test_space.py`）。

## 约束与要求

- **改动前先在 LarkX 里跑通，再让 connector 对接**。connector 依赖的是 git master，所以改完要：LarkX 提交并推 master → 在 connector 里 `uv lock --upgrade-package larkx && uv sync`。
- 只做文字消息及其中的 AT 节点；图片、文件、表情之外的能力不要自行扩展。
- 不要引入 gRPC、消息队列、数据库；本库是纯客户端。
- 保持单租户假设；不要伪造 open_id/union_id/通讯录 user_id。
- 改动要小：新协议能力放进本库，下游只做适配；不要为"以后可能用"预留抽象。
- 不要破坏既有行为和测试；新逻辑（分支、循环、解析）至少留一个测试。
- 内部调试日志按调用方要求**不脱敏**，但日志别进仓库。

## 开发约定

- Python 3.11，`uv` 管理依赖；`uv run pytest -q` 必须全绿。
- 协议接口先在真账号上验证，再写单测（单测用假响应，不打网络）。
- 新增协议能力时：更新 `space.py` 顶部的"已用真实账号验证的能力"清单 + 写清走不通的路。
- 提交信息用简短英文 Conventional Commits。
