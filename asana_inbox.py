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
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static


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
    assignee: str | None = None
    mine: bool = False  # assigned to the current user
    due: datetime | None = None  # local time; midnight for date-only dues
    due_has_time: bool = False
    description: str = ""
    events: list[dict] = field(default_factory=list)  # {time, who, what, text}

    def due_text(self) -> Text | str:
        if self.due is None:
            return ""
        label = self.due.strftime("%Y-%m-%d %H:%M" if self.due_has_time else "%Y-%m-%d")
        if self.completed:
            return Text(label, style="dim")
        now = datetime.now()
        overdue = self.due < now if self.due_has_time else self.due.date() < now.date()
        if overdue:
            return Text(label, style="bold red")
        if self.due.date() == now.date():
            return Text(label, style="yellow")
        return label


class Backend(Protocol):
    def list_items(self, progress=None) -> list[InboxItem]: ...
    def archive(self, ids: list[str]) -> None: ...


class FakeBackend:
    """Generated inbox for trying the UI (--fake) and for docs/screenshots.py."""

    def __init__(self, n: int = 60):
        rnd = random.Random(1)
        project_names = ["Platform", "Website", "Mobile App", "Customer Support",
                    # longer than PROJECT_WIDTH, to exercise shortening (`w`)
                    "Infrastructure Reliability & Observability Initiatives"]
        verbs = ["Fix", "Investigate", "Rotate", "Upgrade", "Review"]
        things = ["login timeout", "flaky CI job", "database migration", "TLS certificate", "search index"]
        # Some titles longer than TITLE_WIDTH, to exercise shortening (`w`).
        long_tails = [
            ": follow up from the incident review, confirm alert thresholds and update the runbook",
            " across all regions before the maintenance window, then notify the on-call rotation",
            " (blocked on vendor support ticket; see thread for the latest workaround and ETA)",
        ]
        # Broad boards a task is often also in, shown in the "Also in" column.
        boards = ["Company Goals", "Team Roadmap", "Engineering Backlog", "Q4 Planning"]
        people = ["Alice", "Bob", "Carol", "Dan"]
        comments = [
            "Reproduced on staging, looks like a timeout in the auth proxy.",
            "Can we get this done before the release freeze?",
            "PR is up, would appreciate a review.",
            "Moved the due date, waiting on the vendor.",
            "Done on my side, please verify.",
        ]
        descriptions = [
            "Users report intermittent failures. Collect logs from the last week, find the common "
            "pattern and propose a fix.\n\nAcceptance: no failures in 7 days of monitoring.",
            "Follow-up from the last incident review.\n\n- update the runbook\n- add an alert\n"
            "- share findings in the weekly sync",
            "",
        ]
        now = datetime.now()

        def memberships(project: str) -> list[str]:
            return [*rnd.sample(boards, rnd.choice([0, 0, 1, 2, 3])), project]

        def events(updated: datetime) -> list[dict]:
            count = rnd.randint(1, 4)
            out, t = [], updated - timedelta(hours=rnd.randint(4, 30) * count)
            for _ in range(count):
                t += timedelta(hours=rnd.randint(2, 30), minutes=rnd.randint(0, 59))
                what = rnd.choice(["comment", "comment", "task assigned", "task due date changed",
                                   "added follower"])
                out.append({"time": min(t, updated).timestamp(), "who": rnd.choice(people),
                            "what": what, "text": rnd.choice(comments) if what == "comment" else ""})
            return out

        self.items = {}
        for i in range(n):
            kind = rnd.choices(["task", "added to project", "conversation", "daily summary"],
                               weights=[14, 4, 1, 1])[0]
            updated = now - timedelta(hours=rnd.randint(1, 24 * 60))
            if kind == "daily summary":
                title, project, projects = f"Daily summary for {updated.date()}", "(daily summaries)", []
            else:
                title = f"{rnd.choice(verbs)} {rnd.choice(things)}" + (
                    rnd.choice(long_tails) if rnd.random() < 0.25 else "")
                project = rnd.choice(project_names)
                projects = memberships(project)
            task = kind in ("task", "added to project")
            mine = task and rnd.random() < 0.3
            self.items[str(i)] = InboxItem(
                id=str(i),
                title=title,
                project=project,
                projects=projects,
                kind=kind,
                updated=updated,
                completed=updated if task and rnd.random() < 0.2 else None,
                url=f"https://app.asana.com/1/0/project/0/task/{1000 + i}",
                assignee="Me" if mine else (rnd.choice([None, *people]) if task else None),
                mine=mine,
                description=rnd.choice(descriptions) if task else "",
                events=events(updated) if kind != "daily summary" else [
                    {"time": updated.timestamp(), "who": None, "what": "daily summary", "text": ""}],
                due=(now.replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=rnd.randint(-10, 20))) if task and rnd.random() < 0.5 else None,
            )

    def list_items(self, progress=None) -> list[InboxItem]:
        return list(self.items.values())

    def archive(self, ids: list[str]) -> None:
        for i in ids:
            self.items.pop(i, None)


SETTINGS = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "asana-inbox" / "settings.json")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value) -> None:
    settings = load_settings()
    if settings.get(key) == value:
        return
    settings[key] = value
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError:
        pass  # not worth failing the UI over


PROJECT_WIDTH = 30  # max chars for the Project column
TITLE_WIDTH = 72  # max chars for the Title column


def ellipsize(text: str, width: int = PROJECT_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


KINDS = {
    "TaskNotificationThread": "task",
    "TaskAddedToPotNotificationThread": "added to project",
    "ConversationNotificationThread": "conversation",
    "DailySummaryNotificationThread": "daily summary",
    "OverdueTasksReminderThread": "overdue reminder",
    "ActionRequestNotificationThread": "access request",
    "PotNotificationThread": "project",
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
        return {"conversation": "(conversations)", "daily summary": "(daily summaries)",
                "overdue reminder": "(overdue reminders)"}.get(
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
                assignee=it.get("assignee"),
                mine=it.get("mine", False),
                due=(datetime.fromtimestamp(it["due_time"]) if it.get("due_time")
                     else datetime.combine(date.fromisoformat(it["due_date"]), datetime.min.time())
                     if it.get("due_date") else None),
                due_has_time=bool(it.get("due_time")),
                description=it.get("description", ""),
                events=it.get("events", []),
            ))
        return items

    def archive(self, ids: list[str]) -> None:
        self.web.archive(ids)


SORTS = {
    "project": lambda it: (it.project.lower(), it.title.lower()),
    "title": lambda it: (it.title.lower(), it.project.lower()),
    "newest": lambda it: -it.updated.timestamp(),
    "oldest": lambda it: it.updated.timestamp(),
    # soonest/most overdue first; no due date last, then newest activity
    "due": lambda it: (it.due is None, it.due or datetime.max, -it.updated.timestamp()),
}


class RegexPrompt(ModalScreen[tuple[str, str] | None]):
    """Ask for a regex; returns (pattern, mode) where mode is "mark", "unmark" or "filter"."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    PLACEHOLDERS = {
        "mark": "Mark shown items matching regex (project, title, other projects, kind, assignee, "
                "'done', 'mine')",
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


class Detail(ModalScreen[None]):
    """Read-only view of one item: metadata, description, and its notifications."""

    BINDINGS = [
        Binding("escape,q,enter", "dismiss(None)", "Close"),
        Binding("y", "copy_url", "Copy URL"),
    ]

    def __init__(self, item: InboxItem):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        it = self.item
        t = Text()
        t.append(it.title + "\n", style="bold")
        meta = [("Kind", it.kind), ("Projects", ", ".join(it.projects) or "-"),
                ("Assignee", ("me" if it.mine else it.assignee) or "-")]
        if it.due:
            meta.append(("Due", it.due_text()))
        if it.completed:
            meta.append(("Completed", it.completed.strftime("%Y-%m-%d %H:%M")))
        meta.append(("Link", it.url or "-"))
        for k, v in meta:
            t.append(f"{k:>10}: ", style="dim")
            t.append(v if isinstance(v, Text) else str(v))
            t.append("\n")
        if it.description.strip():
            t.append("\nDescription\n", style="bold underline")
            t.append(it.description.strip() + "\n")
        if it.events:
            t.append("\nNotifications\n", style="bold underline")
            for e in it.events:
                when = datetime.fromtimestamp(e["time"]).strftime("%Y-%m-%d %H:%M") if e["time"] else "?"
                t.append(f"{when}  ", style="dim")
                t.append(e['who'] or 'Asana', style="bold")
                t.append(f" · {e['what']}\n")
                if e["text"].strip():
                    t.append("    " + e["text"].strip().replace("\n", "\n    ") + "\n")
        with VerticalScroll(id="detail"):
            yield Static(t)  # plain Text: no markup interpretation of task content

    def action_copy_url(self) -> None:
        if self.item.url:
            self.app.copy_to_clipboard(self.item.url)
            self.notify(f"Copied {self.item.url}")


class Confirm(ModalScreen[bool]):
    # Enter confirms (Archive has focus); Tab to Cancel; arrows/PgUp/PgDn scroll the list.
    AUTO_FOCUS = "#yes"
    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("up", "scroll('scroll_up')", show=False),
        Binding("down", "scroll('scroll_down')", show=False),
        Binding("pageup", "scroll('scroll_page_up')", show=False),
        Binding("pagedown", "scroll('scroll_page_down')", show=False),
    ]

    def action_scroll(self, how: str) -> None:
        for lst in self.query("#confirm-list"):
            getattr(lst, how)(animate=False)

    def __init__(self, message: str, details: Text | None = None):
        super().__init__()
        self.message, self.details = message, details

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.message, id="confirm-msg")
            if self.details is not None:
                with VerticalScroll(id="confirm-list", can_focus=False):
                    yield Static(self.details)
            with Horizontal(id="confirm-buttons"):
                yield Button("Archive", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class InboxApp(App):
    CSS = """
    RegexPrompt, Confirm, Detail { align: center middle; }
    #detail { width: 90%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    RegexPrompt Input { width: 70%; }
    #confirm { width: 100; max-width: 90%; height: auto; max-height: 90%; padding: 1 2;
               border: thick $error; background: $surface; }
    #confirm-msg { width: 100%; text-style: bold; margin-bottom: 1; }
    #confirm-list { height: auto; max-height: 60vh; margin-bottom: 1; }
    #confirm-buttons { height: auto; width: 100%; }
    #confirm-buttons Button { margin-right: 2; }
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
        Binding("w", "toggle_truncate", "Full width"),
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
        self.truncate: bool = load_settings().get("truncate", True)

    @staticmethod
    def match_text(it: InboxItem) -> str:
        done = "done" if it.completed else ""
        mine = "mine" if it.mine else ""
        return (f"{it.project} {it.title} {' '.join(it.projects)} {it.kind} {done} "
                f"{it.assignee or ''} {mine}")

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
        # Theme (picked via ctrl+p → "Change theme") persists across runs.
        saved = load_settings().get("theme")
        if saved in self.available_themes:
            self.theme = saved
        self.theme_changed_signal.subscribe(self, lambda theme: save_setting("theme", theme.name))
        table = self.query_one(DataTable)
        # Fixed width: an auto-sized empty column shrinks to 0 and in-place marks become invisible.
        self.mark_col = table.add_column("", width=1)
        table.add_column("Project")
        table.add_column("Done", width=4)
        table.add_columns("Title", "Assignee", "Due", "Kind", "Updated", "Also in")
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

    SCROLLOFF = 3  # rows of context kept above/below the cursor

    def refresh_marks(self) -> None:
        """Update just the mark column in place: no rebuild, so the view doesn't move."""
        table = self.query_one(DataTable)
        for row_key in table.rows:
            want = "●" if row_key.value in self.marked else ""
            if table.get_cell(row_key, self.mark_col) != want:
                table.update_cell(row_key, self.mark_col, want)
        self.update_status(self.visible())

    def keep_context(self) -> None:
        """Vim-style scrolloff: keep SCROLLOFF rows visible around the cursor."""
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        row = table.cursor_row
        visible = table.scrollable_content_region.height - (1 if table.show_header else 0)
        if visible <= 2 * self.SCROLLOFF + 1:
            return
        top = table.scroll_y
        if row > top + visible - 1 - self.SCROLLOFF:
            table.scroll_to(y=row - visible + 1 + self.SCROLLOFF, animate=False)
        elif row < top + self.SCROLLOFF:
            table.scroll_to(y=max(0, row - self.SCROLLOFF), animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.call_after_refresh(self.keep_context)

    def refresh_table(self) -> None:
        """Full rebuild (sort/filter/load/archive); keeps cursor item and scroll offset."""
        table = self.query_one(DataTable)
        cursor_id = self.current_id()
        scroll_y = table.scroll_y
        table.clear()
        for col in table.columns.values():  # clear() keeps old widths; let columns shrink again
            if col.auto_width:
                col.content_width = col.label.cell_len
        prev_project = None
        shown = self.visible()
        for it in sorted(shown, key=SORTS[self.sort]):
            project = it.project if self.sort != "project" or it.project != prev_project else ""
            prev_project = it.project
            table.add_row(
                "●" if it.id in self.marked else "",
                self.cut(project, PROJECT_WIDTH),
                "✓" if it.completed else "",
                Text(self.cut(it.title, TITLE_WIDTH), style="bold" if it.mine else ""),
                Text("★ me", style="bold yellow") if it.mine else (it.assignee or ""),
                it.due_text(),
                it.kind,
                it.updated.strftime("%Y-%m-%d %H:%M"),
                ", ".join(p for p in it.projects if p != it.project),  # last column: never cut
                key=it.id,
            )
        table.scroll_to(y=scroll_y, animate=False)
        if cursor_id in {it.id for it in shown}:
            table.move_cursor(row=table.get_row_index(cursor_id), scroll=False)
        self.update_status(shown)

    def cut(self, text: str, width: int) -> str:
        return ellipsize(text, width) if self.truncate else text

    def action_toggle_truncate(self) -> None:
        self.truncate = not self.truncate
        save_setting("truncate", self.truncate)
        self.refresh_table()
        self.notify("Long titles/projects shortened" if self.truncate else "Showing full titles/projects")

    def update_status(self, shown: list[InboxItem]) -> None:
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
        self.refresh_marks()
        self.query_one(DataTable).action_cursor_down()

    def action_toggle_project(self) -> None:
        cur = self.current_item()
        if cur is None:
            return
        ids = {it.id for it in self.visible() if it.project == cur.project}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_marks()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: show its details."""
        item = next((it for it in self.items if it.id == event.row_key.value), None)
        if item is not None:
            self.push_screen(Detail(item))

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
        self.refresh_marks()

    def action_toggle_all(self) -> None:
        ids = {it.id for it in self.visible()}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_marks()

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
                self.refresh_table()
            else:
                ids = {it.id for it in self.visible() if rx.search(self.match_text(it))}
                unmark = mode == "unmark"
                self.marked = self.marked - ids if unmark else self.marked | ids
                self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} items")
                self.refresh_marks()

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

        shown_ids = {it.id for it in self.visible()}
        marked = sorted((it for it in self.items if it.id in self.marked), key=SORTS[self.sort])
        hidden = sum(1 for it in marked if it.id not in shown_ids)
        details = Text()
        for it in marked:
            details.append(f"{it.project}  ", style="dim")
            details.append(it.title)
            if it.id not in shown_ids:
                details.append("  (hidden by filter)", style="italic yellow")
            details.append("\n")
        details.rstrip()
        note = f" ({hidden} hidden by the filter)" if hidden else ""
        self.push_screen(Confirm(f"Archive these {len(marked)} items?{note}", details), do)

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
