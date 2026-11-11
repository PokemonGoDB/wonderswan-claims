#!/usr/bin/env python3
"""Who is translating which WonderSwan game.

    claim.py list                      every game someone is working on
    claim.py check "Blue Wing Blitz"   is it free? (exit 0 free, 1 taken, 2 a patch already exists)
    claim.py claim "Blue Wing Blitz"   put your name on it (run again to refresh the date)
    claim.py done  "Blue Wing Blitz"   take your name off (released, or given up)
    claim.py wanted                    the games people voted for most, and whether each is free

The list is claims.json in https://github.com/PokemonGoDB/wonderswan-claims. Reading needs nothing.
Writing needs push access to that repository and a GitHub token: the GH_TOKEN or GITHUB_TOKEN
environment variable, or a signed-in `gh` command. Python 3.8+, no packages.
"""
import argparse
import base64
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("WS_CLAIMS_REPO", "PokemonGoDB/wonderswan-claims")
BRANCH = "main"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com/%s/%s/" % (REPO, BRANCH)
CATALOG = "https://raw.githubusercontent.com/mariovirgili/WonderSwan_bps_repo/main/index.json"
WANTED = os.environ.get("WS_WANTED_URL", "https://swanrevival.com/api/wanted.json")
VOTE_PAGE = "https://swanrevival.com/translations/wanted/"
EDIT_URL = "https://github.com/%s/edit/%s/claims.json" % (REPO, BRANCH)
HERE = os.path.dirname(os.path.abspath(__file__))
STALE_DAYS = 30
STATUSES = ("working", "paused")
SYSTEMS = {"wonderswan": "WonderSwan", "wonderswan_color": "WonderSwan Color"}
LANGS = {"en": "English", "es": "Spanish", "it": "Italian", "fr": "French", "de": "German",
         "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese", "ko": "Korean"}
FIELDS = ("game", "system", "who", "language", "status", "since", "updated", "note")


class Conflict(Exception):
    pass


def die(message, code=70):
    print("claim.py: " + message, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- GitHub

_token = False


def token():
    global _token
    if _token is False:
        _token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None
        if not _token:
            try:
                out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
                _token = out.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                _token = None
    return _token


def http(url, tok=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "wonderswan-claims")
    if data is not None:
        # urllib would call the body a web form; GitHub does not mind, but a proxy in between may refuse it (415).
        req.add_header("Content-Type", "application/json")
    if "api.github.com" in url:
        req.add_header("Accept", "application/vnd.github+json")
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def tidy(data):
    """Fill in what a hand edit left out, so one bad block never stops the others being read."""
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        die("claims.json is not in the expected shape; fix it by hand:\n  " + EDIT_URL)
    data["claims"] = [c for c in data["claims"] if isinstance(c, dict) and isinstance(c.get("game"), str)]
    for c in data["claims"]:
        for field in FIELDS:
            if not isinstance(c.get(field), str):
                c[field] = {"status": "working", "language": "English", "note": "",
                            "updated": c.get("since") if isinstance(c.get("since"), str) else "?"}.get(field, "?")
    return data


def load(local, raw=False):
    """Return (data, sha). sha is None for a local file or a read without the API."""
    fix = (lambda d: d) if raw else tidy
    if local:
        with open(os.path.join(HERE, "claims.json"), encoding="utf-8") as f:
            text = f.read()
        try:
            return fix(json.loads(text)), None
        except ValueError as e:
            if raw:
                raise
            die("claims.json is not valid JSON (%s)" % e)
    try:
        meta = json.loads(http("%s/repos/%s/contents/claims.json?ref=%s" % (API, REPO, BRANCH), token()))
        return fix(json.loads(base64.b64decode(meta["content"]).decode("utf-8"))), meta["sha"]
    except ValueError as e:
        die("claims.json is not valid JSON (%s); fix it by hand:\n  %s" % (e, EDIT_URL))
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):  # rate limit without a token: read the public copy instead
            die("cannot read the claims list (%s)" % e)
    except urllib.error.URLError as e:
        die("cannot reach GitHub (%s)" % e.reason)
    return fix(json.loads(http(RAW + "claims.json").decode("utf-8"))), None


def save(local, data, sha, message):
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if local:
        with open(os.path.join(HERE, "claims.json"), "w", encoding="utf-8") as f:
            f.write(text)
        return
    if not token():
        die("no GitHub token. Sign in with `gh auth login`, or set GH_TOKEN, or edit the list by hand:\n  "
            + EDIT_URL)
    body = {"message": message, "branch": BRANCH, "sha": sha,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    try:
        http("%s/repos/%s/contents/claims.json" % (API, REPO), token(), "PUT", body)
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):
            raise Conflict()
        if e.code in (403, 404):
            die("GitHub refused the change (%d). You need push access to %s; ask its owner to add you, "
                "or edit by hand:\n  %s" % (e.code, REPO, EDIT_URL))
        die("GitHub refused the change (%s)" % e)


def change(local, edit):
    """Run edit(data) -> commit message (or None for no change), retrying if someone else wrote first."""
    for _ in range(5):
        data, sha = load(local)
        if not local and sha is None:
            die("cannot write without a GitHub token (see --help)")
        message = edit(data)
        if message is None:
            return
        try:
            save(local, data, sha, message)
            return
        except Conflict:
            continue
    die("the list kept changing while writing; try again")


# ---------------------------------------------------------------- titles

def base(name):
    return re.sub(r"\s*\([^)]*\)", "", name).strip()


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class Titles:
    def __init__(self):
        path = os.path.join(HERE, "titles.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)["titles"]
        else:
            rows = json.loads(http(RAW + "titles.json").decode("utf-8"))["titles"]
        self.system, self.by_crc, self.by_norm = {}, {}, {}
        for row in rows:
            game = base(row["name"])
            system = SYSTEMS.get(row["system"], row["system"])
            if system not in self.system.setdefault(game, []):
                self.system[game].append(system)
            self.by_crc[row["crc32"].upper()] = game
            self.by_norm[norm(game)] = game

    def system_of(self, game):
        return " / ".join(self.system.get(game, ["?"]))

    def find(self, query):
        """Return (game, None) or (None, why)."""
        if re.fullmatch(r"[0-9A-Fa-f]{8}", query.strip()):
            game = self.by_crc.get(query.strip().upper())
            return (game, None) if game else (None, "no title has the CRC32 %s" % query.upper())
        want = norm(base(query)) or norm(query)
        if want in self.by_norm:
            return self.by_norm[want], None
        words = want.split()
        hits = sorted(g for n, g in self.by_norm.items() if all(w in n for w in words))
        if len(hits) == 1:
            return hits[0], None
        if hits:
            return None, "%d titles match \"%s\"; be more specific:\n  %s" % (
                len(hits), query, "\n  ".join(hits[:15]) + ("\n  ..." if len(hits) > 15 else ""))
        def score(name):
            parts = name.split()
            return sum(max(difflib.SequenceMatcher(None, w, t).ratio() for t in parts) for w in words) / len(words)
        near = sorted((n for n in self.by_norm if score(n) >= 0.7), key=score, reverse=True)[:5] if words else []
        hint = "; did you mean:\n  " + "\n  ".join(self.by_norm[n] for n in near) if near else ""
        return None, "no WonderSwan title matches \"%s\"%s" % (query, hint)


def language(text):
    text = (text or "English").strip()
    return LANGS.get(text.lower(), text[:1].upper() + text[1:])


def released(titles):
    """game -> [(owner, language, version, url)] from the shared patch catalog; None if unreachable."""
    try:
        entries = json.loads(http(CATALOG)).get("entries", [])
    except (OSError, ValueError):
        return None
    out = {}
    for e in entries:
        if not e.get("latest") or e.get("superseded") or e.get("duplicate_of"):
            continue
        game = titles.by_crc.get((e.get("source_crc32") or "").upper())
        if not game:
            continue
        origin = e.get("origin") or {}
        owner = (origin.get("source") or "?").split("/")[0]
        lang = LANGS.get(e.get("language") or "", "language not stated")
        url = origin.get("release_url") or origin.get("file_url") or origin.get("source_url") or ""
        rows = out.setdefault(game, [])
        if not any(r[0] == owner and r[1] == lang for r in rows):
            rows.append((owner, lang, e.get("version") or "?", url))
    return out


# ---------------------------------------------------------------- commands

def today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def age(claim):
    try:
        then = datetime.date.fromisoformat(claim.get("updated") or claim.get("since"))
    except (TypeError, ValueError):
        return 0
    return (datetime.datetime.now(datetime.timezone.utc).date() - then).days


def same(a, b):
    return (a or "").lower() == (b or "").lower()


def me(args, needed=False):
    who = args.who or os.environ.get("WS_CLAIM_WHO")
    if not who and not args.local and token():
        try:
            who = json.loads(http(API + "/user", token()))["login"]
        except (OSError, ValueError, KeyError):
            who = None
    if not who and needed:
        die("who are you? Add --who YOUR_GITHUB_NAME (or sign in with `gh auth login`)")
    return who


def describe(claim):
    text = "%s, %s, %s since %s" % (claim["who"], claim["language"], claim["status"], claim["since"])
    if claim.get("updated") and claim["updated"] != claim["since"]:
        text += ", updated %s" % claim["updated"]
    if age(claim) > STALE_DAYS:
        text += " (STALE: no update for %d days, ask them)" % age(claim)
    if claim.get("note"):
        text += " - " + claim["note"]
    return text


def need_game(titles, query):
    game, why = titles.find(query)
    if not game:
        die(why, 64)
    return game


def cmd_list(args):
    data, _ = load(args.local)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    claims = sorted(data["claims"], key=lambda c: norm(c["game"]))
    if not claims:
        print("Nobody has claimed a game.")
    for c in claims:
        print("%s (%s): %s" % (c["game"], c["system"], describe(c)))
    return 0


def cmd_check(args):
    titles = Titles()
    game = need_game(titles, args.game)
    lang, who = language(args.lang), me(args)
    data, _ = load(args.local)
    claims = [c for c in data["claims"] if norm(c["game"]) == norm(game)]
    patches = released(titles)
    print("%s (%s)" % (game, titles.system_of(game)))
    for c in claims:
        print("  In progress: " + describe(c))
    if patches is None:
        print("  Released: unknown (the patch catalog could not be read)")
    for owner, plang, version, url in (patches or {}).get(game, []):
        print("  Released: %s, %s, v%s %s" % (owner, plang, version, url))
    others = [c for c in claims if same(c["language"], lang) and not same(c["who"], who)]
    mine = [c for c in claims if same(c["language"], lang) and same(c["who"], who)]
    done = [p for p in (patches or {}).get(game, []) if same(p[1], lang) and not same(p[0], who)]
    if others:
        print("TAKEN: %s is working on the %s translation. Do not start; ask them first." % (
            " and ".join(c["who"] for c in others), lang))
        return 1
    if mine:
        print("YOURS: you have claimed it.")
        return 0
    if done:
        print("FREE, BUT ALREADY TRANSLATED: %s patch by %s. Decide whether a second one is worth it." % (
            lang, ", ".join("%s (v%s)" % (p[0], p[2]) for p in done)))
        return 2
    print("FREE: nobody has claimed the %s translation." % lang)
    return 0


def cmd_claim(args):
    titles = Titles()
    game = need_game(titles, args.game)
    lang, who = language(args.lang), me(args, needed=True)
    result = {}

    def edit(data):
        claims = data["claims"]
        others = [c for c in claims
                  if norm(c["game"]) == norm(game) and same(c["language"], lang) and not same(c["who"], who)]
        if others and not args.anyway:
            result["taken"] = others
            return None
        for c in claims:
            if norm(c["game"]) == norm(game) and same(c["language"], lang) and same(c["who"], who):
                c["updated"], c["status"] = today(), args.status
                if args.note is not None:
                    c["note"] = args.note
                result["text"] = "Refreshed: " + game
                return "%s: still on %s (%s)" % (who, game, lang)
        claims.append({"game": game, "system": titles.system_of(game), "who": who, "language": lang,
                       "status": args.status, "since": today(), "updated": today(), "note": args.note or ""})
        claims.sort(key=lambda c: (norm(c["game"]), c["language"], c["who"].lower()))
        result["text"] = "Claimed: %s (%s) for %s" % (game, lang, who)
        return "%s claims %s (%s)" % (who, game, lang)

    change(args.local, edit)
    if "taken" in result:
        for c in result["taken"]:
            print("TAKEN: %s - %s" % (game, describe(c)))
        print("Not claimed. Ask them first; --anyway adds your name beside theirs.")
        return 1
    print(result["text"])
    patches = (released(titles) or {}).get(game, [])
    for owner, plang, version, url in patches:
        if same(plang, lang) and not same(owner, who):
            print("Note: %s has already released a patch in %s (v%s) %s" % (owner, plang, version, url))
    return 0


def cmd_done(args):
    titles = Titles()
    game = need_game(titles, args.game)
    who = me(args, needed=True)
    result = {}

    def edit(data):
        keep = [c for c in data["claims"] if not (
            norm(c["game"]) == norm(game) and same(c["who"], who) and (not args.lang or same(c["language"], language(args.lang))))]
        if len(keep) == len(data["claims"]):
            return None
        data["claims"] = keep
        result["removed"] = True
        return "%s is no longer on %s" % (who, game)

    change(args.local, edit)
    print("Removed your claim on %s." % game if result else "You had no claim on %s." % game)
    return 0


def cmd_wanted(args):
    titles = Titles()
    try:
        games = json.loads(http(WANTED)).get("games", [])
    except (OSError, ValueError, AttributeError) as e:
        die("cannot read the vote totals from %s (%s)" % (WANTED, e))
    data, _ = load(args.local)
    patches = released(titles)
    lang = language(args.lang)
    shown = 0
    for g in games if isinstance(games, list) else []:
        game, votes = (g.get("game"), g.get("votes")) if isinstance(g, dict) else (None, None)
        if not isinstance(game, str) or not isinstance(votes, int):
            continue
        claims = [c for c in data["claims"] if norm(c["game"]) == norm(game) and same(c["language"], lang)]
        done = [p for p in (patches or {}).get(game, []) if same(p[1], lang)]
        if claims:
            state = "TAKEN by " + ", ".join(c["who"] for c in claims)
        elif done:
            state = "ALREADY TRANSLATED by " + ", ".join(p[0] for p in done)
        else:
            state = "FREE"
        if args.free and state != "FREE":
            continue
        print("%5d  %s (%s): %s" % (votes, game, titles.system_of(game), state))
        shown += 1
        if shown >= args.top:
            break
    if not shown:
        print("Nobody has voted yet." if not games else "No game with votes is free.")
    if patches is None:
        print("(The patch catalog could not be read, so games already translated are not marked.)")
    print("People vote at " + VOTE_PAGE)
    return 0


def problems(data, titles):
    out = []
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return ['the file must be {"schema": 1, "claims": [...]}']
    for i, c in enumerate(data["claims"], 1):
        where = "claim %d (%s)" % (i, c.get("game", "?") if isinstance(c, dict) else "?")
        if not isinstance(c, dict):
            out.append(where + ": not an object")
            continue
        for field in FIELDS:
            if not isinstance(c.get(field), str):
                out.append("%s: \"%s\" is missing or not text" % (where, field))
        if isinstance(c.get("game"), str) and c["game"] not in titles.system:
            found, _ = titles.find(c["game"])
            out.append("%s: unknown game%s" % (where, "; use \"%s\"" % found if found else ""))
        if c.get("status") not in STATUSES:
            out.append("%s: status must be one of %s" % (where, ", ".join(STATUSES)))
        for field in ("since", "updated"):
            try:
                datetime.date.fromisoformat(c.get(field, ""))
            except (TypeError, ValueError):
                out.append("%s: \"%s\" must be a date like 2026-11-10" % (where, field))
    return out


def cmd_validate(args):
    try:
        data, _ = load(args.local, raw=True)
    except ValueError as e:
        print("claims.json is not valid JSON: %s" % e)
        return 1
    found = problems(data, Titles())
    for line in found:
        print(line)
    print("claims.json: %d claims, %s" % (len(data.get("claims", [])) if isinstance(data, dict) else 0,
                                          "%d problems" % len(found) if found else "ok"))
    return 1 if found else 0


def cell(text):
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").replace("<", "&lt;").strip()


def cmd_render(args):
    titles = Titles()
    data, _ = load(True)
    patches = released(titles)
    claims = sorted(data["claims"], key=lambda c: (norm(c["game"]), c["language"]))
    lines = []
    if not claims:
        lines.append("_Nobody has claimed a game yet._")
    else:
        lines += ["| Game | System | Who | Language | Status | Since | Last update | Patches already out | Note |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for c in claims:
            out = "?" if patches is None else ", ".join(
                "[%s %s v%s](%s)" % (cell(o), cell(l), cell(v), u) if u else "%s %s v%s" % (cell(o), cell(l), cell(v))
                for o, l, v, u in patches.get(c["game"], [])) or "none"
            stale = " ⚠️ %d days" % age(c) if age(c) > STALE_DAYS else ""
            lines.append("| **%s** | %s | [%s](https://github.com/%s) | %s | %s | %s | %s%s | %s | %s |" % (
                cell(c["game"]), cell(c["system"]), cell(c["who"]), cell(c["who"]), cell(c["language"]),
                cell(c["status"]), cell(c["since"]), cell(c["updated"]), stale, out, cell(c.get("note", ""))))
        lines.append("")
        lines.append("%d game%s in progress. ⚠️ = no update for more than %d days: ask before assuming it is "
                     "still being worked on." % (len(claims), "" if len(claims) == 1 else "s", STALE_DAYS))
    path = os.path.join(HERE, "README.md")
    with open(path, encoding="utf-8") as f:
        readme = f.read()
    start, end = "<!-- board:start -->", "<!-- board:end -->"
    if start not in readme or end not in readme:
        die("README.md has no board markers")
    head, rest = readme.split(start, 1)
    new = head + start + "\n" + "\n".join(lines) + "\n" + end + rest.split(end, 1)[1]
    if new != readme:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    print("README.md board: %d claims%s" % (len(claims), "" if new != readme else " (unchanged)"))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", action="store_true", help="use the claims.json beside this script, not GitHub")
    p.add_argument("--who", help="your GitHub name (default: the signed-in GitHub account, or WS_CLAIM_WHO)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("list", help="every game someone is working on")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=cmd_list)

    s = sub.add_parser("check", help="is this game free?")
    s.add_argument("game", help="title, part of a title, or the ROM's CRC32")
    s.add_argument("--lang", help="language you would translate into (default English)")
    s.set_defaults(run=cmd_check)

    s = sub.add_parser("claim", help="put your name on a game, or refresh your claim")
    s.add_argument("game")
    s.add_argument("--lang", help="default English")
    s.add_argument("--note", help="anything the others should know, for example a version or a link")
    s.add_argument("--status", choices=STATUSES, default="working")
    s.add_argument("--anyway", action="store_true", help="claim even though someone else has")
    s.set_defaults(run=cmd_claim)

    s = sub.add_parser("done", aliases=["drop"], help="take your name off a game")
    s.add_argument("game")
    s.add_argument("--lang", help="only this language (default: all of yours for the game)")
    s.set_defaults(run=cmd_done)

    s = sub.add_parser("wanted", help="the games people voted for most, and whether each is free")
    s.add_argument("--free", action="store_true", help="only games nobody has claimed or translated")
    s.add_argument("--top", type=int, default=20, help="how many to show (default 20)")
    s.add_argument("--lang", help="language you would translate into (default English)")
    s.set_defaults(run=cmd_wanted)

    s = sub.add_parser("validate", help="check claims.json")
    s.set_defaults(run=cmd_validate)
    s = sub.add_parser("render", help="redraw the board in README.md (run by GitHub Actions)")
    s.set_defaults(run=cmd_render)

    args = p.parse_args()
    sys.exit(args.run(args))


if __name__ == "__main__":
    main()
