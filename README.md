# wife-news · 每日精选

每天北京时间 12:00，自动把「抗衰、医美、科技、女性觉醒、哲学」的全球动态翻译成中文，推送到微信，并带 AI 语音朗读 + 打分。

## 栏目与来源

| 栏目 | 来源 | 语言 |
|---|---|---|
| 抗衰与长寿 | ScienceDaily、Google News（anti-aging/longevity、日韩抗衰） | 英文→中文 |
| 医美 | Google News（cosmetic/aesthetic medicine）、ScienceDaily | 英文→中文 |
| 科技 | 量子位、爱范儿、少数派 | 中文 |
| 女性觉醒 | Google News（women empowerment/feminism） | 英文→中文 |
| 哲学 | Aeon、Big Think | 英文→中文 |

约 24 条/天。英文内容由 DeepSeek 翻译成中文并写 150 字概述。

## 一次性配置（账号全部用本人的）

1. **仓库设为公开** + **开启 Pages**（Settings → Pages → Source 选 GitHub Actions）
2. **三个 secret**：`SCT_KEY`（Server酱）、`DEEPSEEK_API_KEY`（DeepSeek）、`JSONBIN_KEY` + `JSONBIN_BIN_ID`（打分存储）
3. Actions 页 → `daily-news` → Run workflow 验证

## 改内容

`scripts/daily_news.py` 顶部的 `FEEDS` 是栏目与来源，`NOISE` 是垃圾词黑名单，`150 字左右` 是概述长度（语音时长旋钮）。

## 注意

- 细分领域（抗衰/医美/女性/哲学）中文源极少，主要靠英文源 + DeepSeek 翻译。
- Google News 关键词会有一定噪声，用 👎 打分逐步收敛。
- cron 可能迟到 5~30 分钟；60 天无提交会停用定时任务。
