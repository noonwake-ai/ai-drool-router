# 参与贡献

## 开发环境

```bash
python3 -m pip install -r detector/requirements.txt
cd web && npm ci && cd ..
```

## 提交前必须跑

```bash
python3 -m unittest discover -s detector/tests -t . -p 'test_*.py'
cd web && node --test src/*.test.js && npm run build
```

改了界面的话，顺手跑一下本地预览确认没跑版：

```bash
python3 scripts/dev_preview.py
```

## 代码约定

- **后端**：Python 标准库优先，新增第三方依赖要在 PR 里说明理由。所有对外错误走 `ProbeError`，并且必须能被 `redact()` 脱敏。
- **前端**：函数式组件 + hooks。所有面向用户的文案都要进 `web/src/i18n-core.js` 的中英两张表，不要在组件里写死字符串。
- **配置**：任何「部署方可能想改」的值都放 `config.example.json`，不要写死在代码里。

## 提交规范

- 一个 PR 做一件事，别把重构和功能混在一起
- 提交信息用中文，说清楚**为什么**改，而不只是改了什么
- 新增功能必须带测试；修 bug 请先加一个能复现的测试

## 涉及 Sub2API 写操作

如果你的改动会写 Sub2API（优先级、可调用状态等），请在 PR 描述里说明：

1. 触发条件是什么
2. 写完之后怎么回读校验
3. 失败时会不会影响已有流量
4. 如何关闭这个行为

**默认必须是关闭的。** 不要让部署方在不知情的情况下被改掉网关配置。

## 不要提交

- 任何真实密钥、cookie、token
- `config.json`、`.env`、`data/`、`credentials/`
- `node_modules/`、`dist/`、`__pycache__/`

CI 里有一条密钥形状的扫描，命中会直接失败。
