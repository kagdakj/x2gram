"""Reusable widgets."""

from typing import Iterable, List, Optional, Sequence, Set, Tuple

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable

from ..models import PostStatus

STATUS_STYLE = {
    PostStatus.QUEUED: "cyan",
    PostStatus.UPLOADING: "bold yellow",
    PostStatus.UPLOADED: "green",
    PostStatus.FAILED: "bold red",
    PostStatus.SKIPPED: "dim",
}


def status_text(status: Optional[PostStatus], fallback: str = "new") -> Text:
    if status is None:
        return Text(fallback, style="italic dim")
    return Text(status.value, style=STATUS_STYLE[status])


Row = Tuple[str, Sequence]  # (key, cells without the checkbox column)


class SelectableTable(DataTable):
    """Row table with a checkbox column. Space toggles, `a` selects all / none."""

    BINDINGS = [
        Binding("space", "toggle_row", "Select", show=True),
        Binding("a", "toggle_all", "All/None", show=True),
    ]

    class SelectionChanged(Message):
        def __init__(self, table: "SelectableTable") -> None:
            super().__init__()
            self.table = table

        @property
        def control(self) -> "SelectableTable":
            return self.table

    def __init__(self, columns: Sequence[str], selectable: bool = True, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._columns = list(columns)
        self.selectable = selectable
        self.selected: Set[str] = set()
        self.keys: List[str] = []

    def on_mount(self) -> None:
        if self.selectable:
            self.add_column(" ", key="_sel", width=3)
        for name in self._columns:
            self.add_column(name, key=name)

    @property
    def cursor_key(self) -> Optional[str]:
        if 0 <= self.cursor_row < len(self.keys):
            return self.keys[self.cursor_row]
        return None

    def target_keys(self) -> List[str]:
        """Selected rows in table order, or the row under the cursor if none selected."""
        if self.selected:
            return [k for k in self.keys if k in self.selected]
        return [self.cursor_key] if self.cursor_key else []

    def set_rows(self, rows: Iterable[Row]) -> None:
        """Replace content, keeping cursor position and selection where possible."""
        current = self.cursor_key
        rows = list(rows)
        self.clear()
        self.keys = [key for key, _ in rows]
        self.selected &= set(self.keys)
        for key, cells in rows:
            prefix = [self._mark(key)] if self.selectable else []
            self.add_row(*prefix, *cells, key=key)
        if current in self.keys:
            self.move_cursor(row=self.keys.index(current), animate=False)

    def _mark(self, key: str) -> Text:
        return Text("●", style="bold green") if key in self.selected else Text("○", style="dim")

    def _refresh_mark(self, key: str) -> None:
        self.update_cell(key, "_sel", self._mark(key))

    def action_toggle_row(self) -> None:
        key = self.cursor_key
        if not self.selectable or key is None:
            return
        self.selected.symmetric_difference_update({key})
        self._refresh_mark(key)
        if self.cursor_row < self.row_count - 1:
            self.move_cursor(row=self.cursor_row + 1)
        self.post_message(self.SelectionChanged(self))

    def action_toggle_all(self) -> None:
        if not self.selectable:
            return
        self.selected = set() if len(self.selected) == len(self.keys) else set(self.keys)
        for key in self.keys:
            self._refresh_mark(key)
        self.post_message(self.SelectionChanged(self))

    def clear_selection(self) -> None:
        self.selected.clear()
        for key in self.keys:
            self._refresh_mark(key)
        self.post_message(self.SelectionChanged(self))
