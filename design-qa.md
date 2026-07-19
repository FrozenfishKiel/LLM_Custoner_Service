# 正式电商客服前端 Design QA

- source visual truth path: `C:/Users/frozenfish/.codex/visualizations/2026/07/18/019f74b1-8774-7a21-abaf-924ffe087a92/ecommerce-support-direction-2.png`
- implementation screenshot path: `D:/Projects/llm_customer_service/docs/reports/integration/evidence/customer-frontend-viewport.png`
- viewport: Codex in-app browser 当前视口 `1265 x 709`；pytest 浏览器覆盖 `1440 x 1024`
- state: 未登录首屏，展示登录/注册/找回/重置入口、订单与 AI 售后顾问工作台
- full-view comparison evidence: 已打开选定方向 2 和实现截图，按整体信息架构、视觉层级、色彩、间距、组件密度对比
- focused region comparison evidence: 重点检查左侧深蓝导航、顶部标题、认证卡片、订单卡片、AI 售后顾问面板；未做像素级覆盖，因为源视觉稿是方向稿，不是严格 Figma 标注

**Findings**

- 无 P0/P1/P2 阻塞项。

**已修复的 P2**

- [P2] 首版认证区横向溢出
  Location: `.auth-grid`
  Evidence: 初次截图中 4 列认证卡片超出右侧视口，右侧内容被裁切。
  Fix: 改为稳定的 2 列布局，给 `body` 和 `.workspace` 增加横向溢出保护；复查后 `scrollWidth == clientWidth`。

**五项保真检查**

- Fonts and typography: 使用微软雅黑/Segoe UI 系统字体，标题权重、层级和字号接近方向 2；中文可读性优先。
- Spacing and layout rhythm: 左侧导航、顶部标题、卡片网格、订单/客服双栏保持后台式电商节奏；认证流程因上线功能需要占用更多首屏空间，属于可接受偏离。
- Colors and visual tokens: 深蓝导航、蓝色主按钮、白色卡片、浅灰页面底色与方向 2 一致。
- Image quality and asset fidelity: 用户要求商品图只做背景氛围；实现使用非关键信息的柔和商品色块，不依赖图片作为信息来源。
- Copy and content: 全部主要用户提示为中文，覆盖登录、注册、找回、重置、改密、聊天、退出、错误状态。

**Interaction QA**

- 浏览器真实路径已验证：打开 `/login`、未登录聊天、登录、认证聊天、重置聊天、退出。
- 风险路径已验证：未登录 401、CSRF 403；auth/chat 后端回归覆盖 429/503。
- Console: pytest 浏览器 E2E 记录 `unexpected_console_errors: 0`。

**Implementation Checklist**

- [x] 修复横向溢出
- [x] 保存浏览器截图
- [x] 保存浏览器量化 JSON
- [x] 跑前端契约测试
- [x] 跑浏览器 E2E

**Follow-up Polish**

- P3：如果后续有品牌设计系统，可把 SHOPWISE 标识、图标库和真实商品缩略图替换成正式资产。
- P3：移动端可以再做单独的 390px 精细布局 QA。

final result: passed
