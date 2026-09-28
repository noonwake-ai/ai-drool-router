# AI 安装指引

> **这份文档是写给 AI 助手执行的。**
>
> 如果你是人，把下面这句话复制给你手边的 AI（Codex、Claude Code、Cursor、Windsurf 都行）：
>
> ```
> 帮我部署 AI 流口水检测：https://raw.githubusercontent.com/noonwake-ai/ai-drool-detector/main/docs/install.md
> ```

---

## 你的任务

把 AI 流口水检测部署到用户的机器上，并**验证它真的产生了检测结果**。

这不是一个"跑完命令就算完"的任务。没有看到真实的检测记录，就不算装好。

## 开始前必须先问清楚三件事

不要自己猜，也不要拿默认值糊过去。用一次对话问完，然后开工：

1. **Sub2API 的地址**（例如 `https://coding.example.com`）
2. **Sub2API 管理员 API Key**
3. **装在哪台机器、用 Docker 还是 systemd**

如果用户只想先看看界面，跳过这三件事，直接跑第 2 步的离线预览。

## 红线

这几条比"装完"更重要：

- **不要把密钥写进任何提交、日志、截图或回复里。** 需要回显时只显示前 4 位。
- **不要碰用户现有的任何服务、容器、数据库或配置。** 只新增，不修改。
- **不要打开 `routing.write_priority` / `routing.write_callable`**，除非用户明确说"让这个工具去改我网关的调用优先级"。默认是只算分、不改网关。
- **不要跳过验证。** 每一步都要用命令的实际输出确认，不要假设成功。
- **不要为了让它"看起来成功"而放宽判定条件。**

## 步骤

### 0. 先摸清环境

```bash
python3 -V                 # 需要 3.10 或更高
node -v                    # 只有自己构建前端时才需要
docker --version           # 有 Docker 就走 Docker 路线
```

如果 Python 低于 3.10，用 Docker 路线，或者装一个新版本的 Python。**不要为了迁就旧版本去改依赖文件。**

顺手确认网关可达：

```bash
curl -s -o /dev/null -w '%{http_code}\n' <用户给的 Sub2API 地址>/health
```

200 就继续。不是 200 就先解决连通性，别往下走。

### 1. 拿代码

```bash
git clone https://github.com/noonwake-ai/ai-drool-detector.git
cd ai-drool-detector
```

### 2. 先让用户免费看一眼（强烈建议）

这一步不连网关、不需要密钥、不花一分钱：

```bash
python3 scripts/dev_preview.py          # 打开 http://127.0.0.1:4191/
```

如果机器上没有浏览器，用 curl 确认它活着：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4191/api/state
```

把网址给用户看一眼，确认这就是他想要的东西，再往下走。

### 3A. Docker 路线（有 Docker 就用这条）

```bash
cp config.example.json config.json
```

编辑 `config.json`，至少改三处：`base_url` 填用户给的网关地址、`data_dir` 保持 `/data`、
`identity_salt` 换成一段随机字符串。

`platforms` 里按用户实际在跑的模型改 `model` 和 `effort`。**只留用户真的有的平台**，
把其他的 `enabled` 设成 `false`——少测一家就少花一份钱。

```bash
printf 'SUB2API_ADMIN_KEY=%s\n' '<密钥>' > .env
chmod 600 .env
docker compose up -d
docker compose ps                       # 三个服务都应该是 Up
```

### 3B. systemd 路线

按 [deploy.md](deploy.md) 走。关键点：

- 代码放 `/opt/ai-drool-detector/current`（软链到版本目录，方便回滚）
- 配置和密钥放 `/etc/ai-drool-detector/`，权限 `0640`
- 数据放 `/var/lib/ai-drool-detector/`
- Web 进程和 worker 用不同系统账号，web 读不到密钥

### 4. 第一次只读同步（不花 token）

这一步只拉账号清单，**一个模型请求都不发**：

```bash
# Docker
docker compose run --rm worker sync

# 直接跑
export SUB2API_ADMIN_KEY='<密钥>'
python3 -m detector.monitor --metadata-only
```

期望看到 `"status": "metadata_refreshed"` 并且 `accounts` 大于 0。

**如果这里是 0，先停下。** 说明 `platforms` 的范围没配好，或者账号分组对不上，
继续往下走只会白花钱。

### 5. 跑一轮真实的

```bash
# Docker
docker compose run --rm worker worker

# 直接跑
python3 -m detector.monitor --source initial
```

跑完会打印 `"status": "complete"`，里面有 `scheduled_tests`（这轮发了多少检测）。

> 提醒用户：这一步开始真的花钱。想省着试就把 `platforms` 只留一个平台，
> 或者用 `group_ids` 圈一两个账号。

### 6. 确认结果真的落库了

```bash
python3 -c "
import json; d=json.load(open('data/public/state.json'))
print('账户数:', len(d['accounts']))
for a in d['accounts']:
    print(' ', a['name'], a['model'], '->', a['history']['candy'][-1]['status'])
"
```

**要求：至少有一个账户的最后一条结果不是 `empty`。** 全是 `empty` 说明检测没真正跑起来。

### 7. 起看板

```bash
# Docker 会自动起，直接确认
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4191/api/state

# 直接跑
python3 -m detector.server --dist web/dist --public ./data/public
```

再把页面内容也验一遍：

```bash
curl -s http://127.0.0.1:4191/api/state | head -c 200
```

### 8. 装成常驻服务（如果用户要长期用）

Docker：`docker compose up -d` 已经把 web / worker / pricing 都带起来了。

systemd：

```bash
sudo systemctl enable --now drool-detector-web.service
sudo systemctl enable --now drool-detector-worker.timer
sudo systemctl enable --now drool-detector-sync.timer
sudo systemctl enable --now drool-detector-pricing.timer
systemctl list-timers 'drool-detector-*' --all
```

### 9. 反代（可选）

要把看板给别人看，就套一层反向代理。参考 `deploy/Caddyfile.fragment`。

**公网暴露前，向用户说明这两件事：**

1. 看板会显示账号名。建议把 `privacy.account_names` 改成 `alias` 或 `masked`。
2. `web.public_controls` 打开后，任何能访问页面的人都能暂停/恢复检测。默认关闭。

得到用户确认再改。

## 装完要汇报什么

用用户的母语，简短说清：

- 装在哪、用什么方式（Docker / systemd）
- 看板地址
- 这一轮测了几家、几家通过
- 有没有平台没配上（例如网关里压根没有这个模型）
- `routing.write_priority` 是开还是关（默认关，说明"目前只算分，不改你的网关"）
- 怎么暂停某一家的检测

**不要**把密钥、完整账号列表或内网地址贴在汇报里。

## 排错

| 现象 | 先查什么 |
|---|---|
| 第 4 步账号数是 0 | `group_ids` / `exclude_names` 是不是把账号全排除了 |
| 一直是 `REQUEST_TIMEOUT` | 该平台模型太慢，把 `budgets.drawing_platform_seconds` 调大 |
| 页面空白 | `data/public/state.json` 存不存在；看板进程有没有起 |
| 全部请求失败 | 服务器能不能直连上游；Sub2API 里配的代理是给 Sub2API 用的，本工具要自己出网 |
| `MODEL_NOT_CONFIGURED` | `platforms` 里的 model 名跟网关里的对不上 |
| `PLATFORM_UNSUPPORTED` | `protocol` 填错了，可选值见 README |
| 优先级没变化 | 预期行为：`routing.write_priority` 默认是 false |

## 卸载

只删自己装的东西：

```bash
# Docker
docker compose down
docker volume rm <项目名>_drool-data    # 这会删掉检测历史，先跟用户确认

# systemd
sudo systemctl disable --now drool-detector-web.service \
  drool-detector-worker.timer drool-detector-sync.timer drool-detector-pricing.timer
sudo rm /etc/systemd/system/drool-detector-*
sudo systemctl daemon-reload
```

**不要**顺手删用户的其他东西。

## 给 AI 的最后一条

如果你在执行过程中发现这份文档少了什么、哪条命令跑不通、哪个坑没写，
请把这个发现告诉用户——也可以在仓库开个 issue。
这份文档的价值就在于：下一个 AI 不用再踩同一个坑。
