#!/usr/bin/env python3
"""
youtube_to_notion.py
--------------------
Pulls per-video YouTube Analytics and upserts the outcome metrics into the
"Video Performance" Notion database. Designed to be invoked by a weekly
Claude Code Routine ("run this script and report the summary").

This is deterministic ETL: no model reasoning in the loop. The Routine is
just the scheduler + runner.

Requires: pip install requests

Env vars (set these as Routine secrets):
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN     # one-time OAuth, see setup notes
  NOTION_TOKEN             # internal integration token
  NOTION_DATABASE_ID       # defaults to the DB created for you (below)
"""

import os
import re
import sys
import datetime as dt
import requests

# ---------------------------------------------------------------- config
NOTION_DB = os.environ.get("NOTION_DATABASE_ID", "b355bee4c364440bb99c7e45b2cfb04d")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2022-06-28"

G_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
G_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
G_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

# Methodology constants (see the pipeline spec)
PACKAGING_SOURCES = {"SUBSCRIBER", "RELATED_VIDEO"}   # Browse/home/subs + Suggested
SEARCH_SOURCE = "YT_SEARCH"
REACH_METRIC_START = "2026-01-15"   # thumbnail-impression metrics began this date
MATURITY_DAYS = 90
POWER_IMPRESSIONS = 2000
DILUTION_THRESHOLD_PCT = 30.0

# The thumbnail CTR metric is returned as a ratio (0..1). If a first run shows
# CTRs ~100x too small/large, flip this and re-run.
CTR_IS_RATIO = True
TODAY = dt.date.today().isoformat()

# ---------------------------------------------------------------- google auth
def google_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": G_CLIENT_ID,
        "client_secret": G_CLIENT_SECRET,
        "refresh_token": G_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def gget(url, token, **params):
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=h, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------- video list
def list_videos(token):
    ch = gget("https://www.googleapis.com/youtube/v3/channels", token,
              part="contentDetails", mine="true")
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    vids, page = [], None
    while True:
        resp = gget("https://www.googleapis.com/youtube/v3/playlistItems", token,
                    part="contentDetails", playlistId=uploads,
                    maxResults=50, pageToken=page or "")
        vids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    # hydrate title + duration + publish date
    out = []
    for i in range(0, len(vids), 50):
        chunk = vids[i:i + 50]
        resp = gget("https://www.googleapis.com/youtube/v3/videos", token,
                    part="snippet,contentDetails", id=",".join(chunk))
        for it in resp.get("items", []):
            out.append({
                "id": it["id"],
                "title": it["snippet"]["title"],
                "published": it["snippet"]["publishedAt"][:10],
                "duration_s": iso_duration_seconds(it["contentDetails"]["duration"]),
            })
    return out

def iso_duration_seconds(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

# ---------------------------------------------------------------- analytics
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"

def query(token, start, metrics, video_id, dimensions=None, extra_filter=""):
    filt = f"video=={video_id}" + (f";{extra_filter}" if extra_filter else "")
    params = {
        "ids": "channel==MINE", "startDate": start, "endDate": TODAY,
        "metrics": metrics, "filters": filt,
    }
    if dimensions:
        params["dimensions"] = dimensions
    r = requests.get(ANALYTICS, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    if r.status_code != 200:
        return None  # caller handles fallback / nulls
    return r.json()

def rows(resp):
    return resp.get("rows", []) if resp else []

def to_pct(ratio):
    if ratio is None:
        return None
    return ratio * 100 if CTR_IS_RATIO else ratio

# ---------------------------------------------------------------- per-video pull
def pull_video(token, v):
    vid = v["id"]
    start = max(v["published"], REACH_METRIC_START)
    out = {"reach_confidence": "Low" if v["published"] < REACH_METRIC_START else "High"}

    # A - packaging reach by traffic source (with graceful fallback)
    a = query(token, start,
              "videoThumbnailImpressions,videoThumbnailImpressionsClickRate",
              vid, dimensions="insightTrafficSourceType")
    feed_impr = pkg_clicks = search_impr = search_clicks = 0.0
    if a and rows(a):                       # [source, impressions, clickRate]
        for src, impr, rate in rows(a):
            impr = impr or 0
            clicks = impr * (rate or 0)
            if src in PACKAGING_SOURCES:
                feed_impr += impr; pkg_clicks += clicks
            elif src == SEARCH_SOURCE:
                search_impr += impr; search_clicks += clicks
    else:                                   # fallback: blended reach, no split
        b = query(token, start,
                  "videoThumbnailImpressions,videoThumbnailImpressionsClickRate", vid)
        if b and rows(b):
            impr, rate = rows(b)[0]
            feed_impr = impr or 0
            pkg_clicks = (impr or 0) * (rate or 0)
        out["reach_confidence"] = "Low"     # blended -> downgrade confidence

    out["feed_impressions"] = feed_impr
    out["packaging_ctr"] = to_pct(pkg_clicks / feed_impr) if feed_impr else None
    out["search_ctr"] = to_pct(search_clicks / search_impr) if search_impr else None

    # B - engagement
    b = query(token, start,
              "views,averageViewDuration,averageViewPercentage", vid)
    if b and rows(b):
        views, avd_s, avd_pct = rows(b)[0]
        out.update(views=views, avd_s=avd_s, avd_pct=avd_pct)
    else:
        out.update(views=None, avd_s=None, avd_pct=None)

    # C - view shares for contamination flag
    c = query(token, start, "views", vid, dimensions="insightTrafficSourceType")
    total = ext = note = 0.0
    for src, views in rows(c):
        total += views or 0
        if src == "EXT_URL":
            ext += views or 0
        elif src == "NOTIFICATION":
            note += views or 0
    out["external_share"] = (ext / total * 100) if total else None
    diluted = total and ((ext + note) / total * 100) > DILUTION_THRESHOLD_PCT

    # D - retention curve -> 30s point
    out["retention_30s"] = out["intro_drop"] = None
    if v["duration_s"]:
        d = query(token, start, "audienceWatchRatio", vid,
                  dimensions="elapsedVideoTimeRatio", extra_filter="audienceType==ORGANIC")
        if d and rows(d):
            target = 30.0 / v["duration_s"]
            best = min(rows(d), key=lambda r: abs(r[0] - target))
            out["retention_30s"] = to_pct(best[1])
            out["intro_drop"] = 100 - out["retention_30s"]

    # gating flags
    age_days = (dt.date.today() - dt.date.fromisoformat(v["published"])).days
    matured = age_days >= MATURITY_DAYS
    power_ok = feed_impr >= POWER_IMPRESSIONS
    out.update(
        matured=matured, power_ok=power_ok, signal_diluted=bool(diluted),
        analysis_eligible=bool(matured and power_ok and not diluted),
    )
    return out

# ---------------------------------------------------------------- notion upsert
NHEAD = {"Authorization": f"Bearer {NOTION_TOKEN}",
         "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

def num(x):   return {"number": float(x)} if x is not None else {"number": None}
def chk(x):   return {"checkbox": bool(x)}
def sel(x):   return {"select": {"name": x}} if x else {"select": None}

def find_page(vid):
    r = requests.post(f"https://api.notion.com/v1/databases/{NOTION_DB}/query",
                      headers=NHEAD, json={"filter": {"property": "Video ID",
                      "rich_text": {"equals": vid}}}, timeout=30)
    r.raise_for_status()
    res = r.json().get("results", [])
    return res[0]["id"] if res else None

def outcome_props(m):
    return {
        "Packaging CTR %": num(m["packaging_ctr"]),
        "Feed Impressions": num(m["feed_impressions"]),
        "Search CTR %": num(m["search_ctr"]),
        "AVD (s)": num(m["avd_s"]),
        "AVD %": num(m["avd_pct"]),
        "Views": num(m["views"]),
        "Retention @30s %": num(m["retention_30s"]),
        "Intro Drop %": num(m["intro_drop"]),
        "External Share %": num(m["external_share"]),
        "Signal Diluted": chk(m["signal_diluted"]),
        "Matured": chk(m["matured"]),
        "Power OK": chk(m["power_ok"]),
        "Analysis Eligible": chk(m["analysis_eligible"]),
        "Reach Confidence": sel(m["reach_confidence"]),
        "Last Pulled": {"date": {"start": TODAY}},
    }

def upsert(v, m):
    props = outcome_props(m)
    page = find_page(v["id"])
    if page:  # update outcomes only; preserve manual feature columns
        requests.patch(f"https://api.notion.com/v1/pages/{page}", headers=NHEAD,
                       json={"properties": props}, timeout=30).raise_for_status()
    else:     # new row: also set identity fields, leave feature cols blank
        props.update({
            "Video": {"title": [{"text": {"content": v["title"][:200]}}]},
            "Video ID": {"rich_text": [{"text": {"content": v["id"]}}]},
            "Published": {"date": {"start": v["published"]}},
        })
        requests.post("https://api.notion.com/v1/pages", headers=NHEAD,
                      json={"parent": {"database_id": NOTION_DB}, "properties": props},
                      timeout=30).raise_for_status()

# ---------------------------------------------------------------- main
def main():
    token = google_token()
    videos = list_videos(token)
    results, errors = [], 0
    for v in videos:
        try:
            m = pull_video(token, v)
            upsert(v, m)
            results.append((v, m))
        except Exception as e:
            errors += 1
            print(f"  ! {v['id']} ({v['title'][:40]}): {e}", file=sys.stderr)

    # run summary (spec section 8.6)
    eligible = [(v, m) for v, m in results if m["analysis_eligible"]
                and m["packaging_ctr"] is not None]
    eligible.sort(key=lambda x: x[1]["packaging_ctr"], reverse=True)
    print(f"\n=== YouTube -> Notion run {TODAY} ===")
    print(f"videos processed: {len(results)} | errors: {errors}")
    print(f"analysis-eligible: {len(eligible)}")
    diluted = [v['title'][:50] for v, m in results if m['signal_diluted']]
    if diluted:
        print("signal-diluted (excluded):", "; ".join(diluted))
    if eligible:
        print("\ntop by packaging CTR:")
        for v, m in eligible[:4]:
            print(f"  {m['packaging_ctr']:.2f}%  {v['title'][:55]}")
        print("bottom by packaging CTR:")
        for v, m in eligible[-4:]:
            print(f"  {m['packaging_ctr']:.2f}%  {v['title'][:55]}")

if __name__ == "__main__":
    main()
