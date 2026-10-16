#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=0.80", "httpx>=0.27", "websockets>=13", "certifi", "browser-cookie3>=0.20"]
# ///
"""Regenerate the README screenshots (SVG) from the fake backend: ./docs/screenshots.py"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
sys.path.insert(0, str(DOCS.parent))
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()  # ignore the user's saved theme/settings

import asana_inbox as ai  # noqa: E402
from textual.widgets import DataTable  # noqa: E402

SIZE = (200, 36)


async def main() -> None:
    app = ai.InboxApp(ai.FakeBackend())
    async with app.run_test(size=SIZE) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)

        async def shot(name: str, theme: str) -> None:
            app.theme = theme
            await pilot.pause()
            app.save_screenshot(filename=f"{name}.svg", path=str(DOCS))
            print(f"docs/{name}.svg ({theme})")

        # 1. Default view: grouped by project, a few rows marked.
        await pilot.press("down", "space", "space", "down", "down", "space")
        await shot("main", "solarized-dark")

        # 2. Sorted by due date (project → title → newest → oldest → due).
        await pilot.press(*["s"] * 4)
        table.move_cursor(row=0)  # the view follows the selected row when re-sorting
        await shot("sort-due", "textual-dark")
        await pilot.press("s")  # back to project

        # 3. Filter: only my tasks, all of them marked with `a`.
        await pilot.press("f", *"mine", "enter", "a")
        await shot("filter", "textual-dark")
        await pilot.press("a", "escape")

        # 4. Task details (Enter) on a task with several projects, a due date and comments.
        pick = next(it for it in app.visible()
                    if len(it.projects) >= 3 and it.due and it.description and it.assignee
                    and sum(e["text"] != "" for e in it.events) >= 2)
        table.move_cursor(row=table.get_row_index(pick.id))
        await pilot.press("enter")
        await shot("details", "solarized-dark")
        await pilot.press("escape")

        # 5. Archive confirmation: also mark a whole project with `p`.
        await pilot.press("end", "p", "x")
        await shot("archive", "solarized-dark")


if __name__ == "__main__":
    asyncio.run(main())
