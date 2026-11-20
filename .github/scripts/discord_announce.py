"""Posts a release to a Discord channel through the channel's webhook.

Run by .github/workflows/discord-release.yml in the public repo. Environment:
  WEBHOOK       the announcement channel's webhook URL (repo secret DISCORD_WEBHOOK_URL)
  TEST_WEBHOOK  a test channel's webhook URL (repo secret DISCORD_TEST_WEBHOOK_URL)
  TEST          true: post to TEST_WEBHOOK and ping nobody (the mention is shown, not sent)
  TAG           the release tag, e.g. v2.1.1-alpha
  REPO          owner/name of the public repo
  LANGUAGE      tr, en or both (repo variable DISCORD_LANGUAGE; default both)
  ROLE_ID       optional: the role to mention (repo variable DISCORD_ROLE_ID); nobody else is ever pinged
  STYLE         card (default: Discord's newer components, with screenshots and link buttons) or embed
  GH_TOKEN      optional: a GitHub token for the API
  DRY_RUN       1: print the message instead of posting it

What goes out, per language, from the release notes:
  1. a hidden block, shown only on Discord:  <!-- discord:tr  (newline) markdown (newline) -->
     Lines starting with "-# " become the small print at the bottom of the card.
  2. otherwise the visible text between <!-- discord:tr --> and <!-- /discord -->
  3. otherwise the English part is everything before "## Türkçe" and the Turkish part everything after it.
If Discord refuses a card, the same release goes out as a plain embed instead.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

APP = "Rheadric Lumen"
UA = "RheadricLumen-release-announcer/1.1"
COLOR = 0x22D3EE
DESCRIPTION_LIMIT = 4096  # one embed's description
TOTAL_LIMIT = 6000  # all embeds of one message together
CARD_TEXT_LIMIT = 3800  # all text of a components message (Discord: 4000)
COMPONENTS_V2 = 1 << 15
SCREENSHOTS = ["light", "board-fans", "effects", "telemetry"]  # docs/screenshots/<lang>/<name>.png in the release
WEBHOOK_PREFIXES = tuple(f"https://{host}/api/webhooks/" for host in
                         ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"))

TEXT = {
    "tr": {"title": "{name}: yenilikler", "download": "**İndir:** {links}", "setup": "Kurulum (setup.exe)",
           "zip": "Taşınabilir zip", "more": "[Tamamı GitHub'da]({url})",
           "update": "Uygulama açıksa sağ üstteki yeşil indirme düğmesiyle tek tıkla güncellenir.",
           "early": "Erken erişim sürümü yayında.", "out": "Yeni sürüm yayında.", "news": "Yenilikler",
           "b_setup": "Kurulumu indir", "b_zip": "Taşınabilir zip", "b_notes": "Tüm notlar",
           "shots": {"light": "Işık", "board-fans": "Kasa fanları", "effects": "Efektler", "telemetry": "Telemetri"}},
    "en": {"title": "{name}: what's new", "download": "**Download:** {links}", "setup": "Setup (setup.exe)",
           "zip": "Portable zip", "more": "[Full notes on GitHub]({url})",
           "update": "If the app is open, the green download button at the top right updates it in one click.",
           "early": "An early-access version is out.", "out": "A new version is out.", "news": "What's new",
           "b_setup": "Download the setup", "b_zip": "Portable zip", "b_notes": "Full notes",
           "shots": {"light": "Light", "board-fans": "Case fans", "effects": "Effects", "telemetry": "Telemetry"}},
}


def get_release(repo, tag, token):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def hidden(body, lang):
    m = re.search(rf"<!--\s*discord:{lang}[ \t]*\r?\n(.*?)-->", body, re.S | re.I)
    return m.group(1).strip() if m else None


def section(body, lang):
    found = hidden(body, lang)
    if found is not None:
        return found
    m = re.search(rf"<!--\s*discord:{lang}\s*-->(.*?)<!--\s*/discord\s*-->", body, re.S | re.I)
    if m:
        return m.group(1)
    parts = re.split(r"^##\s+Türkçe\s*$", body, maxsplit=1, flags=re.M)
    if lang == "en":
        return parts[0]
    return parts[1] if len(parts) > 1 else ""


def to_discord(md, headings=False):
    """GitHub Markdown -> Discord: comments go; headings become bold lines unless Discord renders them (cards)."""
    md = md.replace("\r\n", "\n")
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    if not headings:
        md = re.sub(r"^#{1,6}\s+(.+?)\s*#*\s*$", r"**\1**", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def cut(text, limit, more):
    """At most `limit` characters, cut at a line break, ending with a link to the full notes."""
    if len(text) <= limit:
        return text
    tail = f"\n\n… {more}"
    room = limit - len(tail)
    at = text.rfind("\n", 0, room)
    if at < room // 2:
        at = room
    return text[:at].rstrip() + tail


def asset_url(release, suffix):
    for asset in release.get("assets", []):
        if asset.get("name", "").lower().endswith(suffix):
            return asset.get("browser_download_url")
    return None


def identity(repo, ref):
    return {"username": APP, "avatar_url": f"https://raw.githubusercontent.com/{repo}/{ref}/docs/logo.png"}


def mentions(role_id, ping):
    return {"parse": [], "roles": [role_id] if role_id and ping else []}


def build_embed(release, repo, languages, role_id, ping=True):
    name = release.get("name") or release["tag_name"]
    url = release["html_url"]
    body = release.get("body") or ""
    setup, zip_ = asset_url(release, "-setup.exe"), asset_url(release, "-win64.zip")

    parts = []
    for lang in languages:
        t = TEXT[lang]
        links = [f"[{t['setup']}]({setup})" if setup else None, f"[{t['zip']}]({zip_})" if zip_ else None]
        links = " · ".join(link for link in links if link)
        footer = "\n\n".join(x for x in (t["update"], t["download"].format(links=links) if links else "") if x)
        notes = re.sub(r"^-# ", "", to_discord(section(body, lang)), flags=re.M)
        parts.append((t["title"].format(name=name), notes, footer, t["more"].format(url=url)))

    # Share the room between the languages: titles and the fixed footer lines first, the notes get the rest.
    room = TOTAL_LIMIT - 100 - sum(len(title) + len(footer) + 2 for title, _, footer, _ in parts)
    per_notes = min(room // len(parts), DESCRIPTION_LIMIT - max(len(footer) + 2 for _, _, footer, _ in parts))
    embeds = []
    for title, notes, footer, more in parts:
        notes = cut(notes, per_notes, more) if notes else more
        embeds.append({"title": title[:256], "url": url, "description": f"{notes}\n\n{footer}".strip(),
                       "color": COLOR})
    if release.get("published_at"):
        embeds[-1]["timestamp"] = release["published_at"]

    message = {**identity(repo, release["tag_name"]), "embeds": embeds, "allowed_mentions": mentions(role_id, ping)}
    if role_id:
        message["content"] = f"<@&{role_id}>"
    return message


def build_card(release, repo, languages, role_id, ping=True):
    """One container per language: heading + logo, the news, four screenshots, link buttons, small print."""
    tag = release["tag_name"]
    version = tag[1:] if tag.lower().startswith("v") else tag
    raw = f"https://raw.githubusercontent.com/{repo}/{tag}"
    body = release.get("body") or ""
    links = [(asset_url(release, "-setup.exe"), "b_setup"), (asset_url(release, "-win64.zip"), "b_zip"),
             (release["html_url"], "b_notes")]

    containers = []
    room = CARD_TEXT_LIMIT // len(languages)
    for i, lang in enumerate(languages):
        t = TEXT[lang]
        text = to_discord(section(body, lang), headings=True)
        small = [line for line in text.splitlines() if line.startswith("-# ")]
        news = "\n".join(line for line in text.splitlines() if not line.startswith("-# ")).strip()
        intro = t["early"] if "-" in version else t["out"]
        head = (f"<@&{role_id}>\n" if role_id and i == 0 else "") + f"## {APP} {version}\n{intro} {t['update']}"
        news = cut(news, max(200, room - len(head) - sum(len(s) for s in small) - 60), t["more"].format(url=release["html_url"]))

        parts = [
            {"type": 9, "components": [{"type": 10, "content": head}],
             "accessory": {"type": 11, "media": {"url": f"{raw}/docs/logo.png"}, "description": APP}},
            {"type": 14, "divider": True, "spacing": 1},
        ]
        if news:
            parts.append({"type": 10, "content": f"### {t['news']}\n{news}"})
        parts.append({"type": 12, "items": [{"media": {"url": f"{raw}/docs/screenshots/{lang}/{shot}.png"},
                                             "description": t["shots"][shot]} for shot in SCREENSHOTS]})
        parts.append({"type": 14, "divider": True, "spacing": 1})
        buttons = [{"type": 2, "style": 5, "label": t[key], "url": url} for url, key in links if url]
        if buttons:
            parts.append({"type": 1, "components": buttons})
        if small:
            parts.append({"type": 10, "content": "\n".join(small)})
        containers.append({"type": 17, "accent_color": COLOR, "components": parts})

    return {**identity(repo, tag), "flags": COMPONENTS_V2, "components": containers,
            "allowed_mentions": mentions(role_id, ping)}


def post(webhook, message, components):
    """Posts; returns None on success, or Discord's refusal (status, text) so the caller can fall back."""
    data = json.dumps(message).encode("utf-8")
    query = "?wait=true" + ("&with_components=true" if components else "")
    for attempt in range(3):
        req = urllib.request.Request(webhook + query, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"posted ({'card' if components else 'embed'}): Discord answered {r.status}")
                return None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            if e.code == 429 and attempt < 2:
                wait = float(json.loads(detail or "{}").get("retry_after", 2))
                time.sleep(min(wait, 30))
                continue
            return e.code, detail
    return 429, "rate limited"


def main():
    test = (os.environ.get("TEST") or "").strip().lower() == "true"
    webhook = ((os.environ.get("TEST_WEBHOOK") if test else os.environ.get("WEBHOOK")) or "").strip()
    tag, repo = os.environ["TAG"].strip(), os.environ["REPO"].strip()
    dry = os.environ.get("DRY_RUN") == "1"
    if not webhook and not dry:
        if test:
            sys.exit("DISCORD_TEST_WEBHOOK_URL is not set: the test has nowhere to go")
        print("::notice::DISCORD_WEBHOOK_URL is not set: nothing posted")
        return
    if webhook and not webhook.startswith(WEBHOOK_PREFIXES):
        sys.exit("the webhook secret is not a Discord webhook URL")

    language = (os.environ.get("LANGUAGE") or "both").strip().lower()
    languages = {"tr": ["tr"], "en": ["en"]}.get(language, ["tr", "en"])
    role_id = (os.environ.get("ROLE_ID") or "").strip()
    if role_id and not role_id.isdigit():
        sys.exit("DISCORD_ROLE_ID must be the role's numeric id")
    style = (os.environ.get("STYLE") or "card").strip().lower()

    release = get_release(repo, tag, os.environ.get("GH_TOKEN"))
    if release.get("draft"):
        print(f"::notice::{tag} is a draft: nothing posted")
        return
    card = build_card(release, repo, languages, role_id, ping=not test) if style == "card" else None
    embed = build_embed(release, repo, languages, role_id, ping=not test)
    if dry:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(card or embed, ensure_ascii=False, indent=2))
        return
    if test:
        print("test: posting to the test channel, no one is pinged")

    if card is not None:
        refused = post(webhook, card, components=True)
        if refused is None:
            return
        print(f"::warning::Discord refused the card (HTTP {refused[0]} {refused[1]}); sending the embed instead")
    refused = post(webhook, embed, components=False)
    if refused is not None:
        sys.exit(f"Discord refused the message: HTTP {refused[0]} {refused[1]}")


if __name__ == "__main__":
    main()
