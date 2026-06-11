#!/usr/bin/env python3
"""
youtube_to_notion.py  (v2 — finalized metric set)
-------------------------------------------------
Pulls per-video YouTube Analytics and upserts into the "Video Performance"
Notion DB. Run by the weekly Routine. Deterministic ETL — no model reasoning.

What's new in v2:
  - Long vs Short format split (different anchors per format)
  - Duration captured (so % watched can be read against video length)
  - Funnel metrics: impressions/CTR, watch time, AVD, AVD%, retention,
    engagement RATES per 1k views, subs per 1k
  - Age anchors measured at matched age, then frozen:
        Long : 7d / 30d / 90d      Short: 48h / 14d / 60d
  - Headline columns reflect the LATEST reached anchor; "Snapshot Age" says
    which, so you compare like-for-like. Full anchor set stored in JSON.
  - Self-provisions any missing Notion columns on startup.

Env vars (Routine secrets, no quotes):
  GOOGLE_CLIENT_ID  GOOGLE_CLIENT_SECRET  GOOGLE_REFRESH_TOKEN
  NOTION_TOKEN      NOTION_DATABASE_ID (defaults below)

Requires: pip install requests
"""

import os, re, sys, json, datetime as dt
import requests

# ---------------------------------------------------------------- config
NOTION_DB    = os.environ.get("NOTION_DATABASE_ID", "b355bee4c364440bb99c7e45b2cfb04d")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NVER         = "2022-06-28"
G_ID, G_SECRET, G_REFRESH = (os.environ["GOOGLE_CLIENT_ID"],
                             os.environ["GOOGLE_CLIENT_SECRET"],
                             os.environ["GOOGLE_REFRESH_TOKEN"])

PACKAGING_SOURCES = {"SUBSCRIBER", "RELATED_VIDEO"}   # Browse/home/subs + Suggested
SEARCH_SOURCE     = "YT_SEARCH"
REACH_START       = "2026-01-15"   # thumbnail-impression metrics begin here
SHORT_MAX_SEC     = 181            # Shorts are <=180s
POWER_IMPR        = 2000
DILUTION_PCT      = 30.0
CTR_IS_RATIO      = True           # videoThumbnailImpressionsClickRate is 0..1
TODAY             = dt.date.today()
TODAY_S           = TODAY.isoformat()

ANCHORS = {
    "Long":  [("early", 7), ("mid", 30), ("mature", 90)],
    "Short": [("early", 2), ("mid", 14), ("mature", 60)],
}

# ---------------------------------------------------------------- google
def google_token():
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": G_ID, "client_secret": G_SECRET,
        "refresh_token": G_REFRESH, "grant_type": "refresh_token"})
    r.raise_for_status()
    return r.json()["access_token"]

def gget(url, token, **params):
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def iso_seconds(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

def list_videos(token):
    ch = gget("https://www.googleapis.com/youtube/v3/channels", token,
              part="contentDetails", mine="true")
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], ""
    while True:
        resp = gget("https://www.googleapis.com/youtube/v3/playlistItems", token,
                    part="contentDetails", playlistId=uploads, maxResults=50, pageToken=page)
        ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        page = resp.get("nextPageToken", "")
        if not page:
            break
    out = []
    for i in range(0, len(ids), 50):
        resp = gget("https://www.googleapis.com/youtube/v3/videos", token,
                    part="snippet,contentDetails", id=",".join(ids[i:i+50]))
        for it in resp.get("items", []):
            out.append({"id": it["id"], "title": it["snippet"]["title"],
                        "published": it["snippet"]["publishedAt"][:10],
                        "duration_s": iso_seconds(it["contentDetails"]["duration"])})
    return out

def detect_format(vid, duration):
    """Short if the /shorts/ URL serves directly (200) rather than redirecting."""
    if duration and duration > SHORT_MAX_SEC:
        return "Long"
    try:
        r = requests.get(f"https://www.youtube.com/shorts/{vid}",
                         allow_redirects=False, timeout=15)
        return "Short" if r.status_code == 200 else "Long"
    except Exception:
        return "Short" if (duration and duration <= 60) else "Long"

# ---------------------------------------------------------------- analytics
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"

def query(token, metrics, vid, start, end, dimensions=None, extra=""):
    filt = f"video=={vid}" + (f";{extra}" if extra else "")
    params = {"ids": "channel==MINE", "startDate": start, "endDate": end,
              "metrics": metrics, "filters": filt}
    if dimensions:
        params["dimensions"] = dimensions
    r = requests.get(ANALYTICS, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    return r.json() if r.status_code == 200 else None

def rows(resp):
    return resp.get("rows", []) if resp else []

def per_1k(x, views):
    return round(x / views * 1000, 2) if (x is not None and views) else None

def engagement(token, vid, start, end):
    r = query(token, "views,estimatedMinutesWatched,averageViewDuration,"
              "averageViewPercentage,likes,comments,shares,subscribersGained",
              vid, start, end)
    if not (r and rows(r)):
        return None
    v, mins, avd, avdp, likes, comments, shares, subs = rows(r)[0]
    return {"views": v, "watch_min": mins, "avd_s": avd, "avd_pct": avdp,
            "likes_1k": per_1k(likes, v), "comments_1k": per_1k(comments, v),
            "shares_1k": per_1k(shares, v), "subs_1k": per_1k(subs, v)}

def reach(token, vid, start, end):
    """Long-form packaging. Returns (pkg_ctr%, search_ctr%, feed_impr, confidence)."""
    a = query(token, "videoThumbnailImpressions,videoThumbnailImpressionsClickRate",
              vid, start, end, dimensions="insightTrafficSourceType")
    feed_i = pkg_c = srch_i = srch_c = 0.0
    conf = "High"
    if a and rows(a):
        for src, impr, rate in rows(a):
            impr = impr or 0; clicks = impr * (rate or 0)
            if src in PACKAGING_SOURCES:
                feed_i += impr; pkg_c += clicks
            elif src == SEARCH_SOURCE:
                srch_i += impr; srch_c += clicks
    else:  # fallback: blended reach (no traffic-source split)
        b = query(token, "videoThumbnailImpressions,videoThumbnailImpressionsClickRate",
                  vid, start, end)
        if b and rows(b):
            impr, rate = rows(b)[0]
            feed_i = impr or 0; pkg_c = (impr or 0) * (rate or 0); conf = "Low"
    mul = 100 if CTR_IS_RATIO else 1
    pkg = round(pkg_c / feed_i * mul, 2) if feed_i else None
    srch = round(srch_c / srch_i * mul, 2) if srch_i else None
    return pkg, srch, feed_i, conf

def retention(token, vid, duration, end):
    if not duration:
        return None, None, None
    d = query(token, "audienceWatchRatio", vid, REACH_START, end,
              dimensions="elapsedVideoTimeRatio", extra="audienceType==ORGANIC")
    if not (d and rows(d)):
        return None, None, None
    def at(sec):
        target = sec / duration
        best = min(rows(d), key=lambda r: abs(r[0] - target))
        return round(best[1] * 100, 1)
    r30 = at(30); r3 = at(3)
    intro = round(100 - r30, 1) if r30 is not None else None
    return r30, r3, intro

def external_share(token, vid, end):
    c = query(token, "views", vid, REACH_START, end, dimensions="insightTrafficSourceType")
    tot = ext = note = 0.0
    for src, v in rows(c):
        tot += v or 0
        if src == "EXT_URL":   ext += v or 0
        if src == "NOTIFICATION": note += v or 0
    share = round(ext / tot * 100, 1) if tot else None
    diluted = bool(tot and (ext + note) / tot * 100 > DILUTION_PCT)
    return share, diluted

def engaged_views(token, vid, end):
    # Shorts "engaged views" — may not be exposed in the API; fail soft.
    r = query(token, "engagedViews", vid, REACH_START, end)
    try:
        return rows(r)[0][0] if (r and rows(r)) else None
    except Exception:
        return None

# ---------------------------------------------------------------- per video
def pull_video(token, v):
    fmt = detect_format(v["id"], v["duration_s"])
    pub = dt.date.fromisoformat(v["published"])

    # --- engagement / retention: age-anchored (these metrics exist for all time) ---
    snaps, latest = {}, None
    for name, days in ANCHORS[fmt]:
        if pub + dt.timedelta(days=days) > TODAY:        # window not complete yet
            continue
        end_s = (pub + dt.timedelta(days=days)).isoformat()
        snap = engagement(token, v["id"], v["published"], end_s) or {}
        snap["age_days"] = days
        snaps[name] = snap
        latest = name
    head = snaps.get(latest, {}) if latest else {}

    # --- reach / CTR: LIFETIME, not anchored. The thumbnail-impression metric only
    #     exists from REACH_START, so an old video's first-90-day window predates it
    #     entirely. Pull cumulative available impressions instead. ---
    pkg = srch = None
    feed_i = 0
    reach_conf = "n/a"
    if fmt == "Long":
        reach_conf = "High" if v["published"] >= REACH_START else "Low"
        pkg, srch, feed_i, _ = reach(token, v["id"], v["published"], TODAY_S)
        if not feed_i:        # API may not serve pre-metric dates; retry from metric start
            pkg, srch, feed_i, _ = reach(token, v["id"], REACH_START, TODAY_S)

    r30, r3, intro = retention(token, v["id"], v["duration_s"], TODAY_S)
    ext_share, diluted = external_share(token, v["id"], TODAY_S)
    eng_v = engaged_views(token, v["id"], TODAY_S) if fmt == "Short" else None

    matured  = ("mature" in snaps)
    power_ok = feed_i >= POWER_IMPR
    eligible = bool(fmt == "Long" and matured and power_ok and not diluted)

    return {
        "format": fmt, "duration_s": v["duration_s"], "snapshot_age": latest,
        "views": head.get("views"), "watch_min": head.get("watch_min"),
        "avd_s": head.get("avd_s"), "avd_pct": head.get("avd_pct"),
        "pkg_ctr": pkg, "search_ctr": srch,
        "feed_impr": feed_i if fmt == "Long" else None,
        "likes_1k": head.get("likes_1k"), "comments_1k": head.get("comments_1k"),
        "shares_1k": head.get("shares_1k"), "subs_1k": head.get("subs_1k"),
        "engaged_views": eng_v, "ret_30s": r30, "intro_drop": intro,
        "ext_share": ext_share, "diluted": diluted, "matured": matured,
        "power_ok": power_ok, "eligible": eligible, "reach_conf": reach_conf,
        "snapshots_json": json.dumps({"format": fmt, "duration_s": v["duration_s"],
                                      "lifetime_reach": {"pkg_ctr": pkg, "search_ctr": srch,
                                                         "feed_impr": feed_i, "conf": reach_conf},
                                      "retention": {"at_30s": r30, "at_3s": r3,
                                                    "intro_drop": intro},
                                      "external_share": ext_share,
                                      "engaged_views": eng_v, "anchors": snaps},
                                     separators=(",", ":"))[:1990],
    }

# ---------------------------------------------------------------- notion
NHEAD = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NVER,
         "Content-Type": "application/json"}

def ensure_schema():
    want = {
        "Format": {"select": {}}, "Duration (s)": {"number": {"format": "number"}},
        "Watch Time (min)": {"number": {"format": "number"}},
        "Engaged Views": {"number": {"format": "number"}},
        "Likes /1k": {"number": {"format": "number"}},
        "Comments /1k": {"number": {"format": "number"}},
        "Shares /1k": {"number": {"format": "number"}},
        "Subs /1k": {"number": {"format": "number"}},
        "Snapshots JSON": {"rich_text": {}}, "Snapshot Age": {"select": {}},
    }
    db = requests.get(f"https://api.notion.com/v1/databases/{NOTION_DB}",
                      headers=NHEAD, timeout=30)
    db.raise_for_status()
    have = set(db.json().get("properties", {}).keys())
    missing = {k: v for k, v in want.items() if k not in have}
    if missing:
        requests.patch(f"https://api.notion.com/v1/databases/{NOTION_DB}",
                       headers=NHEAD, json={"properties": missing},
                       timeout=30).raise_for_status()
        print(f"schema: added {len(missing)} columns -> {', '.join(missing)}")

def num(x):  return {"number": float(x)} if x is not None else {"number": None}
def chk(x):  return {"checkbox": bool(x)}
def sel(x):  return {"select": {"name": str(x)}} if x else {"select": None}
def txt(x):  return {"rich_text": [{"text": {"content": x}}] if x else []}

def outcome_props(m):
    return {
        "Format": sel(m["format"]), "Duration (s)": num(m["duration_s"]),
        "Snapshot Age": sel(m["snapshot_age"]),
        "Packaging CTR %": num(m["pkg_ctr"]), "Search CTR %": num(m["search_ctr"]),
        "Feed Impressions": num(m["feed_impr"]), "Views": num(m["views"]),
        "Watch Time (min)": num(m["watch_min"]), "AVD (s)": num(m["avd_s"]),
        "AVD %": num(m["avd_pct"]), "Engaged Views": num(m["engaged_views"]),
        "Likes /1k": num(m["likes_1k"]), "Comments /1k": num(m["comments_1k"]),
        "Shares /1k": num(m["shares_1k"]), "Subs /1k": num(m["subs_1k"]),
        "Retention @30s %": num(m["ret_30s"]), "Intro Drop %": num(m["intro_drop"]),
        "External Share %": num(m["ext_share"]), "Signal Diluted": chk(m["diluted"]),
        "Matured": chk(m["matured"]), "Power OK": chk(m["power_ok"]),
        "Analysis Eligible": chk(m["eligible"]),
        "Reach Confidence": sel(m["reach_conf"]),
        "Snapshots JSON": txt(m["snapshots_json"]),
        "Last Pulled": {"date": {"start": TODAY_S}},
    }

def find_page(vid):
    r = requests.post(f"https://api.notion.com/v1/databases/{NOTION_DB}/query",
                      headers=NHEAD, timeout=30,
                      json={"filter": {"property": "Video ID", "rich_text": {"equals": vid}}})
    r.raise_for_status()
    res = r.json().get("results", [])
    return res[0]["id"] if res else None

def upsert(v, m):
    props = outcome_props(m)
    page = find_page(v["id"])
    if page:  # update outcomes only; manual feature columns are preserved
        requests.patch(f"https://api.notion.com/v1/pages/{page}", headers=NHEAD,
                       json={"properties": props}, timeout=30).raise_for_status()
    else:
        props.update({"Video": {"title": [{"text": {"content": v["title"][:200]}}]},
                      "Video ID": {"rich_text": [{"text": {"content": v["id"]}}]},
                      "Published": {"date": {"start": v["published"]}}})
        requests.post("https://api.notion.com/v1/pages", headers=NHEAD, timeout=30,
                      json={"parent": {"database_id": NOTION_DB},
                            "properties": props}).raise_for_status()

# ---------------------------------------------------------------- main
def main():
    ensure_schema()
    token = google_token()
    videos = list_videos(token)
    results, errors = [], 0
    for v in videos:
        try:
            m = pull_video(token, v); upsert(v, m); results.append((v, m))
        except Exception as e:
            errors += 1
            print(f"  ! {v['id']} ({v['title'][:40]}): {e}", file=sys.stderr)

    longs   = [(v, m) for v, m in results if m["format"] == "Long"]
    shorts  = [(v, m) for v, m in results if m["format"] == "Short"]
    elig    = [(v, m) for v, m in results if m["eligible"] and m["pkg_ctr"] is not None]
    elig.sort(key=lambda x: x[1]["pkg_ctr"], reverse=True)
    print(f"\n=== YouTube -> Notion run {TODAY_S} ===")
    print(f"processed: {len(results)} ({len(longs)} long / {len(shorts)} short) | errors: {errors}")
    print(f"analysis-eligible (long, mature+power): {len(elig)}")
    if elig:
        print("top packaging CTR:")
        for v, m in elig[:4]:
            print(f"  {m['pkg_ctr']:.2f}%  {v['title'][:55]}")
        print("bottom packaging CTR:")
        for v, m in elig[-4:]:
            print(f"  {m['pkg_ctr']:.2f}%  {v['title'][:55]}")

if __name__ == "__main__":
    main()