"""Validate distributable skills, required release versions and stable UUIDs."""
import re
import sys
from pathlib import Path
from uuid import UUID

import yaml
from skills_ref import validate


def validate_repository(root):
    errors, identities = [], {}
    manifests = [p for p in Path(root).rglob("SKILL.md") if not any(part.startswith('.') for part in p.relative_to(root).parts)]
    if not manifests:
        return ["未找到 SKILL.md"]
    for manifest in manifests:
        label = str(manifest.relative_to(root))
        errors.extend(f"{label}: {error}" for error in validate(manifest.parent))
        try:
            text = manifest.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n([\s\S]*?)\n---", text)
            metadata = yaml.safe_load(match[1]).get("metadata", {}) if match else {}
            version = metadata.get("version")
            if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
                errors.append(f'{label}: metadata.version 必须声明三段式版本字符串，例如 "1.0.0"')
            identity = metadata.get("skill_id")
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", identity):
                raise ValueError("metadata.skill_id 必须声明固定 UUID")
            identity = str(UUID(identity))
            if identity in identities:
                errors.append(f"{label}: 技能ID与 {identities[identity]} 重复：{identity}")
            else:
                identities[identity] = label
        except (ValueError, AttributeError, yaml.YAMLError) as error:
            errors.append(f"{label}: {error}")
    return errors


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    errors = validate_repository(root)
    print("\n".join(errors) if errors else "技能结构、中文标识、版本号和固定ID唯一性校验通过。")
    raise SystemExit(bool(errors))
