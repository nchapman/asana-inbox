#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "websockets>=13", "certifi"]
# ///
"""Asana web-app sync client (undocumented protocol, see PROTOCOL.md).

Read-only spike:
    ./asana_web.py cookies [--profile NAME|PATH]       # check Firefox auth (prints names only)
    ./asana_web.py bootstrap [--profile ...]           # release, bundle version, start_session, id candidates
    ./asana_web.py inbox --du ID [--view ID] [-n 50] [--dump frames.jsonl]
    ./asana_web.py archive --du ID THREAD_ID... [--yes]   # dry run without --yes

Env: ASANA_UA (User-Agent override), ASANA_FF_CONTAINER (Firefox container id),
     ASANA_DOMAIN (workspace gid; default: last_domain cookie).
"""

import argparse
import asyncio
import configparser
import json
import os
import re
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import certifi
import httpx
import websockets

APP = "https://app.asana.com"
WS_HOST = "cdn.sync.app.asana.com"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "asana-inbox"



class AsanaError(RuntimeError):
    """Expected failure with a human-readable message (auth, HTTP, protocol)."""


# ---------------------------------------------------------------- Firefox cookies

FIREFOX_ROOTS = [
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
    ini = profile / "compatibility.ini"
    m = re.search(r"LastVersion=(\d+)\.", ini.read_text()) if ini.exists() else None
    ver = m.group(1) if m else "140"
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"


def cookie_header(cookies: list[tuple[str, str, str]], host: str) -> str:
    def matches(chost: str) -> bool:
        if chost.startswith("."):
            return host == chost[1:] or host.endswith(chost)
        return host == chost

    jar = {name: value for chost, name, value in cookies if matches(chost)}
    return "; ".join(f"{k}={v}" for k, v in jar.items())


# ---------------------------------------------------------------- bootstrap


class Session:
    def __init__(self, profile: Path):
        global UA
        UA = firefox_user_agent(profile)
        self.cookies = cookies = load_cookies(profile)
        jar = dict(p.split("=", 1) for p in cookie_header(cookies, "app.asana.com").split("; ") if "=" in p)
        missing = [k for k in ("ticket", "user", "last_domain") if k not in jar]
        if missing:
            raise AsanaError(f"cookies missing {missing}; log in to Asana in Firefox")
        self.ticket = jar["ticket"]          # CSRF token, echoed as X-Asana-Ticket
        self.user_id = jar["user"]           # user gid
        self.domain = os.environ.get("ASANA_DOMAIN", jar["last_domain"])  # workspace gid == shard_id
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
        return json.loads(text[text.index("{"):])  # tolerate anti-JSON-hijack prefixes

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

    def bundle_version(self, bundle_url: str) -> str:
        commit = re.search(r"/bundles/([0-9a-f]+)/", bundle_url).group(1)
        cached = CACHE / f"bundle-version-{commit}"
        if cached.exists():
            return cached.read_text().strip()
        rx = re.compile(rb'\{version:"([^"]+)",clientQueries:')
        tail = b""
        with httpx.stream("GET", bundle_url, headers={"User-Agent": UA}, timeout=120) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                buf = tail + chunk
                if m := rx.search(buf):
                    CACHE.mkdir(parents=True, exist_ok=True)
                    cached.write_text(m.group(1).decode())
                    return m.group(1).decode()
                tail = buf[-256:]
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
        if n > 0:
            await self.sub_inbox(n, view_id)
            return len(self.inbox_threads())
        n, got = 100, 0
        while True:
            await self.sub_inbox(n, view_id)
            prev, got = got, len(self.inbox_threads())
            if log:
                log(f"asked {n} threads, got {got}")
            if got < n:
                return got
            if got <= prev:  # server stopped growing: probably a cap on numAdditionalThreads
                raise AsanaError(f"inbox stuck at {got} threads; server may cap numAdditionalThreads")
            await self.send({"msg": "unsub", "id": str(self.next_id)})
            n *= 2

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
        }, timeout=90)

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

    def fallback_title(self, t: dict) -> str:
        obj = self.store.get("DbObject", t["object"] or "")
        if obj.get("typeName") == "DailySummaryStory" and obj.get("date"):
            day = datetime.fromtimestamp(int(obj["date"]) / 1000, timezone.utc).date()
            return f"Daily summary for {day}"
        return obj.get("name") or f"({t['type']} {t['thread']})"

    def task_projects(self, task_id: str) -> list[str]:
        key = json.dumps(["taskProjectsContainingThis", task_id, {}], separators=(",", ":"))
        ids = self.store.get("ServerComputedValue", key).get("value") or []
        return [self.store.get("DbObject", p).get("name") or p for p in ids]

    def inbox_items(self) -> list[dict]:
        """One item per (thread, task): a project thread ("task added to X") bundles several
        tasks, each archivable on its own. Item id is "<thread>/<task>" (or "<thread>" if no task)."""
        items = []
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
                task = self.store.get("DbObject", task_id or "")
                if pot:
                    projects = [self.store.get("DbObject", pot).get("name") or pot]
                elif task_id:
                    projects = self.task_projects(task_id)
                else:
                    projects = []
                items.append({
                    "id": f"{t['thread']}/{task_id}" if task_id else t["thread"],
                    "thread": t["thread"],
                    "task_id": task_id,
                    "type": t["type"],
                    "title": task.get("name") or self.fallback_title(t),
                    # Task.completed is 0 or the completion time in ms
                    "completed": (task.get("completed") or 0) / 1000 or None,
                    "projects": projects,
                    "notifications": nids,
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

    def __init__(self, du: str, view: int | None, profile: str | None = None, n: int = 0):
        self.du, self.view, self.n = du, view, n
        self.sess = Session(find_profile(profile))
        self.rev, bundle = self.sess.release_info()
        self.ver = self.sess.bundle_version(bundle)
        self.notifications: dict[str, list[str]] = {}  # item id -> notification ids

    async def _client(self) -> SyncClient:
        ss = self.sess.start_session(self.rev, f"/1/{self.sess.domain}/inbox/{self.du}")
        client = SyncClient(self.sess, self.du, self.sess.domain)
        await client.connect(ss["session_id"], self.ver, self.rev)
        return client

    async def _list(self) -> list[dict]:
        client = await self._client()
        try:
            await client.load_inbox(self.n, self.view)
            return client.inbox_items()
        finally:
            await client.close()

    async def _archive(self, ids: list[str]) -> None:
        client = await self._client()
        try:
            await client.archive_threads([{"notifications": self.notifications[i]} for i in ids])
        finally:
            await client.close()

    def list_items(self) -> list[dict]:
        items = asyncio.run(self._list())
        self.notifications = {it["id"]: it["notifications"] for it in items}
        return items

    def archive(self, ids: list[str]) -> None:
        asyncio.run(self._archive(ids))


# ---------------------------------------------------------------- CLI


def cmd_cookies(args) -> None:
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
    sess = Session(find_profile(args.profile))
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
    sess = Session(find_profile(args.profile))
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="Firefox profile name or directory (default: Firefox default)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cookies")
    sub.add_parser("bootstrap")
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
    elif args.cmd == "inbox":
        asyncio.run(cmd_inbox(args))
    else:
        asyncio.run(cmd_archive(args))


if __name__ == "__main__":
    try:
        main()
    except AsanaError as e:
        sys.exit(str(e))
