#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=0.80"]
# ///
"""Bulk-archive Asana Inbox items from a TUI."""

import random
import re
from dataclasses import dataclass
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
            )
            for i in range(n)
        }

    def list_items(self) -> list[InboxItem]:
        return list(self.items.values())

    def archive(self, ids: list[str]) -> None:
        for i in ids:
            self.items.pop(i, None)


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
        self.query_one(DataTable).add_columns("", "Project", "Title", "Kind", "Updated")
        self.action_reload()

    def action_reload(self) -> None:
        self.items = self.backend.list_items()
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
                it.title,
                it.kind,
                it.updated.strftime("%Y-%m-%d %H:%M"),
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
            ids = {it.id for it in self.items if rx.search(f"{it.project} {it.title}")}
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
            try:
                self.backend.archive(ids)
            except Exception as e:
                self.notify(f"Archive failed: {e}", severity="error", timeout=10)
                return
            self.notify(f"Archived {len(ids)} items")
            self.marked.clear()
            self.action_reload()

        self.push_screen(Confirm(f"Archive {len(self.marked)} marked items?"), do)


if __name__ == "__main__":
    InboxApp(FakeBackend()).run()
