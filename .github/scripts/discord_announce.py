"""Posts a release's notes to a Discord channel through the channel's webhook.

Run by .github/workflows/discord-release.yml in the public repo. Environment:
  WEBHOOK   the channel's webhook URL (repo secret DISCORD_WEBHOOK_URL); without it nothing is posted
  TAG       the release tag, e.g. v2.1.1-alpha
  REPO      owner/name of the public repo
  LANGUAGE  tr, en or both (repo variable DISCORD_LANGUAGE; default both)
  ROLE_ID   optional: a role to mention (repo variable DISCORD_ROLE_ID); nobody else is ever pinged
  GH_TOKEN  optional: a GitHub token for the API
  DRY_RUN   1: print the message instead of posting it

The notes choose what goes out: the text between <!-- discord:tr --> (or discord:en) and <!-- /discord -->. Without
those markers the English part is everything before "## Türkçe" and the Turkish part everything after it.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "RheadricLumen-release-announcer/1.0"
COLOR = 0x22D3EE
DESCRIPTION_LIMIT = 4096  # one embed's description
TOTAL_LIMIT = 6000  # all embeds of one message together
WEBHOOK_PREFIXES = tuple(f"https://{host}/api/webhooks/" for host in
                         ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"))

TEXT = {
    "tr": {"title": "{name}: yenilikler", "download": "**İndir:** {links}", "setup": "Kurulum (setup.exe)",
           "zip": "Taşınabilir zip", "more": "[Tamamı GitHub'da]({url})", "update":
           "Uygulama açıksa sağ üstteki yeşil indirme düğmesiyle tek tıkla güncellenir."},
    "en": {"title": "{name}: what's new", "download": "**Download:** {links}", "setup": "Setup (setup.exe)",
           "zip": "Portable zip", "more": "[Full notes on GitHub]({url})", "update":
           "If the app is open, the green download button at the top right updates it in one click."},
}


def get_release(repo, tag, token):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def section(body, lang):
    m = re.search(rf"<!--\s*discord:{lang}\s*-->(.*?)<!--\s*/discord\s*-->", body, re.S | re.I)
    if m:
        return m.group(1)
    parts = re.split(r"^##\s+Türkçe\s*$", body, maxsplit=1, flags=re.M)
    if lang == "en":
        return parts[0]
    return parts[1] if len(parts) > 1 else ""


def to_discord(md):
    """GitHub Markdown -> what an embed shows: headings become bold lines (embeds don't render them)."""
    md = md.replace("\r\n", "\n")
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
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


def build_message(release, repo, languages, role_id):
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
        parts.append((t["title"].format(name=name), to_discord(section(body, lang)), footer, t["more"].format(url=url)))

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

    message = {
        "username": "Rheadric Lumen",
        "avatar_url": f"https://raw.githubusercontent.com/{repo}/main/docs/logo.png",
        "embeds": embeds,
        "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []},
    }
    if role_id:
        message["content"] = f"<@&{role_id}>"
    return message


def post(webhook, message):
    data = json.dumps(message).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(webhook + "?wait=true", data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"posted: Discord answered {r.status}")
                return
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 429 and attempt < 2:
                wait = float(json.loads(detail or "{}").get("retry_after", 2))
                time.sleep(min(wait, 30))
                continue
            sys.exit(f"Discord refused the message: HTTP {e.code} {detail}")


def main():
    webhook = os.environ.get("WEBHOOK", "").strip()
    tag, repo = os.environ["TAG"].strip(), os.environ["REPO"].strip()
    dry = os.environ.get("DRY_RUN") == "1"
    if not webhook and not dry:
        print("::notice::DISCORD_WEBHOOK_URL is not set: nothing posted")
        return
    if webhook and not webhook.startswith(WEBHOOK_PREFIXES):
        sys.exit("DISCORD_WEBHOOK_URL is not a Discord webhook URL")

    language = (os.environ.get("LANGUAGE") or "both").strip().lower()
    languages = {"tr": ["tr"], "en": ["en"]}.get(language, ["tr", "en"])
    role_id = (os.environ.get("ROLE_ID") or "").strip()
    if role_id and not role_id.isdigit():
        sys.exit("DISCORD_ROLE_ID must be the role's numeric id")

    release = get_release(repo, tag, os.environ.get("GH_TOKEN"))
    if release.get("draft"):
        print(f"::notice::{tag} is a draft: nothing posted")
        return
    message = build_message(release, repo, languages, role_id)
    if dry:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        print(f"total embed characters: {sum(len(e['title']) + len(e['description']) for e in message['embeds'])}",
              file=sys.stderr)
        return
    post(webhook, message)


if __name__ == "__main__":
    main()
