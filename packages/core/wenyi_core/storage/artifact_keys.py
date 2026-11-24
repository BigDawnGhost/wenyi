"""Lexical artifact naming rules shared by all storage adapters."""


def validate_artifact_key(key: str, *, prefix: bool = False) -> str:
    """Validate a portable run-relative key, returning its original spelling."""
    if not isinstance(key, str) or "\\" in key or "\0" in key or ":" in key:
        raise ValueError("Artifact key must be a portable relative path")
    if prefix and key == "":
        return key
    value = key.removesuffix("/") if prefix else key
    if not value or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("Artifact key must remain within the run")
    return key
