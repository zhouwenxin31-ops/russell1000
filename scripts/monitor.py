#!/usr/bin/env python3
"""
罗素1000 美股异动日报 — 完整版
每日盘后由 GitHub Actions 自动运行：
1. 获取罗素1000成分股列表
2. 批量下载当日行情，找出涨幅前30 / 跌幅前30
3. 为每只股票抓取最新新闻标题（原因）
4. 把真实数据注入 HTML 模板 → docs/index.html
5. GitHub Actions 推送到 GitHub Pages（固定网址）
6. 同时发送邮件通知
"""
import os, json, time, re, smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import yfinance as yf
import pandas as pd
import requests

# ── 环境变量配置（GitHub Secrets）────────────────────────────
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD= os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO      = os.environ.get("EMAIL_TO", "")
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "") or "465")
NEWS_API_KEY  = os.environ.get("NEWS_API_KEY", "")
PAGES_URL     = os.environ.get("PAGES_URL", "")
OPENAI_API_KEY= os.environ.get("OPENAI_API_KEY", "")
TOP_N = 30

# ── 罗素1000成分股（兜底列表）──────────────────────────────────
FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK-B","JPM","UNH",
    "XOM","JNJ","V","PG","MA","AVGO","HD","CVX","MRK","ABBV","LLY","PEP",
    "COST","KO","WMT","BAC","CRM","TMO","MCD","CSCO","ACN","ABT","NFLX",
    "AMD","ADBE","WFC","TXN","DHR","NKE","PM","UPS","NEE","RTX","AMGN",
    "QCOM","SBUX","LIN","INTU","BMY","CAT","GE","HON","IBM","GS","BLK",
    "SPGI","DE","AXP","GILD","SYK","ISRG","PLD","ELV","ADI","VRTX","ZTS",
    "CI","MDLZ","TGT","MO","SO","DUK","CL","ITW","EMR","AON","SHW",
    "F","GM","FCX","USB","PNC","TFC","COF","MCO","MSCI","ICE","CME","FIS",
    "FI","PYPL","UBER","ABNB","SHOP","SNOW","PLTR","DDOG","MDB","NET","ZS",
    "CRWD","OKTA","RIVN","ENPH","FSLR","WM","RSG","HUM","CVS","MCK",
    "IDXX","DXCM","EW","BSX","BDX","RMD","HCA","REGN","BIIB","MRNA",
    "SPG","AMT","CCI","EQIX","DLR","PSA","NUE","STLD","DVN","EOG","COP",
    "MPC","VLO","OXY","SLB","HAL","DIS","CMCSA","T","VZ","TMUS","ORCL",
    "NOW","WDAY","HUBS","ADP","PAYX","ROP","CDNS","SNPS","ANET","MRVL",
    "KLAC","LRCX","AMAT","APP","TTD","AXON","DELL","HPE","NTAP",
    "CMG","YUM","DPZ","WING","CAVA","DRI","TGT","TJX","ROST","BURL",
    "ORLY","AZO","KMX","AN","APD","ECL","LYB","PPG","DKNG","MGM","LVS",
    "CELH","MNST","KDP","AMG","TROW","STT","BK","NTRS","RJF","CBOE",
]

def get_tickers():
    print("📋 获取罗素1000成分股...")
    try:
        url = ("https://www.ishares.com/us/products/239707/ishares-russell-1000-etf"
               "/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        tickers = []
        for line in r.text.split("\n")[9:]:
            col = line.split(",")[0].strip().strip('"')
            if col and 1 <= len(col) <= 5 and re.match(r'^[A-Z\-]+$', col):
                tickers.append(col)
        if len(tickers) > 500:
            print(f"  ✅ iShares: {len(tickers)} 只")
            return list(dict.fromkeys(tickers))
    except Exception as e:
        print(f"  ⚠️ iShares 失败: {e}")
    try:
        df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        sp = [s.replace(".", "-") for s in df["Symbol"].tolist()]
        print(f"  ✅ Wikipedia S&P500: {len(sp)} 只")
        return sp
    except Exception as e:
        print(f"  ⚠️ Wikipedia 失败: {e}")
    print(f"  ⚠️ 兜底列表: {len(FALLBACK)} 只")
    return list(dict.fromkeys(FALLBACK))

# ── 批量拉行情（核心修复版）─────────────────────────────────────
def fetch_perf(tickers, batch=100):
    print(f"\n📊 下载行情 ({len(tickers)} 只)...")
    out = []
    groups = [tickers[i:i+batch] for i in range(0, len(tickers), batch)]

    for gi, chunk in enumerate(groups):
        try:
            raw = yf.download(chunk, period="5d", interval="1d",
                              auto_adjust=True, progress=False, threads=True)
            if raw.empty:
                raise ValueError("empty dataframe")

            if len(chunk) == 1:
                # 单只股票：columns 是 ['Close','High','Low','Open','Volume']
                t = chunk[0]
                try:
                    cl = raw["Close"].dropna()
                    vo = raw["Volume"].dropna()
                    if len(cl) >= 2:
                        p0, p1 = float(cl.iloc[-2]), float(cl.iloc[-1])
                        if p0 > 0 and p1 > 0:
                            out.append({"ticker": t, "price": round(p1, 2), "prev": round(p0, 2),
                                        "chg": round((p1-p0)/p0*100, 2),
                                        "vol": int(vo.iloc[-1]) if len(vo) else 0})
                except Exception as e:
                    pass
            else:
                # 多只股票：MultiIndex columns (metric, ticker)
                close_df = raw["Close"] if "Close" in raw.columns.get_level_values(0) else None
                vol_df   = raw["Volume"] if "Volume" in raw.columns.get_level_values(0) else None
                if close_df is None:
                    raise ValueError("no Close column")
                for t in chunk:
                    try:
                        if t not in close_df.columns:
                            continue
                        cl = close_df[t].dropna()
                        vo = vol_df[t].dropna() if vol_df is not None and t in vol_df.columns else pd.Series()
                        if len(cl) >= 2:
                            p0, p1 = float(cl.iloc[-2]), float(cl.iloc[-1])
                            if p0 > 0 and p1 > 0:
                                out.append({"ticker": t, "price": round(p1, 2), "prev": round(p0, 2),
                                            "chg": round((p1-p0)/p0*100, 2),
                                            "vol": int(vo.iloc[-1]) if len(vo) else 0})
                    except:
                        pass
        except Exception as e:
            print(f"  ⚠️ batch {gi+1} 批量下载失败: {e}")

        print(f"  batch {gi+1}/{len(groups)} 完成 → 累计 {len(out)} 只")
        time.sleep(0.3)

    # ── Fallback：逐只下载（当批量全部失败时）────────────────
    if len(out) < 20:
        print(f"  ⚠️ 批量下载仅得到 {len(out)} 只，改用逐只下载...")
        out2 = []
        for i, t in enumerate(tickers):
            try:
                hist = yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=True)
                if len(hist) >= 2:
                    p0 = float(hist["Close"].iloc[-2])
                    p1 = float(hist["Close"].iloc[-1])
                    if p0 > 0 and p1 > 0:
                        vol = int(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else 0
                        out2.append({"ticker": t, "price": round(p1, 2), "prev": round(p0, 2),
                                     "chg": round((p1-p0)/p0*100, 2), "vol": vol})
            except:
                pass
            if i % 100 == 99:
                print(f"  逐只进度: {i+1}/{len(tickers)}")
                time.sleep(1)
        if len(out2) > len(out):
            out = out2
    return out

# ── 公司基本信息 ─────────────────────────────────────────────
_info_cache = {}
def get_info(ticker):
    if ticker in _info_cache:
        return _info_cache[ticker]
    try:
        d = yf.Ticker(ticker).info
        r = {"name": d.get("longName") or d.get("shortName") or ticker,
             "sector": d.get("sector","Other"),
             "industry": d.get("industry","Other"),
             "mktcap": d.get("marketCap",0) or 0}
    except:
        r = {"name":ticker,"sector":"Other","industry":"Other","mktcap":0}
    _info_cache[ticker] = r
    return r

def fmt_cap(n):
    if not n: return "N/A"
    if n>=1e12: return f"${n/1e12:.2f}T"
    if n>=1e9:  return f"${n/1e9:.1f}B"
    return f"${n/1e6:.0f}M"

def fmt_vol(v):
    if not v: return "N/A"
    if v>=1e9: return f"{v/1e9:.1f}B"
    if v>=1e6: return f"{v/1e6:.1f}M"
    if v>=1e3: return f"{v/1e3:.0f}K"
    return str(v)

# ── GPT AI 分析 ─────────────────────────────────────────────
def gpt_analysis(ticker, name, chg, sector):
    """用 GPT-4o-mini 生成中文股票异动原因分析"""
    if not OPENAI_API_KEY:
        return None
    direction = "大涨" if chg > 0 else "大跌"
    prompt = (f"{name}（{ticker}）今日{direction} {abs(chg):.1f}%，所属行业：{sector}。"
              f"请用中文简要分析可能的原因（2-3句话，不要编造具体新闻，基于行业逻辑和常见驱动因素分析）。")
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "max_tokens": 200, "temperature": 0.7,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except:
        return None

# ── 抓新闻 ───────────────────────────────────────────────────
def fetch_news(ticker, name):
    items = []
    if NEWS_API_KEY:
        try:
            p = {"q": f'"{ticker}" stock',
                 "from": (datetime.utcnow()-timedelta(days=2)).strftime("%Y-%m-%d"),
                 "sortBy":"relevancy","pageSize":5,"language":"en","apiKey":NEWS_API_KEY}
            r = requests.get("https://newsapi.org/v2/everything", params=p, timeout=10)
            for a in r.json().get("articles",[])[:5]:
                t = (a.get("title") or "").strip()
                s = (a.get("source",{}).get("name") or "")
                d = (a.get("publishedAt") or "")[:10]
                if t and "[Removed]" not in t:
                    items.append({"text":t,"src":f"{s} · {d}"})
        except: pass
    if not items:
        try:
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
            r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:5]:
                t = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "")[:16]
                if t:
                    items.append({"text":t,"src":f"Yahoo Finance · {pub}"})
        except: pass
    if not items:
        items = [{"text":"暂无最新新闻，可能为技术性波动或市场整体情绪驱动","src":"—"}]
    return items[:5]

# ── 数据序列化为 JS ──────────────────────────────────────────
def to_js(stocks):
    def e(s):
        return str(s).replace("\\","\\\\").replace("`","\\`").replace("${","\\${")
    rows = []
    for s in stocks:
        news = "[" + ",".join(
            "{text:`"+e(n["text"])+"`,src:`"+e(n["src"])+"`}"
            for n in s["news"]) + "]"
        ai = ("`" + e(s.get("ai","")) + "`") if s.get("ai") else "null"
        rows.append(
            "{rank:"+str(s["rank"])
            +",ticker:`"+e(s["ticker"])+"`"
            +",name:`"+e(s["info"]["name"])+"`"
            +",sector:`"+e(s["info"]["sector"])+"`"
            +",industry:`"+e(s["info"]["industry"])+"`"
            +",price:"+str(s["price"])
            +",prev:"+str(s["prev"])
            +",chg:"+str(s["chg"])
            +",vol:`"+e(fmt_vol(s["vol"]))+"`"
            +",mktcap:`"+e(fmt_cap(s["info"]["mktcap"]))+"`"
            +",ai:"+ai
            +",news:"+news+"}")
    return "[\n"+",\n".join(rows)+"\n]"

# ── 生成 HTML（与之前相同，此处省略完整模板）────────────────
def build_html(gainers, losers, report_date, generated_at, total):
    gjs = to_js(gainers)
    ljs = to_js(losers)
    share = f'<a href="{PAGES_URL}" style="color:var(--blue)">{PAGES_URL}</a>' if PAGES_URL else "GitHub Pages"
    return """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Russell 1000 · 美股异动日报 · """ + report_date + """</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500;9..40,600&display=swap" rel="stylesheet">
<style>
:root{--bg:#080b0f;--surface:#0e1318;--surface2:#141a22;--border:#1e2830;--border2:#263040;
--text:#d4dde8;--muted:#5a6a7a;--dim:#3a4a5a;--green:#00e5a0;--green2:#00b87a;
--red:#ff3d5a;--amber:#ffb830;--blue:#3d9eff;
--mono:'Space Mono',monospace;--sans:'DM Sans',sans-serif;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;line-height:1.5;}
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;height:48px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;}
.logo{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--green);letter-spacing:2px;}
.tag{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;border:1px solid var(--border2);padding:2px 8px;border-radius:2px;}
.dot{width:6px;height:6px;border-radius:50%;background:var(--amber);box-shadow:0 0 8px var(--amber);display:inline-block;margin-right:6px;animation:blink 2s infinite;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.ts{font-family:var(--mono);font-size:10px;color:var(--muted);}
.hero{padding:28px 24px 0;display:grid;grid-template-columns:1fr auto;gap:24px;align-items:end;max-width:1400px;margin:0 auto;}
.eyebrow{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:3px;text-transform:uppercase;margin-bottom:6px;}
.hero-date{font-size:26px;font-weight:300;letter-spacing:-.5px;}
.hero-date strong{font-weight:600;color:#fff;}
.stats{display:flex;gap:2px;}
.stat{background:var(--surface);border:1px solid var(--border);padding:12px 18px;text-align:center;min-width:88px;}
.stat:first-child{border-radius:6px 0 0 6px;}.stat:last-child{border-radius:0 6px 6px 0;}
.stat-val{font-family:var(--mono);font-size:20px;font-weight:700;display:block;}
.stat-lbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-top:2px;display:block;}
.tabs{max-width:1400px;margin:22px auto 0;padding:0 24px;display:flex;border-bottom:1px solid var(--border);}
.tab{padding:10px 20px;font-family:var(--mono);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;user-select:none;position:relative;bottom:-1px;}
.tab:hover{color:var(--text);}
.tab.ga.on{color:var(--green);border-bottom-color:var(--green);}
.tab.lo.on{color:var(--red);border-bottom-color:var(--red);}
.tab.ov.on{color:var(--blue);border-bottom-color:var(--blue);}
.badge{display:inline-block;margin-left:6px;font-size:9px;padding:1px 5px;border-radius:2px;vertical-align:middle;}
.ga .badge{background:rgba(0,229,160,.15);color:var(--green);}
.lo .badge{background:rgba(255,61,90,.15);color:var(--red);}
.ov .badge{background:rgba(61,158,255,.15);color:var(--blue);}
.main{max-width:1400px;margin:0 auto;padding:20px 24px 60px;}
.panel{display:none;}.panel.on{display:block;}
.fbar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap;}
.sw{position:relative;}
.sw::before{content:'⌕';position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:14px;pointer-events:none;}
.fi{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:6px 12px 6px 28px;font-family:var(--sans);font-size:12px;border-radius:4px;outline:none;width:175px;}
.chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:4px 10px;font-size:10px;border-radius:3px;cursor:pointer;transition:all .2s;user-select:none;font-family:var(--mono);}
.chip:hover,.chip.on{border-color:var(--border2);color:var(--text);background:var(--surface2);}
.tw{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:12px;}
thead th{background:var(--surface);border-bottom:1px solid var(--border);padding:7px 10px;text-align:left;font-family:var(--mono);font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);white-space:nowrap;position:sticky;top:48px;cursor:pointer;user-select:none;}
thead th:hover{color:var(--text);}
thead th.sa::after{content:' ↑';color:var(--amber);}
thead th.sd::after{content:' ↓';color:var(--amber);}
tbody tr{border-bottom:1px solid rgba(30,40,48,.7);transition:background .1s;cursor:pointer;}
tbody tr:hover{background:var(--surface2);}
tbody tr.xr{background:var(--surface2);}
tbody td{padding:9px 10px;vertical-align:middle;white-space:nowrap;}
.rk{font-family:var(--mono);font-size:10px;color:var(--dim);width:30px;}
.tk{font-family:var(--mono);font-size:13px;font-weight:700;color:#fff;display:flex;align-items:center;gap:5px;}
.arr{font-size:9px;color:var(--dim);transition:transform .2s;display:inline-block;}
.nm{color:var(--text);max-width:175px;overflow:hidden;text-overflow:ellipsis;}
.sp-pill{display:inline-block;padding:2px 7px;border-radius:2px;font-size:10px;font-family:var(--mono);}
.pc{font-family:var(--mono);font-size:12px;}
.pv{font-family:var(--mono);font-size:11px;color:var(--muted);}
.cc{font-family:var(--mono);font-size:13px;font-weight:700;}
.cc.p{color:var(--green);}.cc.n{color:var(--red);}
.bb{height:3px;background:var(--border);border-radius:2px;width:56px;}
.bf{height:100%;border-radius:2px;}
.vc,.mc{font-family:var(--mono);font-size:11px;color:var(--muted);}
.nr td{padding:0!important;border-bottom:1px solid var(--border2)!important;}
.np{padding:14px 58px 18px;background:var(--surface);border-left:2px solid var(--border2);}
.nh{font-family:var(--mono);font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:8px;}
.nh::after{content:'';flex:1;height:1px;background:var(--border);}
.ai-box{background:rgba(61,158,255,.07);border:1px solid rgba(61,158,255,.2);border-radius:4px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:var(--text);line-height:1.7;}
.ai-label{font-family:var(--mono);font-size:9px;color:var(--blue);letter-spacing:1px;margin-bottom:4px;}
.ni{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid rgba(30,40,48,.4);align-items:flex-start;}
.ni:last-child{border-bottom:none;}
.nd{width:4px;height:4px;border-radius:50%;margin-top:7px;flex-shrink:0;}
.nt{font-size:12px;color:var(--text);line-height:1.65;}
.ns{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:2px;}
.Technology{background:rgba(61,158,255,.12);color:#3d9eff;}
.Communications,.Communication-Services{background:rgba(200,100,200,.12);color:#c864c8;}
.Financial-Services,.Finance{background:rgba(255,184,48,.12);color:#ffb830;}
.Healthcare,.Health-Care{background:rgba(0,229,160,.10);color:#00c896;}
.Energy{background:rgba(255,140,0,.10);color:#ff8c00;}
.Consumer-Discretionary{background:rgba(180,120,255,.10);color:#b478ff;}
.Consumer-Staples{background:rgba(220,160,80,.10);color:#dca050;}
.Industrials{background:rgba(90,160,160,.10);color:#5aa0a0;}
.Real-Estate{background:rgba(255,100,80,.10);color:#ff6450;}
.Materials{background:rgba(100,200,100,.10);color:#64c864;}
.Utilities{background:rgba(200,180,100,.10);color:#c8b464;}
.Other{background:rgba(100,100,100,.10);color:#888;}
.og{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:12px;}
.oc{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:14px;}
.oh{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
.ow{font-family:var(--mono);font-size:10px;color:var(--dim);}
.om{display:flex;align-items:center;padding:4px 0;border-bottom:1px solid rgba(30,40,48,.5);}
.om:last-child{border-bottom:none;}
.ot{font-family:var(--mono);font-size:11px;font-weight:700;color:#fff;width:50px;flex-shrink:0;}
.on2{font-size:11px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 6px;}
.oc2{font-family:var(--mono);font-size:12px;font-weight:700;width:58px;text-align:right;flex-shrink:0;}
.footer{background:var(--surface);border-top:1px solid var(--border);padding:14px 24px;text-align:center;font-family:var(--mono);font-size:10px;color:var(--dim);line-height:1.9;}
::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-track{background:var(--bg);}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px;}
@media(max-width:768px){.hero{grid-template-columns:1fr;}.stats{flex-wrap:wrap;}.nm,.pv,.bb,.mc{display:none;}.np{padding:12px 14px;}}
</style>
</head>
<body>
<div class="topbar">
  <div style="display:flex;align-items:center;gap:12px">
    <span class="logo">MKTSCAN</span>
    <span class="tag">RUSSELL 1000</span>
    <span class="tag" style="color:var(--amber)">DAILY REPORT</span>
  </div>
  <div style="display:flex;align-items:center;gap:14px">
    <span><span class="dot"></span><span class="ts">盘后数据 · """ + report_date + """</span></span>
    <span class="ts" id="lt"></span>
  </div>
</div>
<div class="hero">
  <div>
    <div class="eyebrow">Russell 1000 · Daily Market Intelligence</div>
    <div class="hero-date"><strong>""" + report_date + """</strong> 美股异动日报</div>
    <div style="margin-top:8px;font-size:12px;color:var(--muted)">
      扫描 <span style="color:var(--text)">""" + str(total) + """</span> 只成分股 · 生成于 <span style="color:var(--text)">""" + generated_at + """ UTC</span>（每日盘后自动更新）
    </div>
  </div>
  <div class="stats">
    <div class="stat"><span class="stat-val" style="color:var(--green)">↑ """ + str(TOP_N) + """</span><span class="stat-lbl">涨幅榜</span></div>
    <div class="stat"><span class="stat-val" style="color:var(--red)">↓ """ + str(TOP_N) + """</span><span class="stat-lbl">跌幅榜</span></div>
    <div class="stat"><span class="stat-val" style="color:var(--muted)">""" + str(total) + """</span><span class="stat-lbl">已扫描</span></div>
  </div>
</div>
<div class="tabs">
  <div class="tab ga on" onclick="sw('gainers',this)">涨幅榜 <span class="badge">TOP """ + str(TOP_N) + """</span></div>
  <div class="tab lo"    onclick="sw('losers',this)" >跌幅榜 <span class="badge">TOP """ + str(TOP_N) + """</span></div>
  <div class="tab ov"    onclick="sw('overview',this)">行业总览</div>
</div>
<div class="main">
  <div class="panel on" id="p-gainers">
    <div class="fbar" id="fb-gainers"></div>
    <div class="tw"><table id="t-gainers">
      <thead><tr>
        <th>#</th><th onclick="srt('gainers','ticker')">代码</th><th>公司名称</th><th>行业</th>
        <th onclick="srt('gainers','price')">现价</th><th onclick="srt('gainers','prev')">昨收</th>
        <th id="th-gainers-chg" class="sd" onclick="srt('gainers','chg')">涨跌幅</th>
        <th>幅度</th><th>成交量</th><th>市值</th><th></th>
      </tr></thead>
      <tbody id="b-gainers"></tbody>
    </table></div>
  </div>
  <div class="panel" id="p-losers">
    <div class="fbar" id="fb-losers"></div>
    <div class="tw"><table id="t-losers">
      <thead><tr>
        <th>#</th><th onclick="srt('losers','ticker')">代码</th><th>公司名称</th><th>行业</th>
        <th onclick="srt('losers','price')">现价</th><th onclick="srt('losers','prev')">昨收</th>
        <th id="th-losers-chg" class="sa" onclick="srt('losers','chg')">涨跌幅</th>
        <th>幅度</th><th>成交量</th><th>市值</th><th></th>
      </tr></thead>
      <tbody id="b-losers"></tbody>
    </table></div>
  </div>
  <div class="panel" id="p-overview">
    <div class="og" id="ov-grid"></div>
  </div>
</div>
<div class="footer">
  数据来源：Yahoo Finance · NewsAPI · Russell 1000 Index &nbsp;|&nbsp;
  每个交易日盘后（美东 17:30）自动更新 &nbsp;|&nbsp;
  仅供参考，不构成投资建议 &nbsp;|&nbsp;
  分享网址：""" + share + """
</div>
<script>
const GAINERS = """ + gjs + """;
const LOSERS  = """ + ljs + """;
function sc(s){const m={'Technology':'Technology','Communications':'Communications','Communication Services':'Communication-Services','Financial Services':'Financial-Services','Finance':'Finance','Healthcare':'Healthcare','Health Care':'Health-Care','Energy':'Energy','Consumer Discretionary':'Consumer-Discretionary','Consumer Staples':'Consumer-Staples','Industrials':'Industrials','Real Estate':'Real-Estate','Materials':'Materials','Utilities':'Utilities'};return m[s]||'Other';}
const sortSt={gainers:{col:'chg',asc:false},losers:{col:'chg',asc:true}};
const sectorFilters={gainers:new Set(),losers:new Set()};
let searchQ={gainers:'',losers:''};
function render(key){
  const data=key==='gainers'?[...GAINERS]:[...LOSERS];
  const{col,asc}=sortSt[key];
  data.sort((a,b)=>{let va=a[col],vb=b[col];if(typeof va==='string')va=va.toLowerCase(),vb=vb.toLowerCase();return asc?(va>vb?1:-1):(va<vb?1:-1);});
  const maxAbs=Math.max(...data.map(d=>Math.abs(d.chg)));
  const q=searchQ[key].toLowerCase();const sf=sectorFilters[key];
  const body=document.getElementById('b-'+key);body.innerHTML='';
  data.forEach((s,i)=>{
    if(q&&!s.ticker.toLowerCase().includes(q)&&!s.name.toLowerCase().includes(q))return;
    if(sf.size&&!sf.has(s.sector))return;
    const isGain=s.chg>=0;const color=isGain?'var(--green)':'var(--red)';
    const sign=isGain?'+':'';const barW=maxAbs>0?Math.round(Math.abs(s.chg)/maxAbs*100):0;
    const cls=sc(s.sector);const nid=`n-${key}-${i}`;const rid=`r-${key}-${i}`;
    const tr=document.createElement('tr');tr.id=rid;
    tr.innerHTML=`<td class="rk">${s.rank}</td><td><div class="tk">${s.ticker} <span class="arr" id="a-${nid}">▶</span></div></td><td class="nm">${s.name}</td><td><span class="sp-pill ${cls}">${s.sector}</span></td><td class="pc">$${s.price.toFixed(2)}</td><td class="pv">$${s.prev.toFixed(2)}</td><td class="cc ${isGain?'p':'n'}">${sign}${s.chg.toFixed(2)}%</td><td><div class="bb"><div class="bf" style="width:0;background:${color}" data-w="${barW}%"></div></div></td><td class="vc">${s.vol}</td><td class="mc">${s.mktcap}</td><td></td>`;
    tr.onclick=()=>toggleNews(rid,nid,s,color);body.appendChild(tr);
    const aiHtml=s.ai?`<div class="ai-box"><div class="ai-label">🤖 AI 异动分析</div>${s.ai}</div>`:'';
    const nr=document.createElement('tr');nr.className='nr';nr.id=nid;nr.style.display='none';
    nr.innerHTML=`<td colspan="11"><div class="np"><div class="nh">📰 相关新闻 &amp; 异动原因</div>${aiHtml}${s.news.map(n=>`<div class="ni"><div class="nd" style="background:${color}"></div><div><div class="nt">${n.text}</div><div class="ns">${n.src}</div></div></div>`).join('')}</div></td>`;
    body.appendChild(nr);
  });
  setTimeout(()=>{body.querySelectorAll('.bf').forEach(b=>{b.style.width=b.dataset.w;});},50);
}
function toggleNews(rid,nid,s,color){
  const row=document.getElementById(rid);const news=document.getElementById(nid);const arr=document.getElementById('a-'+nid);
  const open=news.style.display!=='none';news.style.display=open?'none':'table-row';
  row.classList.toggle('xr',!open);if(arr)arr.style.transform=open?'':'rotate(90deg)';
}
function srt(key,col){
  const st=sortSt[key];if(st.col===col)st.asc=!st.asc;else{st.col=col;st.asc=(col==='chg'&&key==='losers');}
  document.querySelectorAll(`#t-${key} thead th`).forEach(th=>th.classList.remove('sa','sd'));
  const th=document.getElementById(`th-${key}-${col}`);if(th)th.classList.add(st.asc?'sa':'sd');
  render(key);
}
function doSearch(key,val){searchQ[key]=val;render(key);}
function buildFilters(key){
  const data=key==='gainers'?GAINERS:LOSERS;
  const sectors=[...new Set(data.map(d=>d.sector))].sort();
  const fb=document.getElementById('fb-'+key);
  fb.innerHTML=`<div class="sw"><input class="fi" placeholder="搜索代码/公司…" oninput="doSearch('${key}',this.value)"></div>`;
  sectors.forEach(s=>{const chip=document.createElement('span');chip.className='chip';chip.textContent=s;chip.onclick=()=>{const f=sectorFilters[key];if(f.has(s)){f.delete(s);chip.classList.remove('on');}else{f.add(s);chip.classList.add('on');}render(key);};fb.appendChild(chip);});
}
function buildOverview(){
  const all=[...GAINERS,...LOSERS];const bySector={};
  all.forEach(s=>{(bySector[s.sector]=bySector[s.sector]||[]).push(s);});
  const grid=document.getElementById('ov-grid');
  grid.innerHTML=Object.entries(bySector).sort(([a],[b])=>a.localeCompare(b)).map(([sector,stocks])=>{
    const cls=sc(sector);const top=[...stocks].sort((a,b)=>Math.abs(b.chg)-Math.abs(a.chg)).slice(0,6);
    return`<div class="oc"><div class="oh"><span class="sp-pill ${cls}">${sector}</span><span class="ow">${stocks.length} 只</span></div>${top.map(s=>{const c=s.chg>=0?'var(--green)':'var(--red)';const sign=s.chg>=0?'+':'';return`<div class="om"><span class="ot">${s.ticker}</span><span class="on2">${s.name}</span><span class="oc2" style="color:${c}">${sign}${s.chg.toFixed(2)}%</span></div>`;}).join('')}</div>`;
  }).join('');
}
function sw(key,el){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));el.classList.add('on');document.getElementById('p-'+key).classList.add('on');}
function updateTime(){const now=new Date();const et=now.toLocaleString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});document.getElementById('lt').textContent=et+' ET';}
buildFilters('gainers');buildFilters('losers');render('gainers');render('losers');buildOverview();updateTime();setInterval(updateTime,1000);
</script>
</body>
</html>"""

# ── 发送邮件 ──────────────────────────────────────────────────
def send_email(subject, body_html):
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        print("⚠️ 邮件配置缺失，跳过发送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(EMAIL_FROM, EMAIL_PASSWORD)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"✅ 邮件已发送 → {EMAIL_TO}")

def email_body(gainers, losers, report_date, pages_url):
    top3g = "".join(
        f'<tr><td style="padding:6px 12px;font-family:monospace;color:#00e5a0;font-weight:700">{s["ticker"]}</td>'
        f'<td style="padding:6px 12px;color:#d4dde8">{s["info"]["name"]}</td>'
        f'<td style="padding:6px 12px;font-family:monospace;color:#00e5a0;font-weight:700">+{s["chg"]:.2f}%</td></tr>'
        for s in gainers[:5])
    top3l = "".join(
        f'<tr><td style="padding:6px 12px;font-family:monospace;color:#ff3d5a;font-weight:700">{s["ticker"]}</td>'
        f'<td style="padding:6px 12px;color:#d4dde8">{s["info"]["name"]}</td>'
        f'<td style="padding:6px 12px;font-family:monospace;color:#ff3d5a;font-weight:700">{s["chg"]:.2f}%</td></tr>'
        for s in losers[:5])
    link = f'<a href="{pages_url}" style="color:#3d9eff">{pages_url}</a>' if pages_url else "（请查看网页）"
    return f"""<!DOCTYPE html><html><body style="background:#080b0f;color:#d4dde8;font-family:sans-serif;padding:24px">
<div style="max-width:600px;margin:0 auto">
<h1 style="font-size:20px;color:#fff">美股异动日报 {report_date}</h1>
<table style="width:100%;margin:16px 0"><tr>
<td style="width:50%;vertical-align:top;padding-right:8px">
<div style="font-size:10px;color:#5a6a7a;margin-bottom:8px">▲ 涨幅 TOP 5</div>
<table style="width:100%;border-collapse:collapse">{top3g}</table>
</td>
<td style="width:50%;vertical-align:top;padding-left:8px">
<div style="font-size:10px;color:#5a6a7a;margin-bottom:8px">▼ 跌幅 TOP 5</div>
<table style="width:100%;border-collapse:collapse">{top3l}</table>
</td></tr></table>
<div style="text-align:center;padding:12px">{link}</div>
<div style="font-size:10px;color:#3a4a5a;text-align:center">仅供参考，不构成投资建议</div>
</div></body></html>"""

# ── 主流程 ────────────────────────────────────────────────────
def main():
    print("="*60)
    print(f"🚀 罗素1000 监控启动 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)

    tickers = get_tickers()
    print(f"  成分股: {len(tickers)} 只")

    perf = fetch_perf(tickers)
    if not perf:
        print("❌ 未能获取任何行情数据，退出。")
        return

    perf = [x for x in perf if abs(x["chg"]) <= 100]
    print(f"\n✅ 有效数据: {len(perf)} 只")

    if len(perf) < 10:
        print(f"⚠️ 有效数据仅 {len(perf)} 只，数据不足，退出")
        return

    perf.sort(key=lambda x: x["chg"], reverse=True)
    n = min(TOP_N, len(perf) // 2)
    gainers_raw = perf[:n]
    losers_raw  = list(reversed(perf[-n:]))

    print(f"\n  涨幅榜首: {gainers_raw[0]['ticker']} +{gainers_raw[0]['chg']}%")
    print(f"  跌幅榜首: {losers_raw[0]['ticker']}  {losers_raw[0]['chg']}%")

    def enrich(stocks):
        out = []
        for i, s in enumerate(stocks):
            info = get_info(s["ticker"])
            print(f"  [{i+1}/{len(stocks)}] {s['ticker']} ({s['chg']:+.2f}%) 处理中...")
            news = fetch_news(s["ticker"], info["name"])
            ai   = gpt_analysis(s["ticker"], info["name"], s["chg"], info["sector"])
            out.append({**s, "rank": i+1, "info": info, "news": news, "ai": ai})
            time.sleep(0.3)
        return out

    print("\n📈 处理涨幅榜...")
    gainers = enrich(gainers_raw)
    print("\n📉 处理跌幅榜...")
    losers  = enrich(losers_raw)

    now = datetime.utcnow()
    report_date  = now.strftime("%Y年%m月%d日")
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    html = build_html(gainers, losers, report_date, generated_at, len(perf))

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\n✅ docs/index.html 已写入")

    if EMAIL_TO:
        top_g = gainers[0]; top_l = losers[0]
        subject = (f"📈 罗素1000日报 {now.strftime('%m/%d')} | "
                   f"涨冠 {top_g['ticker']} +{top_g['chg']:.1f}% · "
                   f"跌冠 {top_l['ticker']} {top_l['chg']:.1f}%")
        send_email(subject, email_body(gainers, losers, report_date, PAGES_URL))

    print("🎉 全部完成！")

if __name__ == "__main__":
    main()
