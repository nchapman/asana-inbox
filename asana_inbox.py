#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=0.80", "httpx>=0.27", "websockets>=13", "certifi", "browser-cookie3>=0.20"]
# ///
"""Bulk-archive Asana Inbox items from a TUI.

    ./asana_inbox.py --du <domain user id> --view <inbox objectViewId>   (or env ASANA_DU / ASANA_VIEW)
    ./asana_inbox.py --fake

Cookies come from the first browser with an Asana login (Firefox, Chrome, Chromium, Brave, Edge);
see --browser/--profile/--cookies, and `./asana_web.py --help` for exporting a cookie file.
"""

import argparse
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label


@dataclass
class InboxItem:
    id: str
    title: str
    project: str
    kind: str
    updated: datetime
    projects: list[str] = field(default_factory=list)  # all projects; `project` is the grouping one
    completed: datetime | None = None
    url: str = ""


class Backend(Protocol):
    def list_items(self, progress=None) -> list[InboxItem]: ...
    def archive(self, ids: list[str]) -> None: ...


class FakeBackend:
    def __init__(self, n: int = 60):
        rnd = random.Random(1)
        projects = ["Platform", "Website", "Mobile App", "Customer Support", "(no project)"]
        verbs = ["Fix", "Investigate", "Rotate", "Upgrade", "Review"]
        things = ["login timeout", "flaky CI job", "database migration", "TLS certificate", "search index"]
        kinds = ["comment", "assigned", "completed", "status update"]
        now = datetime.now()
        self.items = {
            str(i): InboxItem(
                id=str(i),
                title=f"{rnd.choice(verbs)} {rnd.choice(things)}",
                project=rnd.choice(projects),
                kind=rnd.choice(kinds),
                updated=now - timedelta(hours=rnd.randint(1, 24 * 60)),
                completed=now if rnd.random() < 0.2 else None,
                url=f"https://app.asana.com/1/0/task/{1000 + i}",
            )
            for i in range(n)
        }

    def list_items(self, progress=None) -> list[InboxItem]:
        return list(self.items.values())

    def archive(self, ids: list[str]) -> None:
        for i in ids:
            self.items.pop(i, None)


KINDS = {
    "TaskNotificationThread": "task",
    "TaskAddedToPotNotificationThread": "added to project",
    "ConversationNotificationThread": "conversation",
    "DailySummaryNotificationThread": "daily summary",
}


class WebBackend:
    """Real Asana inbox via the web app's sync protocol (asana_web.py)."""

    def __init__(self, du: str, view: int | None, n: int, ignore: set[str], **cookie_source):
        from asana_web import WebInbox  # heavy deps only when used

        self.web = WebInbox(du, view, n, **cookie_source)
        self.ignore = ignore

    def primary_project(self, projects: list[str], kind: str) -> str:
        # Asana lists broad memberships (company goals, planning boards) first; the most
        # specific project tends to be last.
        useful = [p for p in projects if p not in self.ignore]
        if useful or projects:
            return (useful or projects)[-1]
        return {"conversation": "(conversations)", "daily summary": "(daily summaries)"}.get(
            kind, "(no project)")

    def list_items(self, progress=None) -> list[InboxItem]:
        items = []
        for it in self.web.list_items(progress):
            project = self.primary_project(it["projects"], KINDS.get(it["type"], ""))
            items.append(InboxItem(
                id=it["id"],
                title=it["title"],
                project=project,
                kind=KINDS.get(it["type"], it["type"] or "?"),
                updated=datetime.fromtimestamp(it["updated"]),
                projects=it["projects"],
                completed=datetime.fromtimestamp(it["completed"]) if it.get("completed") else None,
                url=it.get("urls", {}).get(project) or it.get("url", ""),
            ))
        return items

    def archive(self, ids: list[str]) -> None:
        self.web.archive(ids)


SORTS = {
    "project": lambda it: (it.project.lower(), it.title.lower()),
    "title": lambda it: (it.title.lower(), it.project.lower()),
    "newest": lambda it: -it.updated.timestamp(),
    "oldest": lambda it: it.updated.timestamp(),
}


class RegexPrompt(ModalScreen[tuple[str, str] | None]):
    """Ask for a regex; returns (pattern, mode) where mode is "mark", "unmark" or "filter"."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    PLACEHOLDERS = {
        "mark": "Mark shown items matching regex (project, title, other projects, kind, 'done')",
        "unmark": "Unmark shown items matching regex",
        "filter": "Show only items matching regex (empty = show all)",
    }

    def __init__(self, mode: str, value: str = ""):
        super().__init__()
        self.mode, self.value = mode, value

    def compose(self) -> ComposeResult:
        yield Input(value=self.value, placeholder=self.PLACEHOLDERS[self.mode])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss((event.value, self.mode))


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Grid(id="confirm"):
            yield Label(self.message)
            yield Button("Archive", variant="error", id="yes")
            yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class InboxApp(App):
    CSS = """
    RegexPrompt, Confirm { align: center middle; }
    RegexPrompt Input { width: 70%; }
    #confirm { grid-size: 2; grid-rows: auto auto; width: 60; height: auto; padding: 1 2;
               border: thick $error; background: $surface; }
    #confirm Label { column-span: 2; width: 100%; margin-bottom: 1; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Mark"),
        Binding("f", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("slash", "regex('mark')", "Mark /re/"),
        Binding("backslash", "regex('unmark')", "Unmark /re/"),
        Binding("p", "toggle_project", "Mark project"),
        Binding("c", "toggle_completed", "Mark completed"),
        Binding("a", "toggle_all", "All/none"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("y", "copy_url", "Copy URL"),
        Binding("x", "archive", "Archive marked"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend
        self.items: list[InboxItem] = []
        self.marked: set[str] = set()
        self.sort = "project"
        self.filter: re.Pattern | None = None

    @staticmethod
    def match_text(it: InboxItem) -> str:
        done = "done" if it.completed else ""
        return f"{it.project} {it.title} {' '.join(it.projects)} {it.kind} {done}"

    def visible(self) -> list[InboxItem]:
        """Items passing the filter; every bulk mark action works on these only."""
        if self.filter is None:
            return self.items
        return [it for it in self.items if self.filter.search(self.match_text(it))]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("", "Project", "Done", "Title", "Kind", "Updated", "Also in")
        self.action_reload()

    def action_reload(self) -> None:
        self.busy("loading…")
        self.run_worker(self._load, thread=True, exclusive=True, group="backend")

    def busy(self, what: str) -> None:
        self.sub_title = what

    def _load(self) -> None:  # worker thread
        try:
            items = self.backend.list_items(lambda s: self.call_from_thread(self.busy, s))
        except BaseException as e:  # noqa: BLE001 - show anything, don't kill the app
            self.call_from_thread(self.notify, f"Load failed: {e}", severity="error", timeout=15)
            self.call_from_thread(self.refresh_table)
            return
        self.call_from_thread(self._set_items, items)

    def _set_items(self, items: list[InboxItem]) -> None:
        self.items = items
        self.marked &= {it.id for it in self.items}
        self.refresh_table()

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        cursor_id = self.current_id()
        table.clear()
        prev_project = None
        shown = self.visible()
        for it in sorted(shown, key=SORTS[self.sort]):
            project = it.project if self.sort != "project" or it.project != prev_project else ""
            prev_project = it.project
            table.add_row(
                "●" if it.id in self.marked else "",
                project,
                "✓" if it.completed else "",
                it.title,
                it.kind,
                it.updated.strftime("%Y-%m-%d %H:%M"),
                ", ".join(p for p in it.projects if p != it.project)[:60],
                key=it.id,
            )
        if cursor_id in {it.id for it in shown}:
            table.move_cursor(row=table.get_row_index(cursor_id))
        self.title = "Asana Inbox"
        parts = [f"{len(self.items)} items", f"{len(self.marked)} marked", f"sort: {self.sort}"]
        if self.filter is not None:
            hidden_marked = len(self.marked - {it.id for it in shown})
            parts[0] = f"{len(shown)}/{len(self.items)} shown"
            parts.append(f"filter: /{self.filter.pattern}/")
            if hidden_marked:
                parts[1] += f" ({hidden_marked} hidden)"
        self.sub_title = " · ".join(parts)

    def current_id(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def current_item(self) -> InboxItem | None:
        cid = self.current_id()
        return next((it for it in self.items if it.id == cid), None)

    def action_toggle(self) -> None:
        cid = self.current_id()
        if cid is None:
            return
        self.marked ^= {cid}
        self.refresh_table()
        self.query_one(DataTable).action_cursor_down()

    def action_toggle_project(self) -> None:
        cur = self.current_item()
        if cur is None:
            return
        ids = {it.id for it in self.visible() if it.project == cur.project}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_table()

    def action_copy_url(self) -> None:
        """Copy the selected item's Asana link via OSC 52 (terminal clipboard escape).
        In tmux this needs `set -g set-clipboard on`."""
        cur = self.current_item()
        if cur is None or not cur.url:
            self.notify("No URL for this item", severity="warning")
            return
        self.copy_to_clipboard(cur.url)
        self.notify(f"Copied {cur.url}")

    def action_toggle_completed(self) -> None:
        ids = {it.id for it in self.visible() if it.completed}
        if not ids:
            self.notify("No completed tasks shown")
            return
        unmark = ids <= self.marked
        self.marked = self.marked - ids if unmark else self.marked | ids
        self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} completed tasks")
        self.refresh_table()

    def action_toggle_all(self) -> None:
        ids = {it.id for it in self.visible()}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_table()

    def action_cycle_sort(self) -> None:
        keys = list(SORTS)
        self.sort = keys[(keys.index(self.sort) + 1) % len(keys)]
        self.refresh_table()

    def action_regex(self, mode: str) -> None:
        def apply(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            pattern, mode = result
            if mode == "filter" and not pattern:
                self.action_clear_filter()
                return
            if not pattern:
                return
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                self.notify(f"Bad regex: {e}", severity="error")
                return
            if mode == "filter":
                self.filter = rx
            else:
                ids = {it.id for it in self.visible() if rx.search(self.match_text(it))}
                unmark = mode == "unmark"
                self.marked = self.marked - ids if unmark else self.marked | ids
                self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} items")
            self.refresh_table()

        current = self.filter.pattern if mode == "filter" and self.filter is not None else ""
        self.push_screen(RegexPrompt(mode, current), apply)

    def action_filter(self) -> None:
        self.action_regex("filter")

    def action_clear_filter(self) -> None:
        if self.filter is not None:
            self.filter = None
            self.refresh_table()

    def action_archive(self) -> None:
        if not self.marked:
            self.notify("Nothing marked", severity="warning")
            return

        def do(ok: bool | None) -> None:
            if not ok:
                return
            ids = sorted(self.marked)
            self.busy(f"archiving {len(ids)}…")
            self.run_worker(lambda: self._archive(ids), thread=True, exclusive=True, group="backend")

        hidden = len(self.marked - {it.id for it in self.visible()})
        note = f"\n({hidden} of them are hidden by the filter)" if hidden else ""
        self.push_screen(Confirm(f"Archive {len(self.marked)} marked items?{note}"), do)

    def _archive(self, ids: list[str]) -> None:  # worker thread
        try:
            self.backend.archive(ids)
        except BaseException as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Archive failed: {e}", severity="error", timeout=15)
            self.call_from_thread(self.refresh_table)
            return

        def done() -> None:
            self.notify(f"Archived {len(ids)} items (press r to re-sync with Asana)")
            self.marked -= set(ids)
            # The mutation was committed; drop locally instead of an expensive full reload.
            self.items = [it for it in self.items if it.id not in set(ids)]
            self.refresh_table()

        self.call_from_thread(done)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="use generated data, no Asana")
    ap.add_argument("--du", default=os.environ.get("ASANA_DU"), help="domain user id (env ASANA_DU)")
    ap.add_argument("--view", type=int, default=os.environ.get("ASANA_VIEW"),
                    help="inbox objectViewId (env ASANA_VIEW)")
    ap.add_argument("--browser", help="cookie source: auto|firefox|chrome|chromium|brave|edge "
                    "(env ASANA_BROWSER; default auto)")
    ap.add_argument("--profile", help="browser profile name or directory")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookies.txt / Cookie-header file instead of a browser (env ASANA_COOKIES)")
    ap.add_argument("-n", type=int, default=0, help="threads to load (0 = all)")
    ap.add_argument("--ignore-project", action="append",
                    default=[p for p in os.environ.get("ASANA_IGNORE_PROJECTS", "").split(",") if p],
                    help="don't group by this project if the task has others (repeatable; "
                         "env ASANA_IGNORE_PROJECTS, comma-separated)")
    args = ap.parse_args()
    if args.fake:
        backend: Backend = FakeBackend()
    else:
        if not args.du:
            ap.error("--du (or ASANA_DU) is required unless --fake")
        print("bootstrapping…")
        try:
            backend = WebBackend(args.du, args.view, args.n, set(args.ignore_project),
                                 browser=args.browser, profile=args.profile, cookie_file=args.cookies)
        except RuntimeError as e:  # AsanaError: no cookies, logged out, HTTP errors
            raise SystemExit(str(e)) from None
    InboxApp(backend).run()


if __name__ == "__main__":
    main()
