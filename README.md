# 居间小助手 · 公开联系人挖掘

面向**工程居间人**的轻量工具：输入一批目标企业 → 自动突破搜索引擎反爬 →
抓取公开网页 → 抽取业务部门联系人 / 电话 / 邮箱 / 关键人 → 表格展示 + CSV 导出。

## 技术栈（按本机实际情况选型）
- Python 3.13（managed）+ FastAPI + uvicorn
- **curl_cffi**（TLS 指纹伪装 `impersonate=chrome`，专治反爬，无需浏览器）
- BeautifulSoup + lxml（解析与抽取）

## 目录结构
```
jujian-assistant/
├── app.py              # FastAPI 后端（GET / 页面，POST /api/search）
├── scraper.py          # 反爬爬虫：多引擎回退 + 联系人抽取 + 并发/延时/代理
├── templates/
│   └── index.html      # 单页前端（批量输入 / 部门预设 / 结果表 / CSV 导出）
└── README.md
```

## 启动
依赖已装在 managed venv：`C:\Users\lenovo\.workbuddy\binaries\python\envs\default`

```bash
# 直接用 uvicorn
.workbuddy\binaries\python\envs\default\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

启动后浏览器打开 http://127.0.0.1:8000

> 端口被占用时先释放：在 PowerShell 用 `netstat -ano | findstr :8000` 找到 PID，
> 再 `Stop-Process -Id <PID> -Force`。

## 用法
1. 企业名单框：每行一家，可批量（如 10 家一起挖）。
2. 关注部门：选预设 chip 或自填（环保工程部 / EPC项目部 / 采购部 / 招标联系人…）。
3. 搜索引擎：默认 **Sogou**（中文企业结果最相关）；Sogou 偶发反爬时**自动回退**到
   Bing → Baidu，无需手动切换。
4. 点「开始挖掘」→ 结果表按相关度排序展示（来源/邮箱/电话/关键人/置信度）→「导出 CSV」。

命令行快速验证（无需开网页）：
```bash
.workbuddy\binaries\python\envs\default\Scripts\python.exe scraper.py "北京碧水源科技股份有限公司"
```

## 反爬策略（来自实测）
- TLS 指纹伪装（curl_cffi `impersonate=chrome`）过 JA3/TLS 检测，无需浏览器。
- 公司名加引号精确匹配 + 多意图查询（联系方式 / 项目对接人），提升相关性。
- 多引擎回退 + SERP 失败重试：Sogou/Bing/Baidu 任一被挑战则自动换源。
- 受控并发（Semaphore=4）+ 随机延时，不对单站打爆。
- 置信度过滤 + 噪声丢弃：命中公司名的页面标「高」，百科/UGC/政府门户噪音默认过滤。
- 可选代理：设环境变量 `SCRAPER_PROXY=http://host:port` 换 IP（住宅代理应对强反爬）。

## 实测结论（开发沙箱，境外 IP）
- 抽取逻辑已验证可拿到**真实公开联系方式**（如 `limu@cepec.cn` / `029-86531300` 中节能、
  `angguohua@originwater.com` 碧水源）。
- 沙箱为境外机房 IP：Bing 返回降级泛结果、Sogou 波动；**Baidu 最稳定**。
- 你本机（国内正常 IP）用 Sogou 默认引擎相关性最好，Baidu/Bing 作为兜底。

## 合规边界（务必遵守）
- 仅采集**公开**信息；不碰登录态、不破付费墙、不爬隐私字段。
- 遵守目标站点 `robots.txt` 与 ToS；控制频率，别压垮对方服务器。
- 输出仅供参考，商务对接前请自行核实真实性。

## 可扩展点
- 加数据源：招标网、采购网、天眼查/企查查公开页（需注意其 ToS 与反爬）。
- 加导出：Excel（openpyxl）、自动去重合并同企业联系人。
- 加持久化：结果入库（SQLite），支持历史查询。
- 加代理池 / 打码：对接住宅代理与验证码服务应对强反爬。
