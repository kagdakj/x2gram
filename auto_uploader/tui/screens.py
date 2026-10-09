"""Modal dialogs."""

from typing import Optional, Sequence, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static

Choice = Tuple[str, str, str]  # (label, value, variant)


class ConfirmScreen(ModalScreen[Optional[str]]):
    """Returns the chosen value, or None on cancel/escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, choices: Sequence[Choice]) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self._title, classes="dialog-title")
            yield Static(self._body, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                for label, value, variant in self._choices:
                    yield Button(label, id=f"choice-{value}", variant=variant, name=value)
                yield Button("Cancel", id="choice-cancel", name="")

    def on_mount(self) -> None:
        self.query(Button).first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.name or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP = """\
# Keys

| Key | Action |
|---|---|
| `s` | Start / stop the auto-upload daemon |
| `f` / `F` | Fetch new posts now / deep fetch (backlog) |
| `space` / `a` | Select row / select all-none |
| `u` | Upload selected rows (or the row under the cursor) |
| `r` | Re-queue selected failed/skipped posts |
| `k` | Skip selected posts (never upload) |
| `delete` | Remove selected posts from the list |
| `x` | Stop the running upload batch |
| `1`–`4` | Switch tab |
| `ctrl+r` | Refresh |
| `q` | Quit |

# How it works

1. **Fetch** pulls reposts (and files dropped into the local folder) and puts new ones in the **queue**.
   On the very first run existing reposts are marked *skipped* so your history isn't mass-uploaded.
   Use **deep fetch** to queue the backlog instead.
2. The **daemon** repeats: fetch → upload queued posts (random delay, rate limits) → sleep.
3. Failed posts stay in the list with their error; press `r` to retry.
4. **Sync** compares your Instagram profile with X and lets you pick what's missing.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog help"):
            with VerticalScroll(classes="help-scroll"):
                yield Markdown(HELP)
            yield Button("Close", variant="primary")

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
