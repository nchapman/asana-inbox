#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=0.80", "httpx>=0.27", "websockets>=13", "certifi"]
# ///
"""Bulk-archive Asana Inbox items from a TUI.

    ./asana_inbox.py --du <domain user id> --view <inbox objectViewId>   (or env ASANA_DU / ASANA_VIEW)
    ./asana_inbox.py --fake
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


class Backend(Protocol):
    def list_items(self) -> list[InboxItem]: ...
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
            )
            for i in range(n)
        }

    def list_items(self) -> list[InboxItem]:
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

    def __init__(self, du: str, view: int | None, profile: str | None, n: int, ignore: set[str]):
        from asana_web import WebInbox  # heavy deps only when used

        self.web = WebInbox(du, view, profile, n)
        self.ignore = ignore

    def primary_project(self, projects: list[str], kind: str) -> str:
        # Asana lists broad memberships (company goals, planning boards) first; the most
        # specific project tends to be last.
        useful = [p for p in projects if p not in self.ignore]
        if useful or projects:
            return (useful or projects)[-1]
        return {"conversation": "(conversations)", "daily summary": "(daily summaries)"}.get(
            kind, "(no project)")

    def list_items(self) -> list[InboxItem]:
        return [
            InboxItem(
                id=it["id"],
                title=it["title"],
                project=self.primary_project(it["projects"], KINDS.get(it["type"], "")),
                kind=KINDS.get(it["type"], it["type"] or "?"),
                updated=datetime.fromtimestamp(it["updated"]),
                projects=it["projects"],
                completed=datetime.fromtimestamp(it["completed"]) if it.get("completed") else None,
            )
            for it in self.web.list_items()
        ]

    def archive(self, ids: list[str]) -> None:
        self.web.archive(ids)


SORTS = {
    "project": lambda it: (it.project.lower(), it.title.lower()),
    "title": lambda it: (it.title.lower(), it.project.lower()),
    "newest": lambda it: -it.updated.timestamp(),
    "oldest": lambda it: it.updated.timestamp(),
}


class RegexPrompt(ModalScreen[tuple[str, bool] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, unmark: bool):
        super().__init__()
        self.unmark = unmark

    def compose(self) -> ComposeResult:
        verb = "Unmark" if self.unmark else "Mark"
        yield Input(placeholder=f"{verb} items whose project/title match regex")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss((event.value, self.unmark))


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
        Binding("slash", "regex(False)", "Mark /re/"),
        Binding("backslash", "regex(True)", "Unmark /re/"),
        Binding("p", "toggle_project", "Mark project"),
        Binding("c", "toggle_completed", "Mark completed"),
        Binding("a", "toggle_all", "All/none"),
        Binding("s", "cycle_sort", "Sort"),
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
            items = self.backend.list_items()
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
        for it in sorted(self.items, key=SORTS[self.sort]):
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
        if cursor_id in {it.id for it in self.items}:
            table.move_cursor(row=table.get_row_index(cursor_id))
        self.title = "Asana Inbox"
        self.sub_title = f"{len(self.items)} items · {len(self.marked)} marked · sort: {self.sort}"

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
        ids = {it.id for it in self.items if it.project == cur.project}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_table()

    def action_toggle_completed(self) -> None:
        ids = {it.id for it in self.items if it.completed}
        if not ids:
            self.notify("No completed tasks in the inbox")
            return
        unmark = ids <= self.marked
        self.marked = self.marked - ids if unmark else self.marked | ids
        self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} completed tasks")
        self.refresh_table()

    def action_toggle_all(self) -> None:
        all_ids = {it.id for it in self.items}
        self.marked = set() if self.marked == all_ids else all_ids
        self.refresh_table()

    def action_cycle_sort(self) -> None:
        keys = list(SORTS)
        self.sort = keys[(keys.index(self.sort) + 1) % len(keys)]
        self.refresh_table()

    def action_regex(self, unmark: bool) -> None:
        def apply(result: tuple[str, bool] | None) -> None:
            if not result or not result[0]:
                return
            pattern, unmark = result
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                self.notify(f"Bad regex: {e}", severity="error")
                return
            ids = {it.id for it in self.items
                   if rx.search(f"{it.project} {it.title} {' '.join(it.projects)} {it.kind}")}
            self.marked = self.marked - ids if unmark else self.marked | ids
            self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} items")
            self.refresh_table()

        self.push_screen(RegexPrompt(unmark), apply)

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

        self.push_screen(Confirm(f"Archive {len(self.marked)} marked items?"), do)

    def _archive(self, ids: list[str]) -> None:  # worker thread
        try:
            self.backend.archive(ids)
        except BaseException as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Archive failed: {e}", severity="error", timeout=15)
            self.call_from_thread(self.refresh_table)
            return

        def done() -> None:
            self.notify(f"Archived {len(ids)} items")
            self.marked -= set(ids)
            # Drop locally right away; the reload confirms against the server.
            self.items = [it for it in self.items if it.id not in set(ids)]
            self.refresh_table()
            self.action_reload()

        self.call_from_thread(done)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="use generated data, no Asana")
    ap.add_argument("--du", default=os.environ.get("ASANA_DU"), help="domain user id (env ASANA_DU)")
    ap.add_argument("--view", type=int, default=os.environ.get("ASANA_VIEW"),
                    help="inbox objectViewId (env ASANA_VIEW)")
    ap.add_argument("--profile", help="Firefox profile name or directory")
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
        backend = WebBackend(args.du, args.view, args.profile, args.n, set(args.ignore_project))
    InboxApp(backend).run()


if __name__ == "__main__":
    main()
