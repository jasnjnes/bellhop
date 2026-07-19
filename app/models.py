from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FileChange(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str | None = None
    content_base64: str | None = None
    delete: bool = False
    mode: Literal["100644", "100755"] = "100644"

    @model_validator(mode="after")
    def validate_action(self):
        values = sum(
            [
                self.content is not None,
                self.content_base64 is not None,
                self.delete,
            ]
        )
        if values != 1:
            raise ValueError("Exactly one of content, content_base64, or delete=true is required.")
        return self


class RepositoryVisibility(BaseModel):
    private: bool = True


class CommitResult(BaseModel):
    owner: str
    repository: str
    branch: str
    previous_head: str
    commit_sha: str
    commit_url: str
    changed_paths: list[str]
