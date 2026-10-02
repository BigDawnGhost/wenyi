"""Desktop conveniences for interactive OAuth sign-in."""

from __future__ import annotations

import shutil
import subprocess
import sys

from rich.console import Console
from wenyi_core.llm.oauth.flows import DeviceCodePrompt


def copy_device_code(code: str) -> bool:
    """Copy a short-lived device code; never pass credential tokens to the clipboard."""
    candidates = (
        [("pbcopy",)]
        if sys.platform == "darwin"
        else [("clip",)]
        if sys.platform == "win32"
        else [
            ("wl-copy",),
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
        ]
    )
    for command in candidates:
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(
                command,
                input=code,
                text=True,
                check=True,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def print_device_prompt(console: Console, prompt: DeviceCodePrompt, *, copy: bool = True) -> None:
    """Show fallback instructions and copy the device code when supported."""
    console.print(
        f"To sign in to {prompt.display_name}, visit [bold]{prompt.verification_url}[/] "
        f"and enter the code [bold]{prompt.user_code}[/]. "
        f"The code expires in {int(prompt.expires_in)}s."
    )
    if copy and copy_device_code(prompt.user_code):
        console.print("Device code copied to clipboard. Waiting for browser approval...")
    else:
        console.print("Copy the code above. Waiting for browser approval...")
