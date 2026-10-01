"""Select only this version's notes while retaining the repository changelog."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def current_notes(changelog: str, version: str) -> str:
    headings = list(re.finditer(r'^#{1,2}\s+v(\d+\.\d+\.\d+)\b[^\n]*$', changelog, re.M))
    for index, heading in enumerate(headings):
        if heading.group(1) == version:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
            notes = changelog[heading.start():end].strip()
            notes = re.sub(r'\n\s*---\s*$', '', notes).rstrip()
            return notes + '\n'
    raise ValueError(f'Release notes missing for v{version}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    import sys
    sys.path.insert(0, str(root))
    from app.diagnostics import VERSION
    notes = current_notes((root / 'RELEASE_NOTES.md').read_text(encoding='utf-8'), VERSION)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding='utf-8')


if __name__ == '__main__':
    main()
