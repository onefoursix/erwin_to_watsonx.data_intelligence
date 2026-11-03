#!/usr/bin/env python3
"""Copies the shared sources into the skills that use them, so each skill folder works on its own.

Edit the files in shared/, then run:  python3 tools/sync.py   (or --check to fail if a copy is out of date)
"""
import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
COPIES = {
    'shared/erwin2postgres.py': ['skills/erwin-model-export/scripts'],
    'shared/erwinbin.py': ['skills/erwin-model-export/scripts'],
    'shared/wxdi_common.py': ['skills/wxdi-metadata-import/scripts', 'skills/erwin-wxdi-governance/scripts'],
}


def main():
    check = '--check' in sys.argv
    stale = []
    for src, dests in COPIES.items():
        for d in dests:
            dst = os.path.join(ROOT, d, os.path.basename(src))
            if os.path.exists(dst) and filecmp.cmp(os.path.join(ROOT, src), dst, shallow=False):
                continue
            stale.append(os.path.relpath(dst, ROOT))
            if not check:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(ROOT, src), dst)
    if check and stale:
        sys.exit('out of date: ' + ', '.join(stale) + ' (run python3 tools/sync.py)')
    print(('stale: ' if check else 'copied: ') + (', '.join(stale) or 'nothing; all copies are current'))


if __name__ == '__main__':
    main()
