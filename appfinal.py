"""Repo Battle Arena - zero-dependency backend (Python 3.9+).
Code computes stats + decides the winner. The LLM only narrates."""
import json, os, re, math, random, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GH, TOKEN = "https://api.github.com", os.getenv("GITHUB_TOKEN")
AKEY, AMODEL = os.getenv("ANTHROPIC_API_KEY"), os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
CACHE = {}

# ---------- GitHub ----------
def gh(path):
    if path in CACHE: return CACHE[path]
    h = {"Accept": "application/vnd.github+json", "User-Agent": "repo-battle-arena"}
    if TOKEN: h["Authorization"] = f"Bearer {TOKEN}"
    try:
        with urllib.request.urlopen(urllib.request.Request(GH + path, headers=h), timeout=15) as r:
            out = (json.loads(r.read() or "null"), r.headers.get("Link", ""))
    except urllib.error.HTTPError as e:
        if e.code == 404: raise ValueError("Repo not found: check the owner/name")
        if e.code in (403, 429): raise ValueError("GitHub rate limit hit - set GITHUB_TOKEN and restart")
        if e.code == 409: return ([], "")  # empty repo
        raise ValueError(f"GitHub error {e.code}")
    CACHE[path] = out
    return out

def parse_repo(s):
    m = re.search(r"(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s.strip())
    if not m: raise ValueError(f"Can't parse repo: {s!r}")
    return m.group(1), m.group(2)

# ---------- Stats ----------
TYPES = {"C++": ("Metal", "⚙️"), "C": ("Metal", "⚙️"), "Rust": ("Metal", "⚙️"),
         "Python": ("Snake", "🐍"), "JavaScript": ("Spark", "⚡"), "TypeScript": ("Spark", "⚡"),
         "Java": ("Brew", "☕"), "Kotlin": ("Brew", "☕"), "Go": ("Aqua", "🌊"),
         "Ruby": ("Gem", "💎"), "Swift": ("Gem", "💎")}
STRONG = {("Metal", "Spark"), ("Spark", "Aqua"), ("Aqua", "Metal"),
          ("Snake", "Brew"), ("Brew", "Gem"), ("Gem", "Snake")}

def norm(x, cap): return min(1.0, math.log10(x + 1) / math.log10(cap + 1))

def fetch_fighter(s):
    o, r = parse_repo(s)
    repo, _ = gh(f"/repos/{o}/{r}")
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body, link = gh(f"/repos/{o}/{r}/commits?since={since}&per_page=1")
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link)
    commits = int(m.group(1)) if m else len(body or [])
    q = urllib.parse.quote(f"repo:{o}/{r} type:issue state:closed")
    closed = gh(f"/search/issues?q={q}&per_page=1")[0].get("total_count", 0)
    pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
    days = max(0, (datetime.now(timezone.utc) - pushed).days)
    lang = repo.get("language") or "Unknown"
    typ, icon = TYPES.get(lang, ("Normal", "⭐"))
    return {"name": repo["full_name"], "avatar": repo["owner"]["avatar_url"], "language": lang,
            "type": typ, "icon": icon, "raw": {"stars": repo["stargazers_count"], "commits_30d": commits,
            "closed_issues": closed, "days_since_push": days},
            "hp": round(80 + 220 * norm(repo["stargazers_count"], 400_000)),
            "attack": round(10 + 90 * norm(commits, 3000)),
            "defense": round(10 + 90 * norm(closed, 50_000)),
            "speed": max(5, round(100 * 0.5 ** (days / 7)))}

# ---------- Battle engine (deterministic: same repos -> same fight) ----------
def mult(att, dfn):
    return 1.3 if (att["type"], dfn["type"]) in STRONG else 0.8 if (dfn["type"], att["type"]) in STRONG else 1.0

def simulate(a, b):
    rng, f = random.Random(a["name"] + "|" + b["name"]), [a, b]
    hp, events = [a["hp"], b["hp"]], []
    order = [0, 1] if a["speed"] >= b["speed"] else [1, 0]
    for rnd in range(1, 26):
        for i in order:
            j = 1 - i
            if hp[0] <= 0 or hp[1] <= 0: break
            crit, mu = rng.random() < 0.12, mult(f[i], f[j])
            dmg = max(3, round(f[i]["attack"] * 100 / (100 + f[j]["defense"]) * 0.6 * mu
                               * (1.5 if crit else 1) * rng.uniform(0.85, 1.15)))
            hp[j] = max(0, hp[j] - dmg)
            events.append({"round": rnd, "attacker": f[i]["name"], "defender": f[j]["name"], "damage": dmg,
                           "crit": crit, "effect": "super effective" if mu > 1 else "not very effective" if mu < 1 else "normal",
                           "hp": hp[:], "attack_stat": f[i]["attack"], "commits_30d": f[i]["raw"]["commits_30d"]})
        if min(hp) <= 0: break
    win = 0 if hp[0] > hp[1] else 1 if hp[1] > hp[0] else (0 if a["speed"] >= b["speed"] else 1)
    return events, f[win]["name"]

# ---------- Narration (LLM writes the story only) ----------
STYLES = {"pokemon": "an over-excited Pokemon announcer", "cricket": "a dramatic cricket commentator",
          "anime": "an anime battle narrator shouting power-up speeches", "shakespeare": "a Shakespearean bard",
          "wrestling": "a bombastic pro-wrestling ring announcer"}

def fallback(a, b, events, winner):
    lines = []
    for e in events:
        s = f"{e['attacker']} hits {e['defender']} for {e['damage']}"
        s += "!! CRITICAL!" if e["crit"] else "."
        if e["effect"] != "normal": s += f" It's {e['effect']}!"
        lines.append(s)
    return {"intro": f"{a['name']} vs {b['name']} - FIGHT!", "lines": lines, "outro": f"{winner} wins!", "source": "template"}

def narrate(a, b, events, winner, style):
    if not AKEY: return fallback(a, b, events, winner)
    facts = {"fighters": [{k: x[k] for k in ("name", "type", "language", "hp", "attack", "defense", "speed", "raw")} for x in (a, b)],
             "events": [{k: e[k] for k in ("attacker", "defender", "damage", "crit", "effect", "hp", "commits_30d")} for e in events],
             "winner": winner}
    prompt = (f"You are {STYLES.get(style, STYLES['pokemon'])} narrating a battle between two GitHub repos. "
              "Use ONLY the facts in the JSON; never change damage, HP or the winner. Mention real numbers (stars, commits) when fun. "
              f"Return ONLY JSON: {{\"intro\": str, \"lines\": [exactly {len(events)} strings, <=25 words each, one per event in order], \"outro\": str}}.\n"
              + json.dumps(facts))
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
            data=json.dumps({"model": AMODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": AKEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        txt = json.loads(urllib.request.urlopen(req, timeout=40).read())["content"][0]["text"]
        out = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        assert len(out["lines"]) == len(events)
        out["source"] = "ai"
        return out
    except Exception as ex:
        print("narration failed, using fallback:", ex)
        return fallback(a, b, events, winner)

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.end_headers(); self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self._send(200, (Path(__file__).parent / "static/index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/battle":
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                a, b = fetch_fighter(q.get("a", "")), fetch_fighter(q.get("b", ""))
                if a["name"] == b["name"]: raise ValueError("Pick two different repos")
                events, winner = simulate(a, b)
                return self._send(200, {"a": a, "b": b, "events": events, "winner": winner,
                                        "story": narrate(a, b, events, winner, q.get("style", "pokemon"))})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})
    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"Repo Battle Arena -> http://localhost:{port}  (AI narration: {'ON' if AKEY else 'OFF - template fallback'})")
    ThreadingHTTPServer(("", port), H).serve_forever()
