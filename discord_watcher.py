#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discord_watcher.py
- Listens for "📣 Bet placed: <home> vs <away>" messages in a channel
- Resolves match id on api-tennis.com
- Posts/edits an embed with live scores until the match finishes
- Stores mapping (home/away/starts -> api_id/message_id/final) in memory per run
"""

import os, re, asyncio
import requests
import discord
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparse
import os, asyncio
from aiohttp import web

# --------- ENV (set these on the cloud host) ---------
BOT_TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID  = int(os.environ["DISCORD_CHANNEL_ID"])           # numeric
API_BASE    = os.environ["API_TENNIS_BASE_URL"].rstrip("/")
API_KEY     = os.environ["API_TENNIS_KEY"]
AUTH_HDR    = os.environ.get("API_TENNIS_AUTH_HEADER", "Authorization")  # "Authorization" or "x-api-key"
POLL_SECS   = int(os.environ.get("POLL_SECONDS", "30"))
RES_DAYS    = int(os.environ.get("RESOLVE_DAYS_WINDOW", "2"))

# Endpoints (adjust to api-tennis docs if different)
SEARCH_ENDPOINT = "/v1/matches/search"
LIVE_ENDPOINT   = "/v1/matches/{match_id}/live"

def auth_headers():
    return {AUTH_HDR: API_KEY}

def normalize(s:str)->str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z \-'.]", " ", s)
    return re.sub(r"\s+", " ", s)

def extract_bet(msg: str):
    """
    Parses a message in the format:
      📣 Bet placed: <home> vs <away>
      side=home/TEAM1 | odds=1.950 | stake=€25.00
      start=2025-08-31T09:00:00Z
    Returns dict or None.
    """
    lines = [l.strip() for l in msg.splitlines() if l.strip()]
    if not lines: return None
    m1 = re.search(r"Bet placed:\s*(.+?)\s+vs\s+(.+)$", lines[0], re.I)
    if not m1: return None
    home, away = m1.group(1), m1.group(2)
    side = odds = stake = starts = ""
    for ln in lines[1:]:
        if ln.lower().startswith("side="):   side   = ln.split("=",1)[1]
        if ln.lower().startswith("odds="):   odds   = ln.split("=",1)[1]
        if ln.lower().startswith("stake="):  stake  = ln.split("=",1)[1]
        if ln.lower().startswith("start="):  starts = ln.split("=",1)[1]
    return {"home":home, "away":away, "side":side, "odds":odds, "stake":stake, "starts":starts}

async def post_or_update(channel, entry, status_text, score_text, is_final):
    title = f"📣 Bet placed: {entry['home']} vs {entry['away']}"
    desc  = f"Side: **{entry['side']}**  |  Odds: **{entry['odds']}**  |  Stake: **{entry['stake']}**\nStart: `{entry['starts'] or '(n/a)'}`"
    colour = 0x2ecc71 if is_final else 0xf1c40f
    embed = discord.Embed(title=title, description=desc, colour=colour)
    if score_text:
        embed.add_field(name="Score", value=f"`{score_text}`", inline=False)
    embed.add_field(name="Status", value=status_text or "—", inline=True)
    footer = "✅ final" if is_final else f"live tracking • {datetime.now(timezone.utc).strftime('%H:%M:%SZ')}"
    embed.set_footer(text=footer)

    if not entry.get("message_id"):
        msg = await channel.send(embed=embed)
        entry["message_id"] = msg.id
    else:
        try:
            msg = await channel.fetch_message(entry["message_id"])
            await msg.edit(embed=embed)
        except Exception:
            msg = await channel.send(embed=embed)
            entry["message_id"] = msg.id

def resolve_match_id(home: str, away: str, starts_iso: str | None) -> str | None:
    # Build date window
    date_from = date_to = None
    if starts_iso:
        try:
            dt = dtparse.parse(starts_iso)
            date_from = (dt - timedelta(days=RES_DAYS)).date().isoformat()
            date_to   = (dt + timedelta(days=RES_DAYS)).date().isoformat()
        except Exception:
            pass

    params = {"player1": home, "player2": away}
    if date_from and date_to:
        params.update({"from": date_from, "to": date_to})
    r = requests.get(f"{API_BASE}{SEARCH_ENDPOINT}", params=params, headers=auth_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()

    # ADAPT if your provider returns a different shape
    if not isinstance(data, list):
        return None
    h = normalize(home); a = normalize(away)
    best, best_score = None, -1
    for m in data:
        p1 = normalize(str(m.get("player1",""))); p2 = normalize(str(m.get("player2","")))
        if not p1 or not p2: continue
        if not ((h in p1 and a in p2) or (h in p2 and a in p1)): continue
        score = 10
        if starts_iso and m.get("start_time"):
            try:
                mt = dtparse.parse(m["start_time"])
                score += max(0, 5 - abs((mt - dtparse.parse(starts_iso)).days))
            except: pass
        stat = str(m.get("status","")).lower()
        if any(k in stat for k in ("not started","scheduled","in progress","live")):
            score += 5
        if score > best_score:
            best, best_score = m, score
    return str(best["id"]) if best else None

def get_live(mid: str) -> tuple[str, str, bool]:
    r = requests.get(f"{API_BASE}{LIVE_ENDPOINT.format(match_id=mid)}", headers=auth_headers(), timeout=12)
    r.raise_for_status()
    data = r.json()

    # ADAPT to provider’s live shape
    status_text = str(data.get("status","")).strip()
    sets  = data.get("sets") or ""
    games = data.get("games") or ""
    point = data.get("point") or ""
    score_text = " | ".join([t for t in (sets, games, point) if t])
    is_final = status_text.lower() in ("finished", "ended", "completed", "final")
    return status_text, score_text, is_final
    

async def health(request):
    return web.Response(text="ok")
    
async def start_http():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[http] listening on :{port}")

# ------- Bot -------
intents = discord.Intents.default()
intents.message_content = True  # MUST be enabled in the developer portal too
client = discord.Client(intents=intents)

# Simple in-memory registry: message_id -> entry(meta)
REG: dict[int, dict] = {}

@client.event
async def on_ready():
    print(f"[discord] logged in as {client.user} (listening in channel {CHANNEL_ID})")
    
_started = False  # module-level guard so we don't start twice

@client.event
async def on_ready():
    global _started
    print(f"[discord] logged in as {client.user} (listening in channel {CHANNEL_ID})")
    if not _started:
        _started = True
        asyncio.create_task(start_http())   # start tiny HTTP server
        asyncio.create_task(poll_loop())    # start your polling loop

async def poll_loop():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("[discord] channel not found; check DISCORD_CHANNEL_ID")
        return

    while not client.is_closed():
        # update every tracked message
        for mid, entry in list(REG.items()):
            if entry.get("final"): 
                continue
            try:
                status_text, score_text, is_final = get_live(entry["api_id"])
            except Exception as ex:
                print(f"[poll_err] {entry['home']} vs {entry['away']}: {ex}")
                continue
            await post_or_update(channel, entry, status_text, score_text, is_final)
            if is_final:
                entry["final"] = True
        await asyncio.sleep(POLL_SECS)

@client.event
async def on_message(message: discord.Message):
    if message.channel.id != CHANNEL_ID:
        return

    # Accept webhooks; ignore other bot/system messages
    if message.author.bot and message.webhook_id is None:
        return

    content = message.content or ""
    if "Bet placed:" not in content:
        return

    bet = extract_bet(content)
    if not bet:
        return

    # Resolve match id
    api_id = None
    try:
        api_id = resolve_match_id(bet["home"], bet["away"], bet["starts"])
    except Exception as ex:
        print(f"[resolve_err] {bet['home']} vs {bet['away']}: {ex}")
        return
    if not api_id:
        print(f"[resolve_miss] {bet['home']} vs {bet['away']}")
        return

    # Create entry & post initial embed; then poll will keep it updated
    entry = {"home": bet["home"], "away": bet["away"], "side": bet["side"], "odds": bet["odds"],
             "stake": bet["stake"], "starts": bet["starts"], "api_id": api_id, "message_id": None, "final": False}

    await post_or_update(message.channel, entry, status_text="scheduled", score_text="", is_final=False)

    # Register for polling (use the posted embed message id)
    if entry.get("message_id"):
        REG[entry["message_id"]] = entry

if __name__ == "__main__":
    client.run(BOT_TOKEN)