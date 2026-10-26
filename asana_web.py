#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "websockets>=13", "certifi", "browser-cookie3>=0.20"]
# ///
"""Asana web-app sync client (undocumented protocol, see PROTOCOL.md).

    ./asana_web.py cookies                             # which cookie source is used (prints names only)
    ./asana_web.py setup [URL] [--view ID]            # guided setup: parses IDs, writes .env
    ./asana_web.py bootstrap                           # release, bundle version, start_session, id candidates
    ./asana_web.py inbox --du ID [--view ID] [-n 50] [--dump frames.jsonl]
    ./asana_web.py archive --du ID THREAD_ID... [--yes]   # dry run without --yes

Cookies (global options, before the subcommand):
    --browser auto|firefox|chrome|chromium|brave|edge   (env ASANA_BROWSER; default auto = first
              browser with an Asana login, in that order). Chromium-family cookies are decrypted
              via the desktop keyring, so it may prompt to unlock it.
    --profile NAME|PATH   Firefox profile name/dir, or Chromium profile ("Default", "Profile 1",
              a profile dir or its Cookies file).
    --cookies FILE        (env ASANA_COOKIES) use a cookie file instead of a browser. Either:
              * Netscape cookies.txt, e.g. `yt-dlp --cookies-from-browser chrome --cookies FILE`
                or a "cookies.txt" browser extension, exported while logged in to app.asana.com;
              * or a Cookie header: DevTools → Network → any app.asana.com request →
                Copy as cURL, paste the `-H 'Cookie: …'` part (or just its value) into FILE.
              The file is a live login: `chmod 600` it and keep it out of git.
              Set ASANA_UA to that browser's User-Agent if Cloudflare complains.

Env: ASANA_UA (User-Agent override), ASANA_FF_CONTAINER (Firefox container id),
     ASANA_DOMAIN (workspace gid; default: the login's last_domain cookie).
     All ASANA_* vars may also live in a .env file (working dir, else script dir;
     see README) — the real environment wins.
"""

import argparse
import asyncio
import configparser
import contextlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
try:
    import readline  # noqa: F401  puts input() in raw mode: pasted lines aren't truncated at MAX_CANON
except ImportError:
    pass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import certifi
import httpx
import websockets

APP = "https://app.asana.com"
WS_HOST = "cdn.sync.app.asana.com"
MAC = sys.platform == "darwin"
# UA platform tokens differ per browser; match the real OS so Cloudflare's cf_clearance stays valid
UA_FF_PLATFORM = "Macintosh; Intel Mac OS X 10.15" if MAC else "X11; Linux x86_64"
UA_CHROME_PLATFORM = "Macintosh; Intel Mac OS X 10_15_7" if MAC else "X11; Linux x86_64"
UA = f"Mozilla/5.0 ({UA_FF_PLATFORM}; rv:140.0) Gecko/20100101 Firefox/140.0"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "asana-inbox"



class AsanaError(RuntimeError):
    """Expected failure with a human-readable message (auth, HTTP, protocol)."""


class NoWorkspaceError(AsanaError):
    """The login names no workspace — recoverable via the inbox URL."""


# keys load_env_file() copied into the environment from a .env file (as opposed to
# values genuinely exported in the shell — those keep overriding .env on every run)
env_file_keys: set[str] = set()


def load_env_file() -> None:
    """Set ASANA_* environment variables from a .env file: the current directory first,
    else one next to the script. KEY=VALUE lines; `#` comments (own line or trailing),
    optional `export ` and quotes. Only ASANA_* keys are honored; variables already in
    the environment keep their value; empty values count as unset; later lines win."""
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        f = base / ".env"
        if not f.is_file():
            continue
        preset = set(os.environ)  # real environment wins over the file
        try:
            text = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            raise AsanaError(f"cannot read {f}: {e}") from e
        parsed: dict[str, str] = {}  # parsed fully first: a bad line sets nothing
        skipped = nul = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if value.startswith("#"):  # e.g. "ASANA_DU= # get it from the URL" — empty value
                value = ""
            elif value[:1] in ('"', "'"):  # quoted: take the inside; ignore any trailing comment
                end = value.find(value[0], 1)
                if end < 0:
                    raise AsanaError(f"{f}: unterminated quote in value of {key!r}")
                value = value[1:end]
            else:  # unquoted: drop a trailing " # comment"
                value = re.split(r"\s#", value, 1)[0].rstrip()
            if not key:
                continue
            if "\0" in key + value:  # UTF-16 and other binary encodings decode with NULs
                nul += 1
                continue
            if not key.startswith("ASANA_"):
                skipped += 1
            else:
                parsed[key] = value
        if nul:
            print(f"warning: {f}: {nul} line(s) with NUL bytes — UTF-16? save the file as UTF-8",
                  file=sys.stderr)
        if skipped:
            print(f"warning: {f}: skipped {skipped} non-ASANA_* key(s)", file=sys.stderr)
        for key, value in parsed.items():
            if value and key not in preset:  # empty value (e.g. "KEY= # TODO") counts as unset
                os.environ[key] = value
                env_file_keys.add(key)  # came from a file, not a real shell export
        return  # first .env found wins; don't also load the script-dir one


def parse_du(text: str) -> tuple[str, str] | None:
    """(domain user id, workspace id) from an inbox URL …/1/<workspace>/inbox/<du>/…,
    or (du, "") from a bare number."""
    text = text.strip().strip("'\"")
    if m := re.search(r"/1/(\d+)/inbox/(\d+)", text):
        return m.group(2), m.group(1)
    if text.isdigit():
        return text, ""
    return None


def parse_view(text: str) -> tuple[str, set[str]] | None:
    """(objectViewId, {ids claiming to be the domain user}) from the WS `sub` message
    JSON (or a bare number). The ids come from db_object_id / currentDomainUserId."""
    text = text.strip()
    if m := re.search(r'"?objectViewId"?\s*:\s*"?(\d+)"?', text):
        du_ids = set(re.findall(r'"?(?:db_object_id|currentDomainUserId)"?\s*:\s*"?(\d+)"?', text))
        return m.group(1), du_ids
    bare = text.strip("'\"")  # an id copied with its quotes, e.g. "1650000000000000"
    if bare.isdigit():
        return bare, set()
    return None


def du_from_session(ss: dict) -> str | None:
    """Domain user id (digits) from a start_session payload — the same fields the
    web app reads. Defensive: any unexpected shape just means "not discovered"."""
    if not isinstance(ss, dict):
        return None
    for path in ("app_data", "luna_app_data"):
        du = ss.get(path).get("domain_user_id") if isinstance(ss.get(path), dict) else None
        if isinstance(du, (str, int)) and str(du).isdigit():
            return str(du)
    route = ss.get("app_data").get("initial_route") if isinstance(ss.get("app_data"), dict) else {}
    route = route if isinstance(route, dict) else {}
    du = route.get("domainUser").get("id") if isinstance(route.get("domainUser"), dict) else None
    return str(du) if isinstance(du, (str, int)) and str(du).isdigit() else None


def view_ids_from_docs(docs: dict[tuple[str, str], dict]) -> tuple[str, list[tuple[str | None, str]]] | None:
    """(default view id, [(name, id), …]) from a store fed by the FilterRow projection:
    the Inbox DbObject carries defaultObjectView; its view children carry the names."""
    inbox = next((id_ for (coll, id_), d in docs.items()
                  if coll == "DbObject" and d.get("typeName") == "Inbox"), None)
    if inbox is None:
        return None
    views = [(d.get("name"), id_) for (coll, id_), d in sorted(docs.items())
             if coll == "DbObject" and d.get("object") == inbox]
    default = docs.get(("DbObject", inbox), {}).get("defaultObjectView")
    if default is not None and not str(default).isdigit():
        default = None  # a shape surprise is treated as "not found", not trusted
    if default is None:
        cq = next((d.get("results") for (coll, id_), d in docs.items()
                   if coll == "ConfirmedQuery" and id_.startswith(f"MultiViewable.objectViews|{inbox}|")
                   and d.get("results")), None)
        if cq:
            cq = [str(i) for i in cq if str(i).isdigit()]
            if cq:
                names = {id_: name for name, id_ in views}
                views = [(names.get(i), i) for i in cq]
                default = cq[0]
    if default is None:
        return None
    return str(default), views


def update_env_file(path: Path, values: dict[str, str]) -> bool:
    """Set KEY=VALUE lines in an existing .env, keeping comments and other keys as-is
    (later duplicates dropped). Returns True if the file already existed.
    Writes via a temp file + rename so a failure can't truncate the old one."""
    bad = [k for k, v in values.items() if "\n" in v or "\r" in v]
    if bad:  # would inject extra lines; ids and cookies never contain newlines
        raise AsanaError(f"refusing to write multi-line value(s) for {bad}")
    existed = path.exists()
    path = path.resolve()  # write through symlinks (dotfiles managers), not over them
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if existed else []
        out: list[str] = []
        seen: set[str] = set()
        for line in lines:
            key = line.removeprefix("export ").partition("=")[0].strip()
            if key in values:
                if key in seen:
                    continue  # keep the first (updated) occurrence, drop later duplicates
                out.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in values.items() if k not in seen]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        if existed:
            os.chmod(tmp, path.stat().st_mode)  # keep e.g. a pre-existing 0600
        tmp.replace(path)
    except UnicodeDecodeError as e:
        raise AsanaError(f"{path} is not UTF-8 ({e}); re-save it as UTF-8 first") from e
    except OSError as e:
        raise AsanaError(f"cannot write {path}: {e}") from e
    return existed


# ---------------------------------------------------------------- Firefox cookies

FIREFOX_ROOTS = [
    Path.home() / "Library/Application Support/Firefox",  # macOS
    Path.home() / ".mozilla/firefox",
    Path.home() / ".config/mozilla/firefox",
    Path.home() / "snap/firefox/common/.mozilla/firefox",
    Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
]


def find_profile(spec: str | None) -> Path:
    if spec and Path(spec).expanduser().is_dir():
        return Path(spec).expanduser()
    for root in FIREFOX_ROOTS:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        profiles = {s: cp[s] for s in cp.sections() if s.startswith("Profile")}

        def resolve(sec) -> Path:
            p = Path(sec["Path"])
            return root / p if sec.get("IsRelative", "1") == "1" else p

        if spec:
            for sec in profiles.values():
                if spec in (sec.get("Name"), sec.get("Path")):
                    return resolve(sec)
            continue
        # Default: the profile the install section points at, else Default=1, else first.
        for s in cp.sections():
            if s.startswith("Install") and "Default" in cp[s]:
                return root / cp[s]["Default"]
        for sec in profiles.values():
            if sec.get("Default") == "1":
                return resolve(sec)
        if profiles:
            return resolve(next(iter(profiles.values())))
    raise AsanaError(f"No Firefox profile found (spec={spec!r}); pass --profile PATH")


def cookie_rows(profile: Path) -> list[dict]:
    """All unexpired *.asana.com cookies. Copies the DB since Firefox locks it."""
    src = profile / "cookies.sqlite"
    if not src.exists():
        raise AsanaError(f"{src} not found")
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            if (p := Path(f"{src}{suffix}")).exists():
                shutil.copy2(p, Path(tmp) / f"cookies.sqlite{suffix}")
        db = sqlite3.connect(Path(tmp) / "cookies.sqlite")
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT host, name, value, originAttributes, lastAccessed, expiry FROM moz_cookies"
                " WHERE (host = 'asana.com' OR host LIKE '%.asana.com') AND expiry > ?"
                " ORDER BY lastAccessed",
                (int(time.time() * 1000),),  # expiry is ms in recent Firefox, s in older; ms bound is safe-ish
            ).fetchall()
            if not rows:  # older Firefox: expiry in seconds
                rows = db.execute(
                    "SELECT host, name, value, originAttributes, lastAccessed, expiry FROM moz_cookies"
                    " WHERE (host = 'asana.com' OR host LIKE '%.asana.com') AND expiry > ?"
                    " ORDER BY lastAccessed",
                    (int(time.time()),),
                ).fetchall()
        finally:
            db.close()
    return [dict(r) for r in rows]


def wanted_origin() -> str:
    """Default jar is originAttributes=''; ASANA_FF_CONTAINER=N picks a Multi-Account Container."""
    c = os.environ.get("ASANA_FF_CONTAINER")
    return f"^userContextId={c}" if c else ""


def load_cookies(profile: Path) -> list[tuple[str, str, str]]:
    """(host, name, value) from the chosen jar, most recently used value per (host, name)."""
    jar: dict[tuple[str, str], str] = {}
    for r in cookie_rows(profile):  # ordered by lastAccessed, so later wins
        if r["originAttributes"] == wanted_origin():
            jar[(r["host"], r["name"])] = r["value"]
    return [(h, n, v) for (h, n), v in jar.items()]


def firefox_user_agent(profile: Path) -> str:
    """Match the profile's Firefox version so Cloudflare's cf_clearance stays valid."""
    if ua := os.environ.get("ASANA_UA"):
        return ua
    ver = "140"
    ini = profile / "compatibility.ini"
    try:
        m = re.search(r"LastVersion=(\d+)\.", ini.read_text()) if ini.exists() else None
        if m:
            ver = m.group(1)
    except OSError as e:  # unreadable file: skip this browser in the auto-detect loop
        raise AsanaError(f"cannot read {ini}: {e}") from e
    return f"Mozilla/5.0 ({UA_FF_PLATFORM}; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"


def cookie_header(cookies: list[tuple[str, str, str]], host: str) -> str:
    def matches(chost: str) -> bool:
        if chost.startswith("."):
            return host == chost[1:] or host.endswith(chost)
        return host == chost

    jar = {name: value for chost, name, value in cookies if matches(chost)}
    return "; ".join(f"{k}={v}" for k, v in jar.items())


# ---------------------------------------------------------------- other cookie sources

Cookies = list[tuple[str, str, str]]  # (host, name, value)

# browser_cookie3 loader name, config dir under ~/.config (Linux) or
# ~/Library/Application Support (macOS); the dir also holds the UA's "Last Version"
CHROMIUMS = {
    "chrome": ("chrome", "google-chrome", "Google/Chrome"),
    "chromium": ("chromium", "chromium", "Chromium"),
    "brave": ("brave", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser"),
    "edge": ("edge", "microsoft-edge", "Microsoft Edge"),
}
BROWSERS = ["firefox", *CHROMIUMS]


def chromium_confdir(confdir_linux: str, confdir_mac: str) -> Path:
    base = Path.home() / ("Library/Application Support" if MAC else ".config")
    return base / (confdir_mac if MAC else confdir_linux)


def chromium_cookies(browser: str, profile: str | None) -> Cookies:
    """Decrypts via the desktop keyring (GNOME Keyring / KWallet) using browser_cookie3."""
    try:
        import browser_cookie3
    except ImportError as e:
        raise AsanaError("browser_cookie3 not installed (run via `uv run`)") from e
    loader, confdir_linux, confdir_mac = CHROMIUMS[browser]
    confdir = chromium_confdir(confdir_linux, confdir_mac)
    if not profile and not confdir.is_dir():
        raise AsanaError(f"not installed (no {confdir})")
    cookie_file = None
    if profile:  # profile dir name ("Default", "Profile 1"), profile dir path, or Cookies file
        p = Path(profile).expanduser()
        if not p.exists():
            p = confdir / profile
        cookie_file = next((str(c) for c in (p, p / "Network/Cookies", p / "Cookies") if c.is_file()), None)
        if not cookie_file:
            raise AsanaError(f"no {browser} Cookies file under {p}")
    try:
        jar = getattr(browser_cookie3, loader)(cookie_file=cookie_file, domain_name="asana.com")
    except Exception as e:  # BrowserCookieError, keyring errors, locked DB, ...
        raise AsanaError(f"{type(e).__name__}: {e}") from e
    return [(c.domain, c.name, c.value) for c in jar]


def chromium_user_agent(browser: str) -> str:
    _, confdir_linux, confdir_mac = CHROMIUMS[browser]
    f = chromium_confdir(confdir_linux, confdir_mac) / "Last Version"
    major = "140"
    try:
        m = re.match(r"\d+", f.read_text().strip()) if f.exists() else None
        if m:  # empty/truncated file (Chrome rewrites it during updates) falls back
            major = m.group(0)
    except OSError as e:  # unreadable file: skip this browser in the auto-detect loop
        raise AsanaError(f"cannot read {f}: {e}") from e
    return (f"Mozilla/5.0 ({UA_CHROME_PLATFORM}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def file_cookies(path: str) -> Cookies:
    """Netscape cookies.txt (browser extensions, `yt-dlp --cookies-from-browser X --cookies F`),
    or a raw Cookie header ("name=v; name2=v2", optionally prefixed by `Cookie:` or a pasted
    `-H 'Cookie: …'` line from DevTools' Copy as cURL)."""
    text = Path(path).expanduser().read_text()
    rows = [l.split("\t") for l in text.splitlines() if l.count("\t") >= 6]
    if rows:
        out = []
        for r in rows:
            host = r[0].removeprefix("#HttpOnly_")
            if host.endswith("asana.com"):
                out.append((host, r[5], r[6].rstrip("\r\n")))
        return out
    m = re.search(r"cookie:\s*([^'\"\n]+)", text, re.I)
    header = (m.group(1) if m else text).strip()
    return [(".asana.com", k.strip(), v.strip())
            for k, v in (p.split("=", 1) for p in header.split(";") if "=" in p)]


def has_login(cookies: Cookies) -> bool:
    return "ticket=" in cookie_header(cookies, "app.asana.com")


def resolve_cookies(browser: str | None = None, profile: str | None = None,
                    cookie_file: str | None = None, log=None) -> tuple[Cookies, str, str]:
    """Return (cookies, user agent, description). browser: one of BROWSERS, or "auto"/None
    to try them in order and take the first with an Asana login. ASANA_UA overrides the UA."""
    browser = browser or os.environ.get("ASANA_BROWSER") or "auto"
    cookie_file = cookie_file or os.environ.get("ASANA_COOKIES")
    override = os.environ.get("ASANA_UA")
    if cookie_file:
        return file_cookies(cookie_file), override or UA, f"file {cookie_file}"
    candidates = BROWSERS if browser == "auto" else [browser]
    errors = []
    for b in candidates:
        try:
            if b == "firefox":
                prof = find_profile(profile)
                cookies, ua, desc = load_cookies(prof), firefox_user_agent(prof), f"firefox {prof}"
            elif b in CHROMIUMS:
                cookies, ua, desc = chromium_cookies(b, profile), chromium_user_agent(b), b
            else:
                raise AsanaError(f"unknown browser {b!r}; choose from {', '.join(BROWSERS)}")
        except AsanaError as e:
            errors.append(f"{b}: {e}")
            continue
        if has_login(cookies):
            return cookies, override or ua, desc
        errors.append(f"{b}: no Asana login cookies")
    raise AsanaError("no usable Asana cookies found:\n  " + "\n  ".join(errors)
                     + "\nLog in to app.asana.com, or pass --cookies FILE (see --help).")


# ---------------------------------------------------------------- bootstrap


def domain_from_route_hint(value: str | None) -> str | None:
    """Workspace gid from the route_hint cookie ("1/<workspace>//<locale>", URL-encoded).
    Fresh Chrome logins no longer set last_domain; route_hint still carries the
    workspace of the last-visited domain."""
    if not value:
        return None
    m = re.match(r"\d+/(\d{5,})//", urllib.parse.unquote(value))
    return m.group(1) if m else None


class Session:
    def __init__(self, browser: str | None = None, profile: str | None = None,
                 cookie_file: str | None = None, domain: str | None = None,
                 use_env: bool = True):
        global UA
        cookies, UA, self.source = resolve_cookies(browser, profile, cookie_file)
        self.cookies = cookies
        jar = dict(p.split("=", 1) for p in cookie_header(cookies, "app.asana.com").split("; ") if "=" in p)
        missing = [k for k in ("ticket", "user") if k not in jar]
        if missing:
            raise AsanaError(f"cookies from {self.source} missing {missing}; log in to Asana again")
        self.ticket = jar["ticket"]          # CSRF token, echoed as X-Asana-Ticket
        self.user_id = jar["user"]           # user gid
        resolved = (domain or (os.environ.get("ASANA_DOMAIN") if use_env else None)
                    or jar.get("last_domain")
                    or domain_from_route_hint(jar.get("route_hint")))
        if not resolved:
            raise NoWorkspaceError(f"cookies from {self.source} name no workspace: no last_domain "
                             "cookie and no parseable route_hint; pass the workspace from the "
                             "inbox URL, or set ASANA_DOMAIN (see README)")
        self.domain = resolved  # workspace gid == shard_id
        self.http = httpx.Client(
            headers={
                "User-Agent": UA,
                "Cookie": cookie_header(cookies, "app.asana.com"),
                "Accept-Language": "en-US,en;q=0.5",
            },
            follow_redirects=True,
            timeout=60,
        )

    def get(self, url: str, accept: str = "*/*", **headers) -> httpx.Response:
        r = self.http.get(url, headers={"Accept": accept, **headers})
        if r.is_error:
            interesting = ("server", "cf-ray", "cf-mitigated", "content-type", "x-asana-",
                           "www-authenticate", "location", "set-cookie")
            def show(k, v):  # don't print cookie values
                return v.split("=", 1)[0] + "=…" if k.lower() == "set-cookie" else v[:120]
            hdrs = "\n".join(f"    {k}: {show(k, v)}" for k, v in r.headers.multi_items()
                             if any(k.lower().startswith(p) for p in interesting))
            body = re.sub(r"\s+", " ", r.text)[:600]
            raise AsanaError(f"{r.status_code} {r.reason_phrase} for {url}\n"
                             f"  headers:\n{hdrs}\n  body: {body}")
        return r

    def close(self) -> None:
        """Release the HTTP connection pool (idempotent; the websocket is
        SyncClient's to close)."""
        self.http.close()

    def start_session(self, release: str, path: str | None = None) -> dict:
        path = path or f"/1/{self.domain}/home"
        params = {
            "path": path, "query_string": "", "user_id": self.user_id,
            "optimistic_pageload": "true", "locale": "en", "release_revision": release,
            "shard_id": self.domain, "guid": str(uuid.uuid4()),
        }
        r = self.get(f"{APP}/app/asana/-/start_session?{urlencode(params)}", "*/*",
                     **{"X-Asana-Ticket": self.ticket, "Referer": f"{APP}{path}",
                        "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Dest": "empty"})
        text = r.text
        try:
            return json.loads(text[text.index("{"):])  # tolerate anti-JSON-hijack prefixes
        except ValueError:
            raise AsanaError(f"start_session body isn't JSON ({len(text)} bytes) — "
                             "logged out, blocked, or a consent page?") from None

    def release_info(self) -> tuple[str, str]:
        """Return (release_revision, bundle_url) scraped from an app HTML page."""
        for url in (f"{APP}/1/{self.domain}/home", f"{APP}/"):
            html = self.get(url, "text/html,application/xhtml+xml,*/*;q=0.8",
                            **{"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}).text
            rev = re.search(r"release_revision=(\d{8}_\d{6}_[0-9a-f]+)", html)
            bundle = re.search(r"https://[^\"'\s]+/build/bundles/[0-9a-f]+/apps/asana/bundle\.js", html)
            if rev and bundle:
                return rev.group(1), bundle.group(0)
        raise RuntimeError("release_revision/bundle not found in app HTML (logged out?)")

    def bundle_version(self, bundle_url: str, stall: float = 60,
                        deadline: float | None = None) -> str:
        """First projectionBundleVersion in the bundle JS. The ~46 MB download uses
        `stall` as the read timeout — no bytes for that long aborts — so a
        slow-but-flowing link always finishes (and caches by bundle commit). Setup,
        which must fall back to manual input, additionally passes an absolute
        monotonic `deadline` capping the total wait."""
        commit = re.search(r"/bundles/([0-9a-f]+)/", bundle_url).group(1)
        cached = CACHE / f"bundle-version-{commit}"
        if cached.exists():
            return cached.read_text().strip()
        rx = re.compile(rb'\{version:"([^"]+)",clientQueries:')
        tail = b""
        with httpx.stream("GET", bundle_url, headers={"User-Agent": UA},
                          timeout=httpx.Timeout(120, read=stall)) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(64 << 10):
                buf = tail + chunk
                if m := rx.search(buf):
                    CACHE.mkdir(parents=True, exist_ok=True)
                    cached.write_text(m.group(1).decode())
                    return m.group(1).decode()
                tail = buf[-256:]
                if deadline is not None and time.monotonic() > deadline:
                    raise AsanaError("bundle download too slow — check the connection,"
                                     " or paste the view id manually")
        raise RuntimeError("projectionBundleVersion not found in bundle")


def find_keys(obj, pattern: re.Pattern, path="") -> list[tuple[str, object]]:
    """Walk JSON looking for keys matching pattern; returns (path, scalar-ish value)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if pattern.search(k) and not isinstance(v, (dict, list)):
                out.append((p, v))
            out += find_keys(v, pattern, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            out += find_keys(v, pattern, f"{path}[{i}]")
    return out


# ---------------------------------------------------------------- websocket


class Store:
    """Merged view of added/changed docs, keyed by (collection, id)."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    @staticmethod
    def key(msg) -> tuple[str, str]:
        i = msg.get("id")
        return msg.get("collection"), i if isinstance(i, str) else json.dumps(i)

    def apply(self, msg: dict) -> None:
        m = msg.get("msg")
        if m in ("added", "changed"):
            doc = self.docs.setdefault(self.key(msg), {})
            doc.update(msg.get("fields") or {})
            for f in msg.get("cleared") or []:
                doc.pop(f, None)
        elif m == "removed":
            self.docs.pop(self.key(msg), None)
        elif m == "batch_removed":
            for i in msg.get("ids") or []:
                self.docs.pop((msg.get("collection"), i if isinstance(i, str) else json.dumps(i)), None)

    def get(self, collection: str, id_: str) -> dict:
        return self.docs.get((collection, id_), {})


class SyncClient:
    def __init__(self, sess: Session, du: str, shard: str, dump: Path | None = None):
        self.sess, self.du, self.shard = sess, du, shard
        self.dump = dump.open("a") if dump else None
        self.store = Store()
        self.next_id = 0
        self.ws = None

    async def connect(self, session_id: str, bundle_version: str, release: str) -> None:
        url = (
            f"wss://{WS_HOST}/socket?version=24&userId={self.du}&shardId={self.shard}"
            "&shardIdType=1&userLocale=en&allowedDomainsInfo=%7B%7D"
        )
        headers = {
            "Cookie": cookie_header(self.sess.cookies, WS_HOST),
            "Origin": APP,
            "User-Agent": UA,
        }
        # uv's standalone Python doesn't see system CAs; use certifi like httpx does.
        ctx = ssl.create_default_context(cafile=certifi.where())
        self.session_id = session_id
        self.ws = await websockets.connect(url, additional_headers=headers, max_size=None, ssl=ctx)
        await self.send({
            "msg": "connect", "session": session_id, "projectionBundleVersion": bundle_version,
            "releaseRevision": release, "connectionCount": 1,
        })
        await self.wait_for(lambda m: m.get("msg") == "connected", 30)

    async def send(self, msg: dict) -> None:
        self._log(">", msg)
        await self.ws.send(json.dumps(msg))

    def _log(self, direction: str, msg) -> None:
        if self.dump:
            self.dump.write(json.dumps({"t": time.time(), "dir": direction, "msg": msg}) + "\n")

    async def wait_for(self, pred, timeout: float) -> dict:
        """Process frames until pred(msg) is true; raises TimeoutError."""
        async with asyncio.timeout(timeout):
            while True:
                raw = await self.ws.recv()
                frame = json.loads(raw)
                self._log("<", frame)
                for msg in frame if isinstance(frame, list) else [frame]:
                    kind = msg.get("msg")
                    if kind == "ping":
                        await self.send({"msg": "pong", **({"id": msg["id"]} if "id" in msg else {})})
                    elif kind in ("error", "nosub", "failed"):
                        raise RuntimeError(f"server: {json.dumps(msg)[:500]}")
                    self.store.apply(msg)
                    if pred(msg):
                        return msg

    async def mutate(self, method: str, endpoint_data: dict, timeout: float = 30, **extra) -> dict:
        self.next_id += 1
        mid = str(self.next_id)
        await self.send({
            "msg": "mutation", "method": method, "id": mid,
            "params": {"http_method": "post", **extra, "endpoint_data": {
                **endpoint_data, "__app_name": "asana", "__session_id": self.session_id}},
        })
        return await self.wait_for(lambda m: m.get("msg") == "committed" and str(m.get("id")) == mid,
                                   timeout)

    async def archive_threads(self, threads: list[dict]) -> None:
        """Mark the notifications of the given threads/items read, which archives them."""
        notifs = [n for t in threads for n in t["notifications"]]
        if notifs:
            await self.mutate("/notification_set_unread_multi", {
                "domain": self.shard, "notifications": notifs,
                "timestamp": int(time.time() * 1000), "unread": False,
            }, expensive_mutation=True)

    async def load_inbox(self, n: int, view_id: int | None, log=None) -> int:
        """n > 0: load n threads. n == 0: load everything by doubling the request until the
        server returns fewer threads than asked. Returns the number of threads loaded."""
        log = log or (lambda s: None)
        if n > 0:
            await self.sub_inbox(n, view_id)
            return len(self.inbox_threads())
        # Each step reloads from scratch, so start big and grow fast.
        n, got = int(os.environ.get("ASANA_FIRST_BATCH", 250)), 0
        while True:
            log(f"loading up to {n} threads…")
            t0 = time.monotonic()
            try:
                await self.sub_inbox(n, view_id)
            except AsanaError as e:
                if not got:
                    raise
                # Earlier, smaller sub's docs are still in the store; keep them.
                log(f"{e}; showing the {got} threads loaded so far")
                return got
            prev, got = got, len(self.inbox_threads())
            log(f"asked {n} threads, got {got} ({time.monotonic() - t0:.1f}s)")
            if got < n:
                return got
            if got <= prev:  # server stopped growing: probably a cap on numAdditionalThreads
                raise AsanaError(f"inbox stuck at {got} threads; server may cap numAdditionalThreads")
            # Previous sub stays open on purpose: unsubscribing could make the server remove its
            # docs, losing the fallback if the next, bigger sub times out. Connection is short-lived.
            n *= 4

    async def sub(self, name: str, params: dict, timeout: float = 30) -> None:
        self.next_id += 1
        sid = str(self.next_id)
        await self.send({
            "msg": "sub", "id": sid, "name": name, "params": params,
            "metadata": {"idleTimeMs": 0, "isReconnect": False, "isDuringPageLoad": False,
                         "loadsEssentialDataForPageLoad": True},
            "behavior": {"deferInvalidationReloadsWhenHidden": True, "shouldDebugTrace": False},
        })
        # Server also reports its own internal subs (id "-1"); wait for ours to be ready.
        try:
            await self.wait_for(lambda m: m.get("msg") == "subscription_state" and str(m.get("id")) == sid
                                and m.get("state") == "ready", timeout)
        except TimeoutError:
            raise AsanaError(f"sub {name} not ready after {timeout}s") from None

    async def sub_inbox(self, num_threads: int = 5, view_id: int | None = None) -> None:
        """Params copied verbatim from the web app (2026-10-15); not yet minimised."""
        big = 8640000000000000

        def rng(n):
            return {"type": "RelativeRange", "lower_delta": 0, "lower_bound": {"type": "SpecialRank.Min"},
                    "upper_bound": {"type": "SpecialRank.Min"}, "upper_delta": n}

        multifilter = {"multifilterList": [],
                       "inboxSortOverride": [{"sortType": "priority", "value": "recency"}]}
        if view_id:
            multifilter = {"objectViewId": view_id, **multifilter}
        await self.sub("CurrentDomainUserProjectionInbox", {
            "db_object_id": self.du,
            "loadTaskWpl": True,
            "loadNotificationsVsActivityRelevanceFields": False,
            "categoriesIsArchived": False,
            "dataPolicyParams": {"type": "not_applicable"},
            "dataPolicyForCurrentDomainUserParams": {"type": "current_actor"},
            "daysUntilUnblockRetrialsForWorkManagementParams": {"offering": "WorkManagement"},
            "daysUntilUnblockRetrialsForClientManagementParams": {"offering": "client-management"},
            "sinceTimestamp": 0,
            "recentCategoryActivitiesRange": rng(3),
            "reviewableSubmittersForActualTimeInputParams": {"numResults": 1},
            "loadTaskTimeEntriesClearDismissibleKey": False,
            "taskTimeEntriesClearDismissibleKey": {},
            "daysUntilUnblockRetrialsForOfferingParams": {"offering": "timesheets-and-budgets"},
            "useDataBoundary": False,
            "inboxThreadsInfo": {
                "showUnread": True, "inboxTab": "activity",
                "upperTimestampBound": big, "lowerTimestampBound": big,
                "numAdditionalThreads": num_threads, "listThreadSize": 3,
                "loadNotificationsVsActivityRelevanceFields": False,
                "multifilterParams": multifilter,
            },
            "notificationThreadViewedStateParam": {"inboxTab": "activity"},
            "currentDomainUserId": int(self.du),
            "paginatedFullTeamMembershipsRange": rng(1),
            "paginatedTeamMembershipsRange": rng(1),
            "paginatedNotificationThreadStar": rng(1),
            "storyCapabilities": {},
            "paginatedRecentProjectActivitiesRange": rng(1),
            "skipCFOptions": False,
            "teamId": -1,
            "shouldLoadComputedMembership": False,
            "domainUserComputedMembershipForShareableParams": {},
            "projectMembershipsNumResults": {"numResults": 50},
            "paginatedTeamMembershipsRangeForCollaboratorRecommendation": rng(2),
            "shareable": {"shareableId": -1},
            "teamComputedMembershipForShareableParams": {"shareableId": -1},
        }, timeout=max(90.0, 0.15 * num_threads))

    async def discover_view(self) -> tuple[str, list[tuple[str | None, str]]] | None:
        """(default view id, [(name, id), …]) for the current user's inbox, via the
        FilterRow projection the web app uses for its view tabs — no DevTools needed.
        None when the sub fails or the store yields no Inbox object (fall back to manual)."""
        try:
            await self.sub("CurrentDomainUserProjectionInboxFilterRow", {
                "db_object_id": self.du,
                "dataPolicyParams": {"type": "not_applicable"},
                "dataPolicyForCurrentDomainUserParams": {"type": "current_actor"},
                "daysUntilUnblockRetrialsForWorkManagementParams": {"offering": "WorkManagement"},
                "daysUntilUnblockRetrialsForClientManagementParams": {"offering": "client-management"},
                "useDataBoundary": False,
                "hasMachineLearningProductFeatureEnabledParams": {
                    "featureKey": "metadata_ml_features", "featureFlag": "mc_priority_inbox",
                    "logEnrollment": False},
                "skipCFOptions": False,
            }, timeout=60)
        except (AsanaError, RuntimeError, TimeoutError) as e:
            print(f"   view discovery failed ({str(e)[:120]})", file=sys.stderr)
            return None
        return view_ids_from_docs(self.store.docs)

    def inbox_threads(self) -> list[dict]:
        # One value per inbox sub (keyed by its params); after load_all re-subs, take the longest.
        order = []
        for (coll, id_), doc in self.store.docs.items():
            if coll == "ServerComputedValue" and "currentDomainUserInboxThreads" in id_:
                if len(doc.get("value") or []) > len(order):
                    order = doc["value"]
        threads = []
        for tid in order:
            t = self.store.get("ComputedObject", tid)
            notifs = [self.store.get("DbObject", n) for n in t.get("notifications") or []]
            task_id = next((n.get("storyParent") for n in notifs if n.get("storyParent")), None)
            obj = t.get("threadObject") or ""
            if self.store.get("DbObject", obj).get("typeName") == "Pot":
                project_ids = [obj]
            else:
                key = json.dumps(["taskProjectsContainingThis", task_id or obj, {}], separators=(",", ":"))
                project_ids = self.store.get("ServerComputedValue", key).get("value") or []
            projects = [self.store.get("DbObject", p).get("name") or p for p in project_ids]
            threads.append({
                "thread": t.get("notificationThread") or tid,
                "projects": projects,
                "id": tid,
                "type": t.get("threadTypeName"),
                "object": t.get("threadObject"),
                "object_type": self.store.get("DbObject", t.get("threadObject") or "").get("typeName"),
                "task": self.store.get("DbObject", task_id or "").get("name"),
                "notifications": t.get("notifications") or [],
                "unread": sum(1 for n in notifs if n.get("unread")),
                "archived": t.get("isArchived"),
            })
        return threads

    def object_url(self, obj: dict, obj_id: str | None, project_id: str | None) -> str:
        """Link for a task/conversation, as seen in Asana rich text (PREFERRED_PATH):
        /1/<domain>/project/<project>/task/<task>. Conversations use the legacy
        /0/<project>/<id> form (redirected by Asana; untested). No object: the inbox."""
        base = f"{APP}/1/{self.shard}"
        kind = obj.get("typeName")
        if obj_id and kind == "Task":
            return f"{base}/project/{project_id}/task/{obj_id}" if project_id else f"{base}/task/{obj_id}"
        if obj_id and kind == "Pot":
            return f"{base}/project/{obj_id}"
        if obj_id and kind == "Conversation" and project_id:
            return f"{APP}/0/{project_id}/{obj_id}"
        return f"{base}/inbox/{self.du}"

    def fallback_title(self, t: dict) -> str:
        obj = self.store.get("DbObject", t["object"] or "")
        dates = {"DailySummaryStory": ("date", "Daily summary for"),
                 "OverdueTasksReminderStory": ("overdueTasksNotificationTimestamp", "Overdue tasks reminder for")}
        if (d := dates.get(obj.get("typeName"))) and obj.get(d[0]):
            day = datetime.fromtimestamp(int(obj[d[0]]) / 1000, timezone.utc).date()
            return f"{d[1]} {day}"
        return obj.get("name") or f"({t['type']} {t['thread']})"

    def events(self, notifs: list[dict]) -> list[dict]:
        """What each notification is about, oldest first: {time, who, what, text}."""
        out = []
        for n in notifs:
            story = self.store.get("DbObject", n.get("story") or "")
            kind = story.get("typeName") or "?"
            what = re.sub(r"(Story|Comment)$", "", kind)
            what = re.sub(r"(?<!^)(?=[A-Z])", " ", what).lower()  # TaskAssigned -> task assigned
            out.append({
                "time": (story.get("__creationTime") or n.get("__creationTime") or 0) / 1000,
                "who": self.user_name(story.get("creatorDu") or n.get("creatorDu")),
                "what": "comment" if kind == "CommentStory" else what,
                "text": story.get("text") or "",
            })
        return sorted(out, key=lambda e: e["time"])

    def user_name(self, du: str | None) -> str | None:
        if not du:
            return None
        key = json.dumps(["domainUserName", du, {}], separators=(",", ":"))
        return self.store.get("ServerComputedValue", key).get("value") or du

    def task_project_ids(self, task_id: str) -> list[str]:
        key = json.dumps(["taskProjectsContainingThis", task_id, {}], separators=(",", ":"))
        return list(self.store.get("ServerComputedValue", key).get("value") or [])

    def conversation_project_ids(self) -> dict[str, list[str]]:
        """Conversation id -> project ids, from Rank docs "Conversation.projectFollowers.<conv>.<pot>"."""
        out: dict[str, list[str]] = {}
        for coll, id_ in self.store.docs:
            if coll == "Rank" and id_.startswith("Conversation.projectFollowers."):
                conv, pot = id_.split(".")[2:4]
                out.setdefault(conv, []).append(pot)
        return out

    def inbox_items(self) -> list[dict]:
        """One item per (thread, task): a project thread ("task added to X") bundles several
        tasks, each archivable on its own. Item id is "<thread>/<task>" (or "<thread>" if no task)."""
        items = []
        conv_projects = self.conversation_project_ids()
        for t in self.inbox_threads():
            pot = t["object"] if t["object_type"] == "Pot" else None
            groups: dict[str | None, list[str]] = {}
            for nid in t["notifications"]:
                parent = self.store.get("DbObject", nid).get("storyParent")
                if self.store.get("DbObject", parent or "").get("typeName") == "DomainUser":
                    parent = None  # e.g. daily summaries hang off the recipient, not a task
                groups.setdefault(parent, []).append(nid)
            for task_id, nids in groups.items():
                notifs = [self.store.get("DbObject", n) for n in nids]
                parent = self.store.get("DbObject", task_id or "")
                # What the item is about: usually the parent itself (task, conversation,
                # project); for access requests the object access was requested to.
                task_ref, task, prefix = task_id, parent, ""
                if parent.get("typeName") == "RequestShareableAccess":
                    task_ref = parent.get("target")
                    task = self.store.get("DbObject", task_ref or "")
                    prefix = f"Access request ({(parent.get('state') or '?').lower()}): "
                elif parent.get("typeName") == "Pot":
                    prefix = "Project shared: "
                kind = task.get("typeName")
                if kind == "Pot":
                    project_ids = [task_ref]
                elif pot:
                    project_ids = [pot]
                elif kind == "Conversation":
                    project_ids = conv_projects.get(task_ref, [])
                elif kind == "Task":
                    project_ids = self.task_project_ids(task_ref)
                else:
                    project_ids = []
                names = [self.store.get("DbObject", p).get("name") or p for p in project_ids]
                # One link per project so the TUI can use the project it groups by.
                urls = {n: self.object_url(task, task_ref, p) for n, p in zip(names, project_ids)}
                items.append({
                    "id": f"{t['thread']}/{task_id}" if task_id else t["thread"],
                    "thread": t["thread"],
                    "task_id": task_id,
                    "url": self.object_url(task, task_ref, project_ids[-1] if project_ids else None),
                    "urls": urls,
                    "type": t["type"],
                    "title": prefix + (task.get("name") or self.fallback_title(t)),
                    # Task.completed is 0 or the completion time in ms
                    "completed": (task.get("completed") or 0) / 1000 or None,
                    "projects": names,
                    "assignee": self.user_name(task.get("assignee")) if kind == "Task" else None,
                    "mine": kind == "Task" and task.get("assignee") == self.du,
                    # dueDate: ms at UTC midnight of a date-only due; dueTime: exact ms if timed
                    "due_date": (datetime.fromtimestamp(task["dueDate"] / 1000, timezone.utc).date()
                                 .isoformat() if kind == "Task" and task.get("dueDate") else None),
                    "due_time": (task["dueTime"] / 1000 if kind == "Task" and task.get("dueTime")
                                 else None),
                    "notifications": nids,
                    "description": task.get("description") or task.get("text") or "",
                    "events": self.events(notifs),
                    "updated": max((n.get("__creationTime") or 0) for n in notifs) / 1000,
                })
        return items

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.dump:
            self.dump.close()


# ---------------------------------------------------------------- blocking facade


class WebInbox:
    """Blocking wrapper for the TUI. Each call opens a fresh websocket (run it in a worker
    thread); HTTP bootstrap (release, bundle version) is done once and reused."""

    def __init__(self, du: str, view: int | None, n: int = 0, *, browser: str | None = None,
                 profile: str | None = None, cookie_file: str | None = None):
        self.du, self.view, self.n = du, view, n
        self.sess = Session(browser, profile, cookie_file)
        self.rev, bundle = self.sess.release_info()
        self.ver = self.sess.bundle_version(bundle)
        self.notifications: dict[str, list[str]] = {}  # item id -> notification ids

    async def _client(self) -> SyncClient:
        ss = self.sess.start_session(self.rev, f"/1/{self.sess.domain}/inbox/{self.du}")
        client = SyncClient(self.sess, self.du, self.sess.domain)
        await client.connect(ss["session_id"], self.ver, self.rev)
        return client

    async def _list(self, log) -> list[dict]:
        client = await self._client()
        try:
            await client.load_inbox(self.n, self.view, log)
            return client.inbox_items()
        finally:
            await client.close()

    async def _archive(self, ids: list[str]) -> None:
        client = await self._client()
        try:
            await client.archive_threads([{"notifications": self.notifications[i]} for i in ids])
        finally:
            await client.close()

    def list_items(self, log=None) -> list[dict]:
        items = asyncio.run(self._list(log))
        self.notifications = {it["id"]: it["notifications"] for it in items}
        return items

    def archive(self, ids: list[str]) -> None:
        asyncio.run(self._archive(ids))


# ---------------------------------------------------------------- CLI


def cmd_cookies(args) -> None:
    cookies, ua, source = resolve_cookies(args.browser, args.profile, args.cookies)
    print(f"source: {source}\nuser-agent: {ua}")
    if not source.startswith("firefox"):
        for host, name, _ in sorted(cookies):
            print(f"  {host:24} {name}")
        return
    profile = find_profile(args.profile)
    rows = cookie_rows(profile)
    print(f"profile: {profile}  (using jar originAttributes={wanted_origin()!r})")
    by_origin: dict[str, list[dict]] = {}
    for r in rows:
        by_origin.setdefault(r["originAttributes"], []).append(r)
    for origin, rs in sorted(by_origin.items()):
        print(f"\n  jar {origin!r}{'  <- used' if origin == wanted_origin() else ''}")
        for r in sorted(rs, key=lambda r: (r["host"], r["name"])):
            if "asana.com" in r["host"] and not r["host"].endswith("developers.asana.com"):
                print(f"    {r['host']:22} {r['name']}")
    if not rows:
        print("  (no asana.com cookies — log in to Asana in this Firefox profile)")


def cmd_bootstrap(args) -> None:
    sess = Session(args.browser, args.profile, args.cookies)
    print("user-agent:", UA)
    print(f"user: {sess.user_id}  domain/shard: {sess.domain}")
    rev, bundle = sess.release_info()
    print("release_revision:", rev)
    print("bundle:", bundle)
    print("projectionBundleVersion:", sess.bundle_version(bundle))
    ss = sess.start_session(rev)
    print("start_session keys:", sorted(ss))
    print("session_id present:", bool(ss.get("session_id")))
    print("\nid candidates (domain user for --du):")
    pat = re.compile(r"shard|domain_?user|domainUser|user_?id|userId|workspace|domain_?id|inbox", re.I)
    for p, v in find_keys(ss, pat)[:80]:
        print(f"  {p} = {v!r}"[:200])


_clip_tool: str | None = None  # name of the last tool that worked; tried first, reset on failure


def sanitize(s: str, limit: int = 80) -> str:
    """Printable-only, length-capped copy of server-provided text before it is echoed:
    view names come from other users, so they must not carry terminal escapes."""
    return "".join(ch for ch in s if ch.isprintable())[:limit] or "?"


def read_clipboard() -> str | None:
    """Text from the system clipboard, or None when no clipboard tool exists/fails.
    Used for the big WS message: terminals truncate long pastes (macOS at 1 kB),
    so setup prefers reading a DevTools \"Copy message\" straight from the clipboard."""
    global _clip_tool
    tools = {"pbpaste": ["pbpaste"],                       # macOS
             "wl-paste": ["wl-paste", "--no-newline"],     # Wayland
             "xclip": ["xclip", "-o", "-selection", "clipboard"],
             "xsel": ["xsel", "--clipboard", "--output"]}
    candidates = [(name, cmd) for name, cmd in tools.items() if shutil.which(name)]
    if _clip_tool:
        candidates.sort(key=lambda nc: nc[0] != _clip_tool)  # remember what worked
    for name, cmd in candidates:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue  # tool exists but failed (headless, wedged display): try the next one
        if out.returncode != 0:
            continue
        _clip_tool = name
        return out.stdout or None
    _clip_tool = None  # nothing worked: re-probe from scratch next time
    return None


def read_paste(prompt: str, parse) -> tuple[str, set[str]] | None:
    """Read a possibly multi-line paste (pretty-printed JSON), accumulating until it
    parses *and* looks complete (balanced braces) so keys on later lines — e.g.
    db_object_id after objectViewId — aren't missed. A blank line ends the paste."""
    buf = ""
    while True:
        line = input(prompt if not buf else "   …> ")
        line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")  # terminal paste markers
        buf = f"{buf}\n{line}" if buf else line
        if not line.strip():
            return parse(buf) if buf.strip() else None  # blank line: paste is done
        if (parsed := parse(buf)) and buf.count("{") <= buf.count("}"):
            return parsed


async def discover_view_ids(sess, rev: str, ver: str, ss: dict, du: str):
    """(default view id, [(name, id), …]) over the live sync socket, or None.
    Connects, asks the FilterRow projection for the inbox's views, disconnects.
    The bundle version is fetched by the caller (sync httpx, own budget) so this
    coroutine stays pure websocket work the caller can time out cleanly.
    Any failure (socket, payload, release) just means: fall back to manual input."""
    client = None
    try:
        if not ss.get("session_id"):
            raise AsanaError("start_session returned no session_id")
        client = SyncClient(sess, du, sess.domain)
        await client.connect(ss["session_id"], ver, rev)
        return await client.discover_view()
    except Exception as e:  # best-effort discovery; the manual path is the safety net
        print(f"   view discovery failed ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return None
    finally:
        if client and client.ws:
            with contextlib.suppress(Exception):
                await client.ws.close()


def cmd_setup(args) -> None:
    """Guided setup: log in via browser cookies, discover the two IDs over the
    protocol (URL/--view/--paste as fallback), write .env."""
    print("asana-inbox setup — log in, discover your IDs, write .env\n")
    try:
        # 1) login: cookies from the browser (or --cookies FILE) come first —
        # both IDs are derived from the session they unlock.
        print("1) Asana login (cookies)")
        du = ws = ""
        bare_du = False  # du arrived without a workspace: its workspace is unverified
        if args.url:
            if not (parsed := parse_du(args.url)):
                raise AsanaError(f"no …/1/<workspace>/inbox/<id>/ in {args.url!r}")
            du, ws = parsed
            bare_du = not ws
        sess = None
        while sess is None:
            try:
                sess = Session(args.browser, args.profile, args.cookies,
                               domain=ws or os.environ.get("ASANA_DOMAIN") or None,
                               use_env=False)  # an existing pin drives setup; a URL ws overrides it
            except NoWorkspaceError:
                # cookies alone name no workspace: the inbox URL carries it
                print("   login works, but the cookies name no workspace.")
                print("   Open your Inbox in the web app. The URL looks like")
                print("   https://app.asana.com/1/<workspace>/inbox/<domain user id>/…")
                raw = input("   paste that URL (⏎ to quit): ")
                if not raw.strip():
                    print("   no URL, no workspace — cancelled", file=sys.stderr)
                    raise SystemExit(1) from None
                parsed = parse_du(raw)
                if parsed is None:
                    print("   couldn't find …/1/<workspace>/inbox/<id>/ in that — try again\n")
                    continue
                du, found_ws = parsed
                if not found_ws:
                    print("   that's only the domain user id — paste the full inbox URL")
                    print("   (the workspace in that URL is what gets pinned)\n")
                    continue
                ws = found_ws
                bare_du = False  # the URL supplied the workspace after all
            except AsanaError as e:
                # not a dead end: the IDs can still be pasted at the prompts below
                print("   ⚠ " + str(e).replace("\n", "\n     "))
                print("     continuing without a login;"
                      " the IDs must be pasted manually (README → Setup)")
                break
            except OSError as e:  # e.g. --cookies file missing
                print("   ✗ " + str(e).replace("\n", "\n     "))
                raise SystemExit(1) from None
        if sess:
            print(f"   ✓ logged in via {sess.source} (user {sess.user_id}, domain {sess.domain})\n")

        # 2) the two IDs: URL/--view flags win, then protocol discovery, then prompts.
        # A URL plus --view needs no protocol access at all.
        ss: dict | None = None
        switched = False
        rev = bundle = ""
        if not (args.url and args.view is not None) and sess:
            try:
                rev, bundle = sess.release_info()
                ss = sess.start_session(rev)
            except (AsanaError, OSError, RuntimeError, httpx.HTTPError) as e:
                # discovery is optional; the prompts below are the fallback
                print(f"2) protocol unreachable ({type(e).__name__}: {str(e)[:120]})")
        if not du and ss:
            du = du_from_session(ss) or ""
        while not du:
            print("2) Domain user ID (ASANA_DU)")
            print("   couldn't discover it; open your Inbox in the web app. The URL looks like")
            print("   https://app.asana.com/1/<workspace>/inbox/<domain user id>/…")
            parsed = parse_du(input("   paste that URL (or just the number after /inbox/): "))
            if parsed:
                du, ws = parsed
                bare_du = not ws  # hand-supplied without a workspace: unverified pairing
                break
            print("   couldn't find /1/<workspace>/inbox/<number>/ in that — try again\n")
        if ws and sess and ws != sess.domain:
            # the URL's workspace wins: otherwise discovery and the written IDs would
            # target one workspace while every request runs against another
            print(f"   ⚠ URL workspace {ws} ≠ cookie workspace {sess.domain} — switching to the URL's")
            try:
                old_sess, sess = sess, Session(args.browser, args.profile, args.cookies,
                                               domain=ws, use_env=False)
                old_sess.close()
                ss, rev, bundle = None, "", ""  # refetched below under the right domain
                switched = True
            except (AsanaError, OSError) as e:
                print("   ⚠ couldn't switch: " + str(e).replace("\n", " ")[:120])
                if not (args.url and args.view is not None):
                    raise SystemExit(1) from None
                # both IDs come from the command line; the stale-domain session is unused below
        if switched and sess and args.view is None:
            # the workspace switch reset ss; refetch under the right domain
            try:
                rev, bundle = sess.release_info()
                ss = sess.start_session(rev)
            except (AsanaError, OSError, RuntimeError, httpx.HTTPError) as e:
                print(f"2) protocol unreachable ({type(e).__name__}: {str(e)[:120]})")
        print(f"   → domain user id {du}" + (f" (workspace {ws})" if ws else "") + "\n")

        # 2b) objectViewId: --view flag, else discovered via the FilterRow sub, else paste
        view = str(args.view) if args.view is not None else None
        if view is not None and view == du:
            print("   ⚠ that --view value is the domain user id, not an objectViewId — ignored\n")
            view = None
        discovered = None
        if view is None:
            print("2) Inbox view ID (ASANA_VIEW)")
            if ss:
                print("   discovering views over the sync socket"
                      " (first run may download the ~46 MB app bundle;"
                      " capped at ~5 min on a slow link)…")
                ver = None
                try:  # sync fetch: stall=read timeout, deadline=hard cap for setup
                    ver = sess.bundle_version(bundle, stall=45,
                                              deadline=time.monotonic() + 300)
                except (AsanaError, RuntimeError, OSError, httpx.HTTPError) as e:
                    print(f"   bundle fetch failed ({type(e).__name__}: {str(e)[:100]})")
                if ver is not None:
                    try:
                        discovered = asyncio.run(asyncio.wait_for(
                            discover_view_ids(sess, rev, ver, ss, du), 120))
                    except TimeoutError:
                        print("   discovery timed out — falling back to manual input")
                        discovered = None
            if discovered:
                default, views = discovered
                print(f"   ✓ discovered {len(views)} view(s):")
                for name, vid in views:
                    mark = " ← default" if vid == default else ""
                    print(f"     {sanitize(vid)}  {sanitize(name or '(unnamed)')}{mark}")
                view = default
                if sys.stdin.isatty() and len(views) > 1:
                    if input(f"   use the default view {default}? [Y/n] ").strip().lower() == "n":
                        view = None
                        while view is None:
                            pick = input(f"   pick 1-{len(views)} (⏎: manual/clipboard entry): ").strip()
                            if not pick:
                                break  # empty answer: manual entry
                            if pick.isdigit() and 1 <= int(pick) <= len(views):
                                name, view = views[int(pick) - 1]
                                print(f"   → {sanitize(view)}  {sanitize(name or '(unnamed)')}")
                            else:
                                print(f"   enter a number 1-{len(views)}, or ⏎ for manual entry")
            if view is None:
                print("   fallback: copy the objectViewId from DevTools (README → Setup).")
                print("   Easiest: right-click the WS \"sub\" message → Copy message, then answer y")
                print("   when it's offered below — it's read from the clipboard"
                      " (terminals truncate long pastes).")
        declined_view = None  # a clipboard value the user said "no" to; new values re-offer
        while view is None:
            du_ids: set[str] = set()
            source = ""  # "clipboard" | "paste"
            if sys.stdin.isatty() and (clip := read_clipboard()) \
                    and (cparsed := parse_view(clip)) and cparsed[0] != declined_view:
                cview, cdu = cparsed
                bare = clip.strip().strip("'\"").isdigit()  # a lone number, no message
                if not (bare and cview == du):  # du still on the clipboard from step 2: skip
                    if bare:
                        accept = input(f"   clipboard has just the number {cview} — an objectViewId?"
                                       " Use it? [y/N] ").strip().lower() == "y"
                    else:
                        ok = du in cdu  # offer, but only an explicit y accepts: clipboard
                        # content is untrusted input — it must not land in .env by default
                        note = ("✓ db_object_id matches the domain user id" if ok else
                                "(no db_object_id to cross-check)" if not cdu else
                                f"⚠ db_object_id {sorted(cdu)} doesn't match the domain user id")
                        accept = input(f"   clipboard has it: objectViewId {cview}  {note}."
                                       " Use it? [y/N] ").strip().lower() == "y"
                    if not accept:
                        declined_view = cview  # don't nag about the same value again
                    else:
                        view, source = cview, "clipboard"
            if view is None:
                parsed = read_paste("   or paste it here (macOS: just the id number —"
                                    "\n   terminals truncate long pastes; blank line to retry)", parse_view)
                if parsed:
                    view, du_ids = parsed
                    source = "paste"
                    if view == du and not du_ids:
                        print("   that's the domain user id, not an objectViewId — try again\n")
                        view = None
                        continue
            if view is None:
                print("   no objectViewId found — try again\n")
                continue
            if source == "paste":
                if du_ids and du not in du_ids:
                    print(f"   ⚠ the message's db_object_id {sorted(du_ids)} doesn't match domain user id {du}")
                    print("     — likely the wrong message or URL; check the README's Setup section")
                    if input("     use this objectViewId anyway? [y/N] ").strip().lower() != "y":
                        print()
                        view = None
                        continue
                elif du_ids:
                    print("   ✓ db_object_id matches the domain user id")
                else:
                    print("   (no db_object_id in the paste to cross-check)")
        print(f"   → objectViewId {view}\n")

        # 3) write .env in the directory we run from
        env_path = Path.cwd() / ".env"
        values = {"ASANA_DU": du, "ASANA_VIEW": view}
        # pin the workspace too: without it the runtime follows whatever last_domain
        # says on the day — wrong for multi-workspace accounts and fresh profiles
        if ws or (sess and sess.domain):
            values["ASANA_DOMAIN"] = ws or sess.domain
        elif old_pin := os.environ.get("ASANA_DOMAIN"):
            # no workspace was learned: keep any existing pin rather than guess
            print(f"   ⚠ ASANA_DOMAIN={old_pin} (from your env/.env) was left untouched —")
            print("     remove it if it's not these IDs' workspace")
        stale = {k: os.environ[k] for k, v in values.items()
                 if (env := os.environ.get(k)) and env != v and k not in env_file_keys}
        existed = update_env_file(env_path, values)
        print(f"{'updated' if existed else 'wrote'} {env_path}:")
        for key, val in values.items():
            print(f"  {key}={val}")
        if stale:
            print("   ⚠ values already exported in this shell override the file above —")
            print("     unset them or re-run from a fresh shell, or they'll be used instead:")
            for k, v in stale.items():
                print(f"     {k}={v}")
        if bare_du and "ASANA_DOMAIN" in values and values["ASANA_DOMAIN"]:
            print(f"   ⚠ the domain user id was supplied without its workspace;")
            print(f"     ASANA_DOMAIN={values['ASANA_DOMAIN']} was taken from your login/env pin —")
            print("     re-run setup with the full inbox URL if this pairing is wrong")
        if sess:
            sess.close()
        print("\nTry it:  ./asana_web.py inbox -n 5   (or just ./asana_inbox.py)")
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled; nothing written", file=sys.stderr)
        raise SystemExit(1) from None


def print_items(items: list[dict]) -> None:
    """Items grouped under their thread; ids are what `archive` takes."""
    by_thread: dict[str, list[dict]] = {}
    for it in items:
        by_thread.setdefault(it["thread"], []).append(it)
    for thread, its in by_thread.items():
        if len(its) > 1:
            print(f"{thread}  {its[0]['type']}  ({len(its)} tasks)")
        for it in its:
            indent = "  " if len(its) > 1 else ""
            print(f"{indent}{it['id']:<34} n={len(it['notifications']):<2} {it['title']}")
            print(f"{indent}{'':34}      projects: {', '.join(it['projects']) or '-'}")


async def open_inbox(args) -> tuple[SyncClient, list[dict]]:
    sess = Session(args.browser, args.profile, args.cookies)
    rev, bundle = sess.release_info()
    ver = sess.bundle_version(bundle)
    ss = sess.start_session(rev, f"/1/{sess.domain}/inbox/{args.du}")
    client = SyncClient(sess, args.du, args.shard or sess.domain,
                        Path(args.dump) if args.dump else None)
    await client.connect(ss["session_id"], ver, rev)
    print("connected", file=sys.stderr)
    await client.load_inbox(args.n, args.view, log=lambda s: print(s, file=sys.stderr))
    return client, client.inbox_threads()


async def drain(client: SyncClient, seconds: float) -> None:
    """Keep applying server pushes for a while (no-op pred)."""
    try:
        await client.wait_for(lambda m: False, seconds)
    except TimeoutError:
        pass


async def cmd_inbox(args) -> None:
    client, threads = await open_inbox(args)
    try:
        items = client.inbox_items()
        print_items(items)
        print(f"{len(items)} items in {len(threads)} threads", file=sys.stderr)
    finally:
        await client.close()


async def cmd_archive(args) -> None:
    """Archive items ("<thread>/<task>") or whole threads ("<thread>")."""
    client, _ = await open_inbox(args)
    try:
        items = client.inbox_items()
        wanted = set(args.id)
        chosen = [it for it in items if it["id"] in wanted or it["thread"] in wanted]
        found = {it["id"] for it in chosen} | {it["thread"] for it in chosen}
        if missing := wanted - found:
            print(f"not among the {len(items)} loaded items (raise -n?): {sorted(missing)}",
                  file=sys.stderr)
        print_items(chosen)
        if not chosen:
            return
        if not args.yes:
            print(f"dry run: would mark {sum(len(it['notifications']) for it in chosen)} notifications "
                  "read for the items above; pass --yes to archive", file=sys.stderr)
            return
        await client.archive_threads(chosen)
        print("committed", file=sys.stderr)
        await drain(client, 3)
        chosen_ids = {it["id"] for it in chosen}
        remaining = client.inbox_items()
        still = chosen_ids & {it["id"] for it in remaining}
        print("still listed after archive: " + (", ".join(sorted(still)) or "none"), file=sys.stderr)
        siblings = [it for it in remaining if it["thread"] in {c["thread"] for c in chosen}]
        if siblings:
            print("other items left in the same thread(s):", file=sys.stderr)
            print_items(siblings)
    finally:
        await client.close()


def add_cookie_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--browser", choices=["auto", *BROWSERS],
                    help="cookie source browser (env ASANA_BROWSER; default auto)")
    ap.add_argument("--profile", help="browser profile name/dir (default: browser's default)")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookies.txt or Cookie-header file instead of a browser (env ASANA_COOKIES)")


def main() -> None:
    try:
        load_env_file()
    except AsanaError as e:  # a broken .env shouldn't stop cookie listing and friends
        print(f"warning: {e}", file=sys.stderr)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_cookie_args(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cookies")
    sub.add_parser("bootstrap")
    setup = sub.add_parser("setup", help="guided setup: parse the two IDs, check login, write .env")
    setup.add_argument("url", nargs="?", help="inbox URL …/1/<workspace>/inbox/<du>/… (else prompted)")
    setup.add_argument("--view", type=int, help="objectViewId if you already have it (else prompted)")
    add_cookie_args(setup)  # also work after the subcommand, as the setup hints suggest
    inbox = sub.add_parser("inbox", help="list threads")
    arch = sub.add_parser("archive", help="archive items/threads by id (dry run unless --yes)")
    arch.add_argument("id", nargs="+", help="item id <thread>/<task>, or a thread id for the whole thread")
    arch.add_argument("--yes", action="store_true", help="actually archive")
    for p in (inbox, arch):
        p.add_argument("--du", default=os.environ.get("ASANA_DU"), required="ASANA_DU" not in os.environ,
                       help="domain user id (web inbox URL /1/<domain>/inbox/<du>; env ASANA_DU)")
        p.add_argument("--shard", help="shardId (default: domain id)")
        p.add_argument("-n", type=int, default=0, help="threads to load (0 = all)")
        p.add_argument("--view", type=int, default=os.environ.get("ASANA_VIEW"),
                       help="inbox objectViewId (env ASANA_VIEW)")
        p.add_argument("--dump", help="append raw frames as JSONL (contains internal data; don't commit)")
    args = ap.parse_args()
    if args.cmd == "cookies":
        cmd_cookies(args)
    elif args.cmd == "bootstrap":
        cmd_bootstrap(args)
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "inbox":
        asyncio.run(cmd_inbox(args))
    else:
        asyncio.run(cmd_archive(args))


if __name__ == "__main__":
    try:
        main()
    except AsanaError as e:
        sys.exit(str(e))
