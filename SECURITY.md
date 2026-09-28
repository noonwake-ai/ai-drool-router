# 安全策略

## 报告漏洞

如果你发现了安全问题，请不要开公开 issue。用 GitHub 的
[私密漏洞报告](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
提交，或者直接联系仓库维护者。

请在报告里写清：影响范围、复现步骤、你的验证环境。**不要附带任何真实密钥。**

## 这个项目的安全模型

部署方需要理解三件事：

1. **Sub2API 管理员密钥是最高权限凭据。** 它能读写你网关里的账号配置。本项目的 worker 需要它，Web 进程不需要也不应该拿到它。
2. **看板默认是公开的。** 它展示账号名、平台、模型、分数与检测结果。不展示密钥，但账号名本身可能属于敏感信息——公网部署前请确认这点没问题。
3. **匿名暂停接口默认关闭。** `web.public_controls: true` 会让任何能访问页面的人暂停或恢复某个账号的检测。除非你确实需要，否则保持关闭。

## 项目已经做的防护

- 上游错误在写库前统一脱敏：密钥、令牌、邮箱、上游 URL 全部替换
- 浏览器只拿得到脱敏后的公开投影，拿不到任何凭据
- Web 进程被建议用独立系统用户运行，并屏蔽私有库与凭据目录
- 模型生成的 HTML 在 `sandbox allow-scripts` 的 iframe 中执行，禁止网络、表单与同级框架
- 对 Sub2API 只有两个写操作，且都带回读校验
- `routing.write_priority` / `routing.write_callable` 默认关闭

## 部署方需要自己负责的

- 用 systemd 等机制把 Web 进程和密钥隔开（`deploy/` 里的单元已经这么做）
- 不要把 `config.json`、`.env`、`data/` 提交到任何仓库
- 公网部署时自行决定是否加访问控制
- 定期轮换 Sub2API 管理员密钥
