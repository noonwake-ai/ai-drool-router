# 部署手册

面向第一次把 AI 流口水降智调度放到服务器上的场景。目标是：**不动你现有的任何服务**，只新增一个只读看板和一个定时 worker。

## 0. 前置检查

在开始之前确认：

- [ ] Sub2API 已经在跑，并且你能登进管理后台
- [ ] 你手上有 Sub2API 的管理员 API Key
- [ ] 服务器能出网访问各模型上游（如果你给账号配了代理，Sub2API 里也配了）
- [ ] Python 3.10+ / Node 20+ 可用（Node 只在构建前端时用得到）

先验证网关可达：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://你的-sub2api-域名/health
```

返回 `200` 说明路由正确，再往下走。

## 1. 目录规划

这套部署用四个固定位置，互不重叠：

| 路径 | 放什么 | 权限 |
|---|---|---|
| `/opt/drool-detector/current` | 代码（软链到版本目录） | root 可写 |
| `/etc/drool-detector/` | 配置与密钥 | 0600 / 0750 |
| `/var/lib/drool-detector/` | 运行数据 | worker 可写 |
| `/etc/systemd/system/` | 服务单元 | root |

创建目录：

```bash
sudo install -d -m 0755 /opt/drool-detector/releases
sudo install -d -m 0750 /etc/drool-detector
sudo install -d -m 0750 /var/lib/drool-detector
sudo install -d -m 0750 /var/lib/drool-detector/controls
sudo install -d -m 0700 /var/lib/drool-detector/private
sudo install -d -m 0750 /var/lib/drool-detector/public
```

## 2. 专用系统账号

worker 和 web 用不同账号，web 读不到私有库和密钥：

```bash
sudo groupadd --system drool-detector
sudo useradd --system --gid drool-detector --no-create-home --shell /usr/sbin/nologin drool-worker
sudo useradd --system --gid drool-detector --no-create-home --shell /usr/sbin/nologin drool-web

sudo chown -R drool-worker:drool-detector /var/lib/drool-detector
sudo chmod 0750 /var/lib/drool-detector
sudo chmod 0700 /var/lib/drool-detector/private
```

## 3. 部署一个版本

```bash
VERSION=20260929-v1
RELEASE=/opt/drool-detector/releases/$VERSION

sudo install -d -m 0755 "$RELEASE"
sudo rsync -a --delete \
  --exclude node_modules --exclude .git --exclude data \
  ./ "$RELEASE/"

cd "$RELEASE/web" && sudo npm ci && sudo npm run build

sudo ln -sfn "$RELEASE" /opt/drool-detector/current
```

验证它能起来：

```bash
cd /opt/drool-detector/current
sudo -u drool-worker python3 -c "import detector.monitor; print('ok')"
```

## 4. 配置与密钥

```bash
sudo install -m 0640 -o root -g drool-detector config.example.json /etc/drool-detector/config.json
sudoedit /etc/drool-detector/config.json
```

必须改的三处：

```jsonc
{
  "base_url": "https://你的-sub2api-域名",
  "data_dir": "/var/lib/drool-detector",
  "identity_salt": "换成一串别人猜不到的随机字符"
}
```

如果看板要暴露到公网，**再改一处**：

```jsonc
"privacy": {"account_names": "alias"}
```

账号名常常直接暴露你的真实上游（比如供应商全名、带公司后缀的账号名）。`alias` 会把它换成稳定的假名，历史记录仍然对齐；`masked` 则只保留首尾字符。只在内网用可以保持 `full`。

`identity_salt` 用来把账号 ID 哈希成公开 ID。换掉它会让看板上的账号 ID 全部变化（历史记录仍保留，但前端缓存会重新取），所以**一旦上线就别再改**。

再放密钥：

```bash
sudo install -m 0600 deploy/drool-detector.env.example /etc/drool-detector/drool-detector.env
sudoedit /etc/drool-detector/drool-detector.env
```

填进去：

```ini
SUB2API_ADMIN_KEY=真实密钥
DROOL_BASE_URL=https://你的-sub2api-域名
DROOL_DATA_DIR=/var/lib/drool-detector
DROOL_CONFIG=/etc/drool-detector/config.json
```

```bash
sudo chown root:drool-detector /etc/drool-detector/drool-detector.env
sudo chmod 0640 /etc/drool-detector/drool-detector.env
```

> 密钥只被 worker 和 pricing 读。web 单元的 `InaccessiblePaths` 已经把这个目录屏蔽掉了。

## 5. 装服务

```bash
sudo install -m 0644 deploy/systemd/*.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now drool-detector-web.service
sudo systemctl enable --now drool-detector-worker.timer
sudo systemctl enable --now drool-detector-sync.timer
sudo systemctl enable --now drool-detector-pricing.timer
```

## 6. 首次验证

**第一步：只同步账号，不花钱。**

```bash
sudo systemctl start drool-detector-sync.service
sudo journalctl -u drool-detector-sync.service -n 30 --no-pager
```

看到 `metadata_refreshed` 就说明网关连通、账号读到了。这时候打开看板应该能看到账号列表，但还没有检测结果。

**第二步：跑一轮真实的。**

```bash
sudo systemctl start drool-detector-worker.service
sudo journalctl -u drool-detector-worker.service -f
```

> 想先花钱少一点，就在 `config.json` 里只留一个平台，或者用 `group_ids` 圈一两个账号。

**第三步：确认看板。**

```bash
curl -s http://127.0.0.1:4191/api/state | head -c 300
```

## 7. 反向代理

用 Caddy 的话：

```caddyfile
drool.你的域名 {
    encode gzip zstd
    reverse_proxy 127.0.0.1:4191
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**公网部署请先读 README 的「安全边界」。** 尤其是 `web.public_controls`：打开之后任何能访问页面的人都能暂停/恢复账号的检测。只想给别人看结果的话，保持默认关闭。

## 8. 日常运维

```bash
# 看 worker 最近一次结果
sudo journalctl -u drool-detector-worker.service -n 50 --no-pager

# 看定时器下次触发时间
systemctl list-timers 'drool-detector-*' --all

# 看 web 是否活着
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4191/healthz
```

## 9. 升级与回滚

升级：

```bash
VERSION=20261001-v2
RELEASE=/opt/drool-detector/releases/$VERSION
sudo install -d -m 0755 "$RELEASE"
sudo rsync -a --exclude node_modules --exclude .git --exclude data ./ "$RELEASE/"
cd "$RELEASE/web" && sudo npm ci && sudo npm run build
sudo ln -sfn "$RELEASE" /opt/drool-detector/current
sudo systemctl restart drool-detector-web.service
```

回滚：

```bash
sudo ln -sfn /opt/drool-detector/releases/上一个版本 /opt/drool-detector/current
sudo systemctl restart drool-detector-web.service
```

**数据和配置都不用动。** 回滚只换代码。

## 10. 排错

| 现象 | 先查什么 |
|---|---|
| 看板空白 | `data/public/state.json` 是否存在；`journalctl -u drool-detector-sync` 有没有 `SYNC_FAILED` |
| 一直「暂无数据」 | worker 有没有真的跑过一轮；账号是否被 `group_ids` / `exclude_names` 排除了 |
| 全部请求失败 | 服务器能不能直连上游；账号在 Sub2API 里配的代理是否生效 |
| 一直「预算截断」 | 该平台模型太慢，把 `drawing_platform_seconds` 调大 |
| 优先级没有变化 | `routing.write_priority` 还是默认的 `false` |
| web 起不来端口占用 | `ss -lntp \| grep 4191`，改 `web.port` |

启动前的自检脚本：

```bash
cd /opt/drool-detector/current
sudo -u drool-worker env $(cat /etc/drool-detector/drool-detector.env | xargs) \
  python3 -m detector.monitor --metadata-only
```

这条命令不发起模型请求，只读账号，适合快速验证「配置对不对、密钥通不通」。

## 附：启用 GitHub Actions

仓库里已经准备好 `.github/workflows/ci.yml`，但它需要推送方的 token 带有 `workflow`
scope 才能提交（GitHub 对 OAuth App 的限制）。如果首次推送时被拦下，执行：

```bash
gh auth refresh -h github.com -s workflow
git add .github/workflows/ci.yml
git commit -m "工程：启用 CI"
git push
```

CI 会跑：Python 3.10 / 3.12 两套后端测试、前端测试与构建、Docker 镜像构建并从
容器里验证离线预览，以及公开投影的密钥/内网地址检查。
