#!/usr/bin/env python3
"""每日精选：抗衰/医美/科技/女性觉醒/哲学，中文呈现 + AI 语音朗读。

流程（GitHub Actions）:
    抓 RSS → DeepSeek 翻译+概述 → 生成中文 Markdown
    → edge-tts 合成语音 mp3 + 播放页 → 推送到个人微信（Server酱）

用法:
    python3 daily_news.py --dry-run                 # 抓取+翻译+打印，不合成不推送
    python3 daily_news.py --out brief.md --site site  # 产出 brief.md 与 site/index.html、site/latest.mp3
    python3 daily_news.py --send brief.md           # 读取 brief.md 并推送到微信

环境变量:
    SCT_KEY              Server酱 SendKey（SCT 开头）
    DEEPSEEK_API_KEY     DeepSeek API key（缺失则跳过翻译，保留英文）
    SITE_URL             语音播放页地址，默认 https://lone-liu.github.io/wife-news
"""
import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEEDS = [
    {
        "section": "抗衰与长寿",
        "limit": 6,
        "sources": [
            ("ScienceDaily", "https://www.sciencedaily.com/rss/health_medicine.xml", 2),
            ("GNews 抗衰", "https://news.google.com/rss/search?q=anti-aging%20OR%20longevity%20when%3A2d&hl=en-US&gl=US&ceid=US:en", 2),
            ("GNews 日韩抗衰", "https://news.google.com/rss/search?q=Japan%20OR%20Korea%20anti-aging%20research%20when%3A2d&hl=en-US&gl=US&ceid=US:en", 2),
        ],
    },
    {
        "section": "医美",
        "limit": 4,
        "sources": [
            ("GNews 医美", "https://news.google.com/rss/search?q=cosmetic+OR+aesthetic+medicine+when:2d&hl=en-US&gl=US&ceid=US:en", 3),
            ("ScienceDaily", "https://www.sciencedaily.com/rss/health_medicine.xml", 1),
        ],
    },
    {
        "section": "科技",
        "limit": 6,
        "sources": [
            ("量子位", "https://www.qbitai.com/feed", 2),
            ("爱范儿", "https://www.ifanr.com/feed", 2),
            ("少数派", "https://sspai.com/feed", 2),
        ],
    },
    {
        "section": "女性觉醒",
        "limit": 4,
        "sources": [
            ("GNews 女性", "https://news.google.com/rss/search?q=women%20empowerment%20OR%20feminism%20when%3A2d&hl=en-US&gl=US&ceid=US:en", 4),
        ],
    },
    {
        "section": "哲学",
        "limit": 4,
        "sources": [
            ("Aeon", "https://aeon.co/feed.rss", 2),
            ("BigThink", "https://bigthink.com/feed/", 2),
        ],
    },
]

MAX_ITEMS = 36
MAX_AGE_HOURS = 36
SUMMARY_MAX = 80
UA = "Mozilla/5.0 (compatible; daily-news-bot/3.0)"
VOICE = "zh-CN-XiaoxiaoNeural"
SITE_URL = os.environ.get("SITE_URL", "https://lone-liu.github.io/wife-news")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

FOCUS = ("中国", "China", "BIM", "建筑", "工程", "EPC", "房地产")
NOISE = (
    "喜加一", "免费领取", "限时免费", "优惠券", "打折", "促销", "秒杀",
    "开奖", "中奖", "彩票", "招聘启事", "更正", "讣告",
    "Substack", "for Rent", "Retail Space", "Shop/Retail", "BIGBANG", "GiiiRIP",
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s\u3000,，.。!！?？:：;；、\"'“”‘’（）()\[\]【】《》<>·—\-_|/\\]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def clean(text):
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def clip(text, limit):
    """截断到 limit；优先句末收口，其次词边界，一律不加省略号。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("。", "！", "？", ". ", "! ", "? "):
        idx = head.rfind(sep)
        if idx >= limit // 2:
            return head[: idx + len(sep)].strip()
    idx = max(head.rfind(" "), head.rfind("，"), head.rfind("、"), head.rfind(","))
    if idx > limit // 2:
        return head[:idx].rstrip()
    return head.rstrip()


def norm_key(title):
    return PUNCT_RE.sub("", (title or "").lower())


def is_noise(title):
    return any(word in title for word in NOISE)


def is_cjk(text):
    return bool(CJK_RE.search(text or ""))


def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def normalize_dt(d):
    if d is None:
        return None
    if d.tzinfo is not None:
        return d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = summary = link = pub = None
        for child in node:
            name = child.tag.split("}")[-1]
            if name == "title":
                title = child.text
            elif name in ("description", "summary", "content") and summary is None:
                summary = child.text
            elif name == "link":
                href = child.attrib.get("href")
                if link is None or child.attrib.get("rel") in (None, "alternate"):
                    link = href or child.text
            elif name in ("pubDate", "date", "published", "updated"):
                pub = child.text
        items.append((clean(title), clean(summary), (link or "").strip(), parse_date(pub)))
    return items


def collect():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    seen = set()
    result = []
    errors = []

    for spec in FEEDS:
        picked = []
        pool_cap = spec["limit"] + 6   # 多抓候选，供打分后替换
        for label, url, per_source in spec["sources"]:
            if len(picked) >= pool_cap:
                break
            try:
                entries = parse_feed(fetch(url))
            except Exception as e:
                errors.append("%s / %s: %s" % (spec["section"], label, type(e).__name__))
                continue

            dated = [(t, s, l, normalize_dt(d)) for (t, s, l, d) in entries]
            dated.sort(key=lambda x: x[3] or dt.datetime.min, reverse=True)
            dated = ([x for x in dated if any(k in x[0] for k in FOCUS)]
                     + [x for x in dated if not any(k in x[0] for k in FOCUS)])

            taken = 0
            for title, summary, link, when in dated:
                if taken >= per_source + 2 or len(picked) >= pool_cap:
                    break
                if not title or not link or is_noise(title):
                    continue
                if when is not None and when < cutoff:
                    continue
                key = norm_key(title)
                if not key or key in seen:
                    continue
                seen.add(key)
                taken += 1
                picked.append({
                    "source": label,
                    "max": per_source,
                    "title": title,
                    "desc": summary,
                    "summary": clip(summary, SUMMARY_MAX),
                    "link": link,
                })
        if picked:
            result.append({"section": spec["section"], "limit": spec["limit"], "items": picked})

    return result, errors


def fetch_article(url, timeout=12):
    """抓取网页正文文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>|</li>|</tr>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", "", raw)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def extract_main(text, limit=1200):
    """挑出正文主体段落（跳过导航短文本），截断到 limit 字符。"""
    paras = [pp.strip() for pp in (text or "").split("\n")]
    paras = [pp for pp in paras if len(pp) > 25]
    return " ".join(paras)[:limit]


def get_body(it):
    """抓原文，失败或过短则退回 RSS 简介。"""
    try:
        t = extract_main(fetch_article(it["link"]))
        if len(t) > 120:
            return t
    except Exception:
        pass
    return it.get("desc") or ""


def _deepseek(key, prompt, attempts=3):
    """调用 DeepSeek，带重试；返回 (parsed_json_or_None, error_msg_or_None)。"""
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    for a in range(attempts):
        req = urllib.request.Request(DEEPSEEK_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(json)?\s*|\s*```$", "", content).strip()
            return json.loads(content), None
        except urllib.error.HTTPError as e:
            retryable = e.code in (429, 500, 502, 503, 504)
            if retryable and a < attempts - 1:
                time.sleep(2 * (a + 1))
                continue
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                detail = ""
            return None, "HTTP %s: %s" % (e.code, detail)
        except Exception as e:
            if a < attempts - 1:
                time.sleep(2 * (a + 1))
                continue
            return None, "%s: %s" % (type(e).__name__, e)
    return None, "重试后仍失败"


def process(groups, key):
    """抓原文 → DeepSeek 分批翻译+概述+关键词。失败保留原文，不中断。"""
    flat = []
    for g in groups:
        for it in g["items"]:
            flat.append(it)
    if not flat or not key:
        return 0, 0, None

    # 并行抓取原文正文
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        bodies = list(ex.map(get_body, flat))

    done = 0
    failed_batches = 0
    last_err = None
    BATCH = 6

    for start in range(0, len(flat), BATCH):
        chunk = flat[start:start + BATCH]
        chunk_bodies = bodies[start:start + BATCH]
        payload = [{"title": it["title"], "body": chunk_bodies[i]} for i, it in enumerate(chunk)]
        prompt = (
            "你是资深新闻编辑。下面是 JSON 数组，每个对象是一条新闻的 {title, body}。\n"
            "对每条严格输出 {title, summary, keywords}：\n"
            "1) title 完整简体中文标题，英文译成简体中文并保留完整意思，中文保留原文。\n"
            "2) summary 一段 150 字左右的简体中文概述，覆盖核心事实、关键人物/数字与结论，信息具体不空洞。\n"
            "3) keywords 2-3 个简体中文关键词（字符串数组）。\n"
            "只输出 JSON 数组，不要任何解释、不要 markdown 代码块：\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        translated, err = _deepseek(key, prompt)
        if translated is None:
            failed_batches += 1
            last_err = "第%d批失败: %s" % (start // BATCH + 1, err)
            print("[warn] %s" % last_err, file=sys.stderr)
        else:
            for it, tr in zip(chunk, translated):
                if tr.get("title"):
                    it["title"] = clean(tr["title"])
                if tr.get("summary"):
                    it["summary"] = clean(tr["summary"])
                if tr.get("keywords"):
                    it["keywords"] = [clean(k) for k in tr["keywords"] if clean(k)]
            done += len(translated)
            time.sleep(1)  # 批次间间隔，避免连发限流

    if failed_batches:
        return len(flat), done, "%d/%d 批失败，已保留该批原文" % (failed_batches, (len(flat) + BATCH - 1) // BATCH)
    return len(flat), done, None


def _norm_scores(d):
    return {
        "downvote": {k: int(v) for k, v in d.get("downvote", {}).items()},
        "blocked": [str(k) for k in d.get("blocked", [])],
        "upvote": {k: int(v) for k, v in d.get("upvote", {}).items()},
    }


def load_scores():
    """读取打分配置：优先 JSONBin（网页打分），否则回退 scores.json。"""
    key = os.environ.get("JSONBIN_KEY", "").strip()
    bin_id = os.environ.get("JSONBIN_BIN_ID", "").strip()
    if key and bin_id:
        try:
            req = urllib.request.Request("https://api.jsonbin.io/v3/b/%s" % bin_id)
            req.add_header("X-Master-Key", key)
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "ignore")).get("record", {})
            return _norm_scores(d)
        except Exception:
            pass
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scores.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return _norm_scores(d)
    except Exception:
        return {"downvote": {}, "blocked": [], "upvote": {}}


def score_item(title, summary, scores):
    """返回该条目的分数；命中拉黑或 3 次低分返回 None（丢弃）。"""
    text = (title or "") + " " + (summary or "")
    for kw in scores["blocked"]:
        if kw and kw in text:
            return None
    s = 0
    for kw, c in scores["upvote"].items():
        if kw and kw in text:
            s += c * 10
    for kw, c in scores["downvote"].items():
        if kw and kw in text:
            if c >= 3:
                return None
            s -= c * 100
    return s


def apply_scores(groups, scores):
    """打分过滤与排序：拉黑剔除、低分降权、高分优先，按源取满配额。"""
    out = []
    for group in groups:
        by_source, order = {}, []
        for it in group["items"]:
            src = it["source"]
            if src not in by_source:
                by_source[src] = []
                order.append(src)
            by_source[src].append(it)
        final = []
        for src in order:
            items = by_source[src]
            scored = []
            for it in items:
                sc = score_item(it["title"], it["summary"], scores)
                if sc is None:
                    continue
                scored.append((sc, it))
            scored.sort(key=lambda x: x[0], reverse=True)
            maxn = items[0]["max"]
            final.extend(it for _, it in scored[:maxn])
        group["items"] = final[: group.get("limit", len(final))]
        if group["items"]:
            out.append(group)

    total = sum(len(g["items"]) for g in out)
    for group in reversed(out):
        while total > MAX_ITEMS and group["items"]:
            group["items"].pop()
            total -= 1
        if total <= MAX_ITEMS:
            break
    return [g for g in out if g["items"]]


def build_markdown(groups, today, with_voice=True):
    lines = ["## 每日精选 %s" % today]
    if with_voice:
        lines.append("")
        lines.append("> 🔊 [点击收听今日 AI 语音版](%s)" % SITE_URL)
    for group in groups:
        lines.append("")
        lines.append("### %s" % group["section"])
        last_source = None
        for it in group["items"]:
            if it["source"] != last_source:
                lines.append("**%s**" % it["source"])
                last_source = it["source"]
            entry = "- [%s](%s)" % (it["title"], it["link"])
            if it["summary"]:
                entry += " —— %s" % it["summary"]
            lines.append(entry)
    return "\n".join(lines).rstrip() + "\n"


def build_tts_text(groups):
    parts = ["每日简报。"]
    for g in groups:
        parts.append(g["section"] + "。")
        for it in g["items"]:
            parts.append(it["title"] + "。" + ((it["summary"] or "") + "。"))
    return "\n".join(parts)


def synth(text, out_mp3):
    fd, tf = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        subprocess.run(
            [sys.executable, "-m", "edge_tts", "--voice", VOICE, "--file", tf, "--write-media", out_mp3],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(tf)


def build_site(groups, today, site_dir):
    os.makedirs(site_dir, exist_ok=True)
    synth(build_tts_text(groups), os.path.join(site_dir, "latest.mp3"))

    jk = os.environ.get("JSONBIN_KEY", "").strip()
    jb = os.environ.get("JSONBIN_BIN_ID", "").strip()

    cards = []
    for g in groups:
        cards.append('<h2>%s</h2>' % html.escape(g["section"]))
        last_source = None
        for it in g["items"]:
            if it["source"] != last_source:
                cards.append('<h3 class="src">%s</h3>' % html.escape(it["source"]))
                last_source = it["source"]
            kws = [k for k in (it.get("keywords") or []) if k]
            kws_json = html.escape(json.dumps(kws, ensure_ascii=False), quote=True)
            chips = "".join('<span class="kw">#%s</span>' % html.escape(k) for k in kws)
            cards.append(
                '<div class="item" data-kws="%s">'
                '<div class="t"><a href="%s" target="_blank" rel="noopener">%s</a></div>'
                '<div class="s">%s</div>'
                '<div class="kws">%s</div>'
                '<div class="btns"><button class="up" title="喜欢，多推这类">👍 多推</button>'
                '<button class="down" title="不喜欢，少推这类">👎 少推</button></div>'
                '</div>'
                % (kws_json, html.escape(it["link"]), html.escape(it["title"]),
                   html.escape(it["summary"] or ""), chips)
            )
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日简报 %(today)s</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px 20px 60px;color:#1a1a1a;line-height:1.7;background:#fafafa}
h1{font-size:20px} h2{font-size:16px;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:24px}
h3.src{font-size:13px;color:#999;margin:14px 0 4px}
.item{background:#fff;border-radius:10px;padding:12px 14px;margin:8px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.t a{font-size:15px;font-weight:600;color:#111;text-decoration:none}
.s{font-size:13px;color:#555;margin:6px 0}
.kw{font-size:11px;color:#0a66c2;background:#eef4fc;border-radius:10px;padding:1px 8px;margin-right:4px;display:inline-block}
.btns{margin-top:8px}
.btns button{border:1px solid #ddd;background:#fff;border-radius:16px;padding:5px 14px;font-size:13px;margin-right:8px;cursor:pointer}
.btns button:active{background:#f0f0f0}
.audio{position:sticky;top:0;background:#fafafa;padding:8px 0;border-bottom:1px solid #eee}
.speed{margin-top:6px;font-size:12px;color:#666;display:flex;align-items:center;gap:6px}
.speed button{border:1px solid #ddd;background:#fff;border-radius:12px;padding:2px 10px;font-size:12px;cursor:pointer}
.speed button.on{background:#0a66c2;color:#fff;border-color:#0a66c2}
</style></head><body>
<div class="audio">
  <audio id="player" controls src="latest.mp3" style="width:100%%"></audio>
  <div class="speed"><span>语速</span>
    <button data-rate="1">1x</button>
    <button data-rate="1.5" class="on">1.5x</button>
    <button data-rate="2">2x</button>
  </div>
</div>
<h1>每日简报 %(today)s · 可打分 · AI 语音</h1>
<p style="font-size:13px;color:#888">点 👍 多推这类、👎 少推这类；同一关键词被 👎 3 次将不再推送。</p>
%(body)s
<script>
var player = document.getElementById("player");
player.playbackRate = 1.5;
document.querySelectorAll(".speed button").forEach(function(b){
  b.addEventListener("click", function(){
    player.playbackRate = parseFloat(b.dataset.rate);
    document.querySelectorAll(".speed button").forEach(function(x){ x.classList.remove("on"); });
    b.classList.add("on");
  });
});
var BIN = { key: "%(jk)s", id: "%(jb)s" };
async function getRecord(){
  var r = await fetch("https://api.jsonbin.io/v3/b/"+BIN.id, {headers:{"X-Master-Key":BIN.key}});
  if(!r.ok) throw new Error("读取失败 "+r.status);
  return ((await r.json()).record) || {};
}
async function putRecord(rec){
  var r = await fetch("https://api.jsonbin.io/v3/b/"+BIN.id, {
    method:"PUT", headers:{"X-Master-Key":BIN.key,"Content-Type":"application/json"},
    body: JSON.stringify(rec)
  });
  if(!r.ok) throw new Error("写入失败 "+r.status);
}
async function vote(el, key, btnClass){
  if(!BIN.key || !BIN.id){ alert("打分存储未配置，请先设置 JSONBin"); return; }
  var kws; try { kws = JSON.parse(el.dataset.kws); } catch(e){ kws = []; }
  if(!kws.length){ alert("本条无关键词，无法打分"); return; }
  var btn = el.querySelector("."+btnClass);
  var old = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  try {
    var rec = await getRecord();
    rec[key] = rec[key] || {};
    for(var i=0;i<kws.length;i++){ rec[key][kws[i]] = (rec[key][kws[i]]||0) + 1; }
    await putRecord(rec);
    btn.textContent = old + " ✓";
  } catch(e){
    btn.disabled = false; btn.textContent = old;
    alert("打分失败：" + e.message);
  }
}
document.querySelectorAll(".item").forEach(function(item){
  item.querySelector(".up").addEventListener("click", function(){ vote(item,"upvote","up"); });
  item.querySelector(".down").addEventListener("click", function(){ vote(item,"downvote","down"); });
});
</script>
</body></html>""" % {"today": today, "body": "\n".join(cards), "jk": jk, "jb": jb}
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def push(markdown, title, key):
    url = "https://sctapi.ftqq.com/%s.send" % key
    body = urllib.parse.urlencode({"title": title[:32], "desp": markdown}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--site")
    ap.add_argument("--send")
    args = ap.parse_args()

    tz = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(tz).strftime("%Y-%m-%d")

    # —— 纯发送模式：只读已有文件并推送 ——
    if args.send:
        with open(args.send, encoding="utf-8") as f:
            markdown = f.read()
        key = os.environ.get("SCT_KEY", "").strip()
        if not key:
            print("缺少环境变量 SCT_KEY", file=sys.stderr)
            return 3
        res = push(markdown, "每日精选 %s" % today, key)
        print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
        return 0 if res.get("code") in (0, "0") else 1

    groups, errors = collect()
    for e in errors:
        print("[warn] %s" % e, file=sys.stderr)

    total = sum(len(g["items"]) for g in groups)
    if total == 0:
        print("没有抓到任何条目，放弃本次推送", file=sys.stderr)
        return 2

    # 翻译
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        n_src, n_done, err = process(groups, key)
        if err:
            print("[warn] %s" % err, file=sys.stderr)
        else:
            print("[info] 翻译并概述 %d 条" % n_done, file=sys.stderr)
    else:
        print("[warn] 未设置 DEEPSEEK_API_KEY，跳过处理，保留原文标题与摘要", file=sys.stderr)

    # 打分过滤：拉黑剔除、低分降权、高分优先
    groups = apply_scores(groups, load_scores())

    markdown = build_markdown(groups, today, with_voice=bool(args.site))
    print(markdown)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(markdown)

    if args.site:
        build_site(groups, today, args.site)
        print("[info] 语音与播放页已生成到 %s" % args.site, file=sys.stderr)

    if args.dry_run:
        print("[dry-run] 共 %d 条，未推送" % total, file=sys.stderr)
        return 0

    # 非 dry-run 且带 SCT_KEY 时顺带发送（本地完整测试用；Actions 里用 --send 分步）
    key = os.environ.get("SCT_KEY", "").strip()
    if key:
        res = push(markdown, "每日精选 %s" % today, key)
        print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
        return 0 if res.get("code") in (0, "0") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
