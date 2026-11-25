"""Bind the CLI to the Core distribution built from the same Git identity."""

from hatchling.metadata.plugin.interface import MetadataHookInterface


class CoreDependencyHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        # Hatch resolves the VCS version before running metadata hooks. A
        # standalone sdist rebuild restores both fields from PKG-INFO instead.
        metadata["dependencies"] = [
            f"wenyi-core=={metadata['version']}",
            *self.config["dependencies"],
        ]
