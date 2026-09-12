"""Operator-controlled manifests, loaded atomically; no executable YAML plugins."""
import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from .errors import CoreError
from .models import LabDefinition


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("Manifest mapping keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class LabRegistry:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._labs: dict[str, LabDefinition] = {}

    def reload(self) -> int:
        if not self.root.is_dir():
            raise CoreError("LAB_ROOT_MISSING", "Configured lab directory does not exist", 503)
        found = {}
        for path in sorted(self.root.glob("*/lab.yaml")):
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    raise ValueError("Manifest symlinks are not supported")
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("Manifest outside configured root")
                if path.stat().st_size > 262144:
                    raise ValueError("Manifest exceeds 256 KiB")
                raw = path.read_text(encoding="utf-8")
                if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                       for t in yaml.scan(raw)):
                    raise ValueError("YAML anchors and aliases are not supported")
                lab = LabDefinition.model_validate(yaml.load(raw, Loader=UniqueLoader))
                if path.parent.name != lab.id:
                    raise ValueError("Lab directory must equal manifest id")
                if lab.id in found:
                    raise ValueError("Duplicate lab id")
                found[lab.id] = lab
            except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError, RecursionError) as exc:
                # File names are operator-owned. Never include manifest values in the error.
                raise CoreError("INVALID_LAB", f"Invalid manifest: {path.parent.name}/lab.yaml", 422) from exc
        self._labs = found
        return len(found)

    def get(self, lab_id: str) -> LabDefinition:
        lab = self._labs.get(lab_id)
        if lab is None:
            raise CoreError("LAB_NOT_FOUND", "Lab not found", 404)
        return lab.model_copy(deep=True)

    def all(self) -> list[LabDefinition]:
        return [self.get(lab_id) for lab_id in sorted(self._labs)]

