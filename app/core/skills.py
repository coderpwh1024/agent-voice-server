"""Read Nacos skill instructions without executing downloaded code."""

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

import yaml

EMAIL_SKILL_NAME = "send-current-user-email"


VIDEO_SKILL_NAME = "manage-user-videos"


IMAGE_SKILL_NAME = "manage-user-images"


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    digest: str


EmailSkill = SkillDefinition


def read_email_skill(archive: bytes) -> EmailSkill:
    return read_skill(archive, EMAIL_SKILL_NAME)


def read_skill(archive: bytes, name: str) -> SkillDefinition:
    if len(archive) > 1024 * 1024:
        raise ValueError("Skill archive is too large")
    try:
        with ZipFile(BytesIO(archive)) as package:
            entries = package.infolist()
            if len(entries) > 30 or sum(entry.file_size for entry in entries) > 1024 * 1024:
                raise ValueError("Skill archive exceeds resource limits")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                    raise ValueError("Skill archive contains an unsafe path")
            candidates = [
                entry for entry in entries if entry.filename in {"SKILL.md", f"{name}/SKILL.md"}
            ]
            if len(candidates) != 1 or candidates[0].file_size > 32000:
                raise ValueError("Skill must contain one bounded SKILL.md")
            content = package.read(candidates[0]).decode("utf-8")
    except (BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("Invalid skill archive") from exc
    parts = content.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError("SKILL.md requires YAML frontmatter")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or metadata.get("name") != name:
        raise ValueError("Skill name does not match the runtime capability")
    description = metadata.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise ValueError("Skill requires a description of at most 1024 characters")
    if not parts[2].strip():
        raise ValueError("Skill instructions cannot be empty")
    return SkillDefinition(
        name=name,
        description=description.strip(),
        instructions=parts[2].strip(),
        digest=hashlib.sha256(content.encode()).hexdigest(),
    )
