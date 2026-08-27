#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Studiekompassets daglige udgivelse - kørt af GitHub Actions, ikke af en Mac.

    python3 udgiv.py --dry-run          # vis hvad der ville ske
    python3 udgiv.py                    # udgiv dagens, hvis der er et
    python3 udgiv.py --dato 2026-09-02

HVORFOR DEN LIGGER HER OG IKKE PÅ EMILS MASKINE
===============================================
Emil bad 27-08-2026 om, at det hele skulle ligge i et eksternt system, og at han
kun skulle forholde sig til det én gang om måneden.

Den forrige løsning var en LaunchAgent på hans Mac. Den overlevede lukkede apps
og genstart, men ikke en slukket maskine. Én maskine er ét enkelt fejlpunkt, og
en måned er lang tid.

DET, MAN SKAL VIDE, FØR MAN LEDER EFTER EN GENVEJ (målt 25-08-2026):
Instagram kan IKKE planlægges via API'et. `scheduled_publish_time` svarer
»(#3) User must be on whitelist«. Metas egen dokumentation for Content
Publishing beskriver slet ikke scheduling; den nævner det kun som appens eget
ansvar: »We recommend that your app also enforce the publishing rate limit,
especially if your app allows app users to schedule posts to be published in the
future.« Meta forventer altså, at køen ligger i din app - ikke hos dem.

Derfor er ETHVERT eksternt planlægningsværktøj - Buffer, Later, Metricool,
Postiz - en jobkø, der gemmer opslaget og kalder API'et, når minuttet kommer.
Den eneste undtagelse er Meta Business Suite, hvor Meta selv holder køen.

DEN HER FIL ER JOBKØEN. Den kører på GitHubs servere. Det er ikke en omvej uden
om en manglende funktion; det er præcis den løsning, alle andre bruger.

Facebook KAN planlægges hos Meta, men kun 29 dage frem (+29 accepteret, +30
afvist, målt 25-08). Det rækker ikke til en måned lagt ind på forhånd, og det
ville splitte sandheden i to. Derfor kører begge kanaler herfra.

IDEMPOTENS: kørslen spørger ALTID Graph, om dagens aktiv allerede ligger der,
før den udgiver. Kører noget andet også - Emils Mac, en agent - bliver der
stadig kun udgivet én gang.
"""
import argparse
import datetime
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HER = os.path.dirname(os.path.abspath(__file__))
PAGE_ID = "1200096559847576"
IG_ID = "17841433070531346"
API = "https://graph.facebook.com/v21.0"
RAW = "https://raw.githubusercontent.com/Rexmaxusss/studiekompasset-medier/main"
TOKEN = os.environ.get("STUDIEKOMPASSET_FB_TOKEN", "")


def log(*a):
    print(*a, flush=True)


def _kald(url, data=None, headers=None, raw=False):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if not raw else data
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            t = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "replace")
    try:
        return json.loads(t)
    except ValueError:
        return {"raw": t}


def graph(sti, **p):
    p["access_token"] = TOKEN
    return _kald(f"{API}/{sti}?" + urllib.parse.urlencode(p))


def post(sti, **p):
    p["access_token"] = TOKEN
    return _kald(f"{API}/{sti}", data=p)


def allerede_ude(dato):
    """Ét opslag pr. kanal pr. dag. Måles på PUBLICEREDE aktiver, ikke på en kø.

    Facebook måles på published_posts og ikke på video_reels: en udgivet reel
    laver BÅDE en reel- og en feedindgang, mens video_reels ikke kan se et
    feedkort. Målt 24-08, hvor der lå et kort men ingen reel - video_reels sagde
    »intet ude«, hvilket ville have givet to opslag samme dag."""
    fb = graph(f"{PAGE_ID}/published_posts", fields="id,created_time", limit=15)
    fb_ude = any(x.get("created_time", "").startswith(dato) for x in fb.get("data", []))
    ig = graph(f"{IG_ID}/media", fields="id,timestamp", limit=10)
    ig_ude = any(x.get("timestamp", "").startswith(dato) for x in ig.get("data", []))
    return fb_ude, ig_ude


def vent_paa_faerdig(container, forsoeg=40):
    """Instagram skal have containeren FINISHED, før den kan udgives (~25 sek.)."""
    import time
    for _ in range(forsoeg):
        d = graph(container, fields="status_code,status")
        kode = d.get("status_code")
        if kode == "FINISHED":
            return True
        if kode == "ERROR":
            log("   container-fejl:", str(d.get("status"))[:180])
            return False
        time.sleep(5)
    return False


def ig_video(url, caption=None, story=False):
    """Instagram henter selv videoen fra en offentlig URL - filupload afvises."""
    p = {"media_type": "STORIES" if story else "REELS", "video_url": url}
    if caption and not story:
        p["caption"] = caption
    d = post(f"{IG_ID}/media", **p)
    cid = d.get("id")
    if not cid:
        log("   FEJL ved container:", str(d)[:200])
        return None
    if not vent_paa_faerdig(cid):
        return None
    ud = post(f"{IG_ID}/media_publish", creation_id=cid)
    return ud.get("id")


def fb_video(sti_paa_disk, kant, description=None):
    """Facebooks tre-trins flow: start, upload af bytes, finish."""
    s = post(f"{PAGE_ID}/{kant}", upload_phase="start")
    vid, up = s.get("video_id"), s.get("upload_url")
    if not (vid and up):
        log("   FEJL ved start:", str(s)[:200])
        return None
    stoerrelse = os.path.getsize(sti_paa_disk)
    with open(sti_paa_disk, "rb") as f:
        r = _kald(up, data=f.read(), raw=True, headers={
            "Authorization": f"OAuth {TOKEN}",
            "offset": "0",
            "file_size": str(stoerrelse),
            "Content-Type": "application/octet-stream",
        })
    if not r.get("success"):
        log("   FEJL ved upload:", str(r)[:200])
        return None
    p = {"upload_phase": "finish", "video_id": vid}
    if kant == "video_reels":
        p["video_state"] = "PUBLISHED"
    if description:
        p["description"] = description
    f2 = post(f"{PAGE_ID}/{kant}", **p)
    if not f2.get("success"):
        log("   FEJL ved finish:", str(f2)[:200])
        return None
    return vid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dato")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # Europe/Copenhagen. GitHub-løbere kører i UTC, og en udgivelse kl. 10.15
    # dansk tid må ikke havne på den forkerte dato omkring midnat.
    nu = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    dato = a.dato or nu.date().isoformat()

    plan_sti = os.path.join(HER, "plan", f"{dato[:7]}.json")
    if not os.path.exists(plan_sti):
        log(f"{dato}: ingen plan for {dato[:7]}. Filen {os.path.basename(plan_sti)} mangler.")
        return 3          # 3 = månedsplanen mangler; workflowet skal larme på den
    plan = json.load(open(plan_sti, encoding="utf-8"))
    dagens = [u for u in plan if u["dato"] == dato]
    if not dagens:
        log(f"{dato}: ingen planlagt udgivelse. Ingenting gjort.")
        return 0

    u = dagens[0]
    reel = f"studiekompasset-{u['navn']}-{u['dato']}.mp4"
    story = f"studiekompasset-story-{u['navn']}-{u['dato']}.mp4"
    log(f"{dato}: {u['navn']}")

    for f in (reel, story):
        if not os.path.exists(os.path.join(HER, f)):
            log(f"  STOP: {f} findes ikke i arkivet.")
            return 2

    if not TOKEN:
        log("  STOP: STUDIEKOMPASSET_FB_TOKEN mangler.")
        return 4

    fb_ude, ig_ude = allerede_ude(dato)
    log(f"  allerede ude i dag:  facebook={fb_ude}  instagram={ig_ude}")
    if fb_ude and ig_ude:
        log("  Begge kanaler har allerede dagens aktiv. Ingenting gjort.")
        return 0

    if a.dry_run:
        log(f"  TØRLØB: ville udgive {reel} + {story}")
        log(f"    IG-url: {RAW}/{urllib.parse.quote(reel)}")
        return 0

    fejl = []
    if not ig_ude:
        log("  1/4 Instagram-reel")
        r = ig_video(f"{RAW}/{urllib.parse.quote(reel)}", u["ig_tekst"])
        log("   ->", r or "FEJLEDE"); fejl += [] if r else ["ig-reel"]
        log("  2/4 Instagram-story")
        r = ig_video(f"{RAW}/{urllib.parse.quote(story)}", story=True)
        log("   ->", r or "FEJLEDE"); fejl += [] if r else ["ig-story"]
    if not fb_ude:
        log("  3/4 Facebook-reel")
        r = fb_video(os.path.join(HER, reel), "video_reels", u["fb_tekst"])
        log("   ->", r or "FEJLEDE"); fejl += [] if r else ["fb-reel"]
        log("  4/4 Facebook-story")
        r = fb_video(os.path.join(HER, story), "video_stories")
        log("   ->", r or "FEJLEDE"); fejl += [] if r else ["fb-story"]

    # Efterkontrol: et »success: true« er ikke bevis for, at noget står live.
    fb2, ig2 = allerede_ude(dato)
    log(f"  EFTERKONTROL: facebook={fb2}  instagram={ig2}")
    if fejl:
        log("  FEJLEDE FLADER:", ", ".join(fejl))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
