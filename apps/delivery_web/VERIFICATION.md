**C 取件页验证记录**

- `npm run typecheck` 通过。
- `npm test -- --run` 通过（13 个测试：api 8 个、取件页 5 个）。
- `npm run build` 通过并产出 `dist/`（构建产物，不提交）。
- 本地 node 版本为 23，依赖要求 22/24，测试与构建在 `node:22-alpine` 容器内执行，与 `infra/delivery-web.Dockerfile` 一致。
- 真实环境验证（https://delivery.orangeai.cfd ）：
  - 运营方登记库存邮箱并分配、签发取件码后，在浏览器输入取件码即显示邮箱地址、会话剩余时间、邮件主题/发件人/时间与纯文本正文。
  - 同一个取件码再次使用返回 401 `DELIVERY_PICKUP_INVALID`，页面显示「取件码无效、已使用或已过期。」
  - 错误码请求由浏览器直接发起时返回 `{"detail":{"code":"DELIVERY_PICKUP_INVALID"}}`，与前端错误映射一致。
- 取件码与会话令牌只保存在内存（React state），未写入 localStorage/Cookie；页面不渲染 HTML 正文。
