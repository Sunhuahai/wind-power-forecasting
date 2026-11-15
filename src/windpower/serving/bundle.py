"""Versioned model bundle: manifest plus component artifacts with checksums."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from windpower.serving.compose import compose as compose_tree
from windpower.serving.compose import validate_tree

REQUIRED = ('bundle_version', 'created_at', 'protocol', 'components', 'compose')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Bundle:
    root: Path
    manifest: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.manifest['bundle_version']

    @property
    def components(self) -> dict:
        return self.manifest['components']

    def component_ids(self) -> list[str]:
        return sorted(self.components)

    def verify(self) -> dict:
        """Check every declared artifact exists and matches its recorded checksum."""
        report = {}
        for name, spec in self.components.items():
            files = spec.get('files', {})
            missing = [key for key, meta in files.items()
                       if not (self.root / meta['path']).exists()]
            mismatched = [key for key, meta in files.items()
                          if (self.root / meta['path']).exists()
                          and sha256(self.root / meta['path']) != meta['sha256']]
            report[name] = dict(files=len(files), missing=missing, mismatched=mismatched)
        return report

    def validate(self) -> None:
        missing = [key for key in REQUIRED if key not in self.manifest]
        if missing:
            raise ValueError(f'manifest missing keys: {missing}')
        validate_tree(self.manifest['compose'], set(self.components))

    def compose(self, values: dict) -> 'np.ndarray':
        self.validate()
        return compose_tree(self.manifest['compose'], values)


def load_bundle(path: str | Path) -> Bundle:
    root = Path(path)
    manifest_path = root / 'manifest.json'
    if not manifest_path.exists():
        raise FileNotFoundError(f'no manifest at {manifest_path}')
    bundle = Bundle(root=root, manifest=json.loads(manifest_path.read_text()))
    bundle.validate()
    return bundle
