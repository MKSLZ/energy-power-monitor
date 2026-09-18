# 能源电力市场监控日报 · 部署指南

本项目是一个全自动流水线：定时抓取公开行情与新闻 →（可选）LLM 做事件影响推演 → 用 Jinja2 渲染成静态 HTML → 自动发布到 GitHub Pages。覆盖 石油 / 天然气·LNG / 煤炭 / 电力 四大品种。

---

## 一、实测源可用性（2026-09-18 16:38，无 key 在线跑一次，如实记录，不夸大）

| 状态 | 源 | 实测结果 |
|---|---|---|
| ✅ 实抓成功 | 腾讯 qt.gtimg.cn 实时行情 | Brent $98.84、WTI $95.67、HH（美国天然气）$2.99，另带黄金/白银。 |
| ✅ 实抓成功 | 国内财经快讯 + 政府门户 | 财联社 / 证券时报 / 新浪财经、国家能源局 / 发改委 / 中电联，按「油气煤电」关键词过滤后有数。 |
| ❌ 不可达 | 国际新闻 RSS | 路透 / 新华社 / OilPrice 等候选 RSS 当前不可达，页面相应位置标 N/A。 |
| ⚠️ 无公开接口 / 反爬，标 N/A 或沿用 | 欧洲气电 | TTF / JKM / EPEX 实时价无公开免费接口；GIE AGSI 无 key 时只拿到 `gas_day` 元数据，满库度 N/A。 |
| ⚠️ 无公开接口 / 反爬，标 N/A 或沿用 | 国内煤电现货 | 秦港 Q5500、SHPGX、广东电力交易中心现货（门户反爬/滞后，按要求标数据日期 + N/A）。 |
| ⚠️ 只能定性 | 天气 / 长江来水 | 拿不到逐日数值，给精确定性描述，数值 N/A。 |
| ⚠️ 需额外 key | EIA | 需配置 `EIA_API_KEY` 才有数，否则标 N/A。 |

> 单源失败不会让整轮崩溃，只会在对应区块标 N/A 或沿用上期。无 `LLM_API_KEY` 时页面自动走**「数据速览版」**并在报头明确注明「本期AI影响推演不可用/未配置Key」，保证任何区块不空白。

---

## 二、部署步骤

1. **建仓并推送**
   在 GitHub 建一个**公开（public）**仓库，把本仓库 push 上去。
   当前云机无 git 凭据，请在你本地（或有 token 的环境）执行：
   ```bash
   git remote add origin https://github.com/<用户名>/<仓库名>.git
   git branch -M main
   git push -u origin main
   ```

2. **配置 Secrets**
   仓库 `Settings → Secrets and variables → Actions`，新建以下 Secret：
   - `LLM_BASE_URL`
   - `LLM_API_KEY`
   - `LLM_MODEL`
   - （可选）`EIA_API_KEY`

   推荐提供商（免费额度即可起步）：

   - **Groq**（推荐）：去 [groq.com](https://groq.com) 开通并拿 API key
     - `LLM_BASE_URL = https://api.groq.com/openai/v1`
     - `LLM_MODEL = llama-3.3-70b-versatile`
   - **Google Gemini**（备选，OpenAI 兼容模式）：
     - `LLM_BASE_URL = https://generativelanguage.googleapis.com/v1beta`
     - `LLM_MODEL = gemini-2.0-flash`
     - 注：走 OpenAI 兼容模式接入；如不通，以 Google 官方文档最新兼容端点为准。

3. **开启 GitHub Pages**
   仓库 `Settings → Pages → Build and deployment → Source` 选择 **GitHub Actions**。

4. **手动跑一次**
   进入 `Actions` 页，选中 `energy-daily-update` workflow，点 `Run workflow`。成功后即可访问：
   ```
   https://<用户名>.github.io/<仓库名>/
   ```

5. **定时刷新**
   workflow 已内置 cron：**北京时间每 3 小时的第 13 分**跑一次（UTC 换算写在 `.github/workflows/update.yml` 注释里）。
   注意：GitHub 定时任务可能延迟数分钟到十几分钟，属正常现象。

---

## 三、费用

- **公开仓库**：GitHub Actions 运行分钟数与 GitHub Pages 均免费。
- **私有仓库**：GitHub Pages 需付费计划（Pro/Team/Enterprise）。

---

## 四、故障排查

- **Actions 红叉**：点开看具体日志。最常见原因：Secrets 没填对 / LLM 返回 401 鉴权失败 / 某数据源超时。单源超时不影响整轮，页面只会把该源标 N/A。
- **某品种显示 N/A**：说明该源当前抓不到、需 key 或门户反爬，**这不是 bug**，对应源见上文「实测源可用性」表。
- **Pages 不更新**：确认 ① Pages Source 已选 `GitHub Actions`；② workflow 运行成功（绿色勾）；③ 首页是 `site/index.html`。
- **访问提示**：`github.io` 在中国大陆访问可能不稳定，必要时可自行套代理或换国内静态托管。
