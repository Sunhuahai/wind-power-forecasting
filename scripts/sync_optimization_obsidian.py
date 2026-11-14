"""Sync successful progress and linked evidence to an explicitly supplied vault project."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault-project', type=Path, required=True)
    args = parser.parse_args()
    vault = args.vault_project
    if not vault.is_dir():
        raise ValueError('Existing Obsidian project directory required')
    note_path = ROOT/'docs/optimization-progress.md'
    note = note_path.read_text()
    files = {note_path, ROOT/'docs/optimization-progress.json'}
    replacements = {}
    for link in re.findall(r'\]\(([^)]+)\)',note):
        if '://' in link:
            continue
        source = (note_path.parent/link).resolve()
        relative = source.relative_to(ROOT)
        if not source.is_file():
            raise FileNotFoundError(source)
        files.add(source)
        replacements[link] = f'优化进展/{relative.as_posix()}'
    for extension in ['png','pdf','svg','csv']:
        files.add(ROOT/f'docs/figures/optimization-progress.{extension}')
    registry = json.loads((ROOT/'docs/optimization-progress.json').read_text())
    for milestone in registry['milestones']:
        source = ROOT/milestone['source']
        files.add(source)
        if 'paired' in milestone:
            files.add(ROOT/milestone['paired'])
        for name in ['weights.json','protocol.json','verification.json','oof_audit.json']:
            path = source.parent/name
            if path.exists():
                files.add(path)
    manifest = []
    for source in sorted(files):
        relative = source.relative_to(ROOT)
        target = vault/'优化进展'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Copy mismatch: {relative}')
        manifest.append(dict(source=str(relative),sha256=digest))
    for old,new in replacements.items():
        note=note.replace(f']({old})',f']({new})')
    for link in re.findall(r'\]\(([^)]+)\)',note):
        if '://' not in link and not (vault/link).exists():
            raise FileNotFoundError(link)
    target = vault/'风电预测优化技巧与验证记录.md'
    target.write_text(note,encoding='utf-8')
    if target.read_text()!=note:
        raise RuntimeError('Note verification failed')
    (vault/'优化进展/sync-manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False))
    print(f'Synced {len(registry["milestones"])} successful milestones and {len(files)} verified evidence files.')


if __name__=='__main__':
    main()
