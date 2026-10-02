"""Keyboard-driven selection without additional terminal dependencies."""

from __future__ import annotations

import os
import select as io_select
import sys

import typer
from rich.console import Console
from rich.live import Live
from rich.text import Text


def _read_key() -> str:
    """Read a key and always restore terminal attributes, including on cancellation."""
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        return key + msvcrt.getwch() if key in {"\x00", "\xe0"} else key
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor, when=termios.TCSANOW)
        key = os.read(descriptor, 1).decode()
        if key == "\x1b":
            while io_select.select([descriptor], [], [], 0.05)[0]:
                key += os.read(descriptor, 1).decode()
                if key[-1].isalpha():
                    break
        return key
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def select(console: Console, label: str, labels: list[str], *, default: int = 0) -> int | None:
    """Select with arrow keys in a terminal, or exact labels in redirected input."""
    if not labels:
        return None
    index = min(max(default, 0), len(labels) - 1)
    if not (console.is_terminal and sys.stdin.isatty()):
        for item in labels:
            console.print(f"  {item}", markup=False)
        while True:
            console.print(f"{label} [{labels[index]}]: ", end="", markup=False)
            answer = sys.stdin.readline()
            if not answer:
                return None
            answer = answer.strip()
            if not answer:
                return index
            if answer in labels:
                return labels.index(answer)
            console.print("Enter an exact label, or use command-line flags.")

    def render() -> Text:
        text = Text(f"{label} (↑/↓ to move, Enter to select, Ctrl-C to cancel)\n")
        start = max(0, min(index - 5, len(labels) - 10))
        for position in range(start, min(start + 10, len(labels))):
            active = position == index
            text.append(
                f"{'❯' if active else ' '} {labels[position]}\n",
                style="bold cyan" if active else "",
            )
        text.append(f"{index + 1}/{len(labels)}", style="dim")
        return text

    with Live(render(), console=console, auto_refresh=False, transient=True) as live:
        while True:
            key = _read_key()
            if key in {"\x1b[A", "\x1bOA", "\xe0H", "k"}:
                index = (index - 1) % len(labels)
            elif key in {"\x1b[B", "\x1bOB", "\xe0P", "j"}:
                index = (index + 1) % len(labels)
            elif key in {"\r", "\n"}:
                break
            elif key in {"\x03", "\x04"}:
                raise typer.Abort()
            live.update(render(), refresh=True)
    console.print(f"{label}: {labels[index]}", markup=False)
    return index
