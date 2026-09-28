## 这个 PR 做了什么

<!-- 一句话说清目的。如果是修 bug，请写清触发条件。 -->

## 为什么

<!-- 为什么需要这个改动。 -->

## 验证

- [ ] `python3 -m unittest discover -s detector/tests -t . -p 'test_*.py'`
- [ ] `cd web && node --test src/*.test.js && npm run build`
- [ ] 改了界面的话，用 `python3 scripts/dev_preview.py` 看过实际渲染

## 涉及 Sub2API 写操作？

- [ ] 不涉及
- [ ] 涉及（请补全下面四项）

<!--
1. 触发条件：
2. 回读校验方式：
3. 失败时会不会影响已有流量：
4. 怎么关掉：
-->

## 检查

- [ ] 没有提交任何真实密钥、cookie、token
- [ ] 更新了相关文档（README / docs）
- [ ] 新增行为带了测试
