"""LLM prompts, loaded from a versioned YAML file (PROMPTS_FILE).

Loaded once at startup and validated, so a missing or empty prompt stops the
agent from starting instead of failing on the first claim.
"""
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PromptSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tc_validation: str = Field(min_length=20)
    field_match: str = Field(min_length=20)
    history: str = Field(min_length=20)
    gift_assignment: str = Field(min_length=20)
    summary: str = Field(min_length=20)


class Prompts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    prompts: PromptSet


class PromptsError(RuntimeError):
    pass


def load_prompts(path: str | Path) -> Prompts:
    path = Path(path)
    if not path.is_file():
        raise PromptsError(f"prompts file not found: {path.resolve()}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data.get("version"), (int, float)):
            data["version"] = str(data["version"])
        prompts = Prompts.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise PromptsError(f"invalid prompts file {path}: {e}") from e
    # strip trailing newlines from YAML block scalars
    return prompts.model_copy(update={"prompts": PromptSet(
        **{k: v.strip() for k, v in prompts.prompts.model_dump().items()})})
