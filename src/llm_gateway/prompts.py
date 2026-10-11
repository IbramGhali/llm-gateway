"""Prompt registry: versioned templates referenced by id, so apps do not ship prompt text and
every request records exactly which prompt version produced it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml


@dataclass(frozen=True)
class PromptVersion:
    id: str
    version: int
    system: str
    template: Template

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    def render(self, variables: dict[str, str]) -> str:
        try:
            return self.template.substitute(variables)
        except KeyError as exc:
            raise ValueError(f"prompt {self.ref} needs variable {exc.args[0]}") from exc


class PromptRegistry:
    def __init__(self, directory: Path) -> None:
        self.prompts: dict[str, dict[int, PromptVersion]] = {}
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.prompts[data["id"]] = {
                v["version"]: PromptVersion(
                    data["id"], v["version"], v["system"], Template(v["template"])
                )
                for v in data["versions"]
            }

    def get(self, prompt_id: str, version: int | None = None) -> PromptVersion:
        versions = self.prompts.get(prompt_id)
        if not versions:
            raise LookupError(f"unknown prompt {prompt_id}")
        chosen = version if version is not None else max(versions)
        if chosen not in versions:
            raise LookupError(f"prompt {prompt_id} has no version {chosen}")
        return versions[chosen]
