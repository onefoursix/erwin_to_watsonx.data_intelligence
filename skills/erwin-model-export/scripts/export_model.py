#!/usr/bin/env python3
"""Export an erwin model (.erwin) in one go: the interactive viewer page, all of its metadata as one JSON
file, and SQL scripts that create its tables, keys and views.

Usage:
    export_model.py MODEL.erwin --name NAME [--schema SCHEMA] [--dialect postgres|sqlserver|both]
                    [--out-dir DIR] [--title TITLE] [--select TABLE]

Writes into DIR (default: work/<schema>/ in the project home, the folder the other erwin skills read):
    <schema>.metadata.json        everything in the model: tables, columns, keys, relationships, views,
                                  domains, and the governance data (subject-area categories, business terms,
                                  term assignments, classification suggestions, valid values, user-defined
                                  properties). The watsonx.data intelligence skills work from this file.
    <schema>.<dialect>.sql        one per dialect: CREATE SCHEMA, tables, keys, foreign keys, descriptions, views
    <NAME>.diagram.html           the viewer page (publish it as an Artifact, or open it locally)

NAME is the model's name as people know it (e.g. eMovies). It becomes the root glossary category and,
for the governance skill, the catalog name. SCHEMA defaults to NAME in snake_case.
"""
import argparse
import csv
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
FORMAT = 'erwin-model-export/1'


def home():
    """The project home: ERWIN_WXDI_HOME, else the first folder above this script with a .env or work/."""
    if os.environ.get('ERWIN_WXDI_HOME'):
        return os.path.expanduser(os.environ['ERWIN_WXDI_HOME'])
    d = HERE
    while d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, '.env')) or os.path.isdir(os.path.join(d, 'work')):
            return d
        d = os.path.dirname(d)
    return os.getcwd()


def snake(name):
    """The converter's own naming (erwin2postgres.snake): eMovies -> emovies, Publication System -> publication_system."""
    return re.sub(r'[^0-9A-Za-z]+', '_', name).strip('_').lower() or 'unnamed'


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        sys.exit(f'failed: {" ".join(cmd[1:3])}\n{out}')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('model', help='path to the .erwin file')
    ap.add_argument('--name', required=True, help='the model name, e.g. eMovies (root category and catalog name)')
    ap.add_argument('--schema', help='database schema for the SQL (default: the name in snake_case)')
    ap.add_argument('--dialect', choices=['postgres', 'sqlserver', 'both'], default='both')
    ap.add_argument('--out-dir', help='default: work/<schema>/ in the project home')
    ap.add_argument('--title', help='viewer page title (default: "<name> Data Model")')
    ap.add_argument('--select', help='table open in the viewer when it loads')
    a = ap.parse_args()

    model = os.path.abspath(os.path.expanduser(a.model))
    if not os.path.exists(model):
        sys.exit(f'No such model file: {model}')
    schema = a.schema or snake(a.name)
    out = os.path.abspath(os.path.expanduser(a.out_dir)) if a.out_dir else os.path.join(home(), 'work', schema)
    os.makedirs(out, exist_ok=True)
    parser = [sys.executable, '-I', os.path.join(HERE, 'erwin2postgres.py'), model, '--schema', schema]
    dialects = ['postgres', 'sqlserver'] if a.dialect == 'both' else [a.dialect]

    sql = {}
    for d in dialects:
        path = os.path.join(out, f'{schema}.{d}.sql')
        print(run(parser + ['--dialect', d, '--out', path]).splitlines()[0])
        sql[d] = os.path.basename(path)

    with tempfile.TemporaryDirectory() as tmp:
        meta, gloss = os.path.join(tmp, 'meta'), os.path.join(tmp, 'gloss')
        notes = run(parser + ['--out', os.path.join(tmp, 'x.sql'), '--metadata', meta, '--glossary', gloss,
                              '--category', a.name])
        m = json.load(open(os.path.join(meta, 'model.json'), encoding='utf-8'))
        rows = read_csv(os.path.join(gloss, 'glossary.csv'))
        usage = read_csv(os.path.join(gloss, 'reference_data_usage.csv'))
        refdata = {os.path.splitext(os.path.basename(p))[0]: read_csv(p)
                   for p in sorted(glob.glob(os.path.join(gloss, 'reference_data', '*.csv')))}
        doc = {
            'format': FORMAT,
            'name': a.name,
            'schema': schema,
            'model_file': model,
            'exported_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'erwin_model': m.get('model'),
            'sql': sql,
            'entities': m.get('entities', []),
            'relationships': m.get('relationships', []),
            'views': m.get('views', []),
            'domains': m.get('domains', []),
            'glossary': {
                'root_category': a.name,
                # rows in the watsonx.data intelligence glossary CSV format (Name, Artifact Type, Category,
                # Description, Abbreviations, Secondary Categories); category paths use "Root >> Child"
                'rows': rows,
                'extra_secondary_categories': read_csv(os.path.join(gloss, 'extra_secondary_categories.csv')),
                'term_assignments': read_csv(os.path.join(gloss, 'term_assignments.csv')),
                'classification_suggestions': read_csv(os.path.join(gloss, 'classification_suggestions.csv')),
                'custom_properties': read_csv(os.path.join(gloss, 'custom_properties.csv')),
                'reference_data': refdata,
                'reference_data_usage': usage,
            },
            # the parser's review notes, minus its summary lines (they name the temporary folder)
            'parser_notes': [l.strip() for l in notes.splitlines()[1:] if l.strip() and tmp not in l],
        }
    meta_path = os.path.join(out, f'{schema}.metadata.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    g = doc['glossary']
    n_cat = sum(r['Artifact Type'] == 'category' for r in g['rows'])
    n_term = sum(r['Artifact Type'] == 'glossary_term' for r in g['rows'])
    print(f'metadata: {len(doc["entities"])} tables, {len(doc["views"])} views, {len(doc["relationships"])} '
          f'relationships, {n_cat} categories, {n_term} terms, {len(g["term_assignments"])} term assignments, '
          f'{len(g["classification_suggestions"])} classification suggestions -> {meta_path}')

    html = os.path.join(out, f'{a.name}.diagram.html')
    cmd = [sys.executable, '-I', os.path.join(HERE, 'erwin_diagram.py'), model, '--out', html,
           '--title', a.title or f'{a.name} Data Model']
    if a.select:
        cmd += ['--select', a.select]
    print(run(cmd).splitlines()[-1])
    for n in doc['parser_notes']:
        print('  note:', n)


if __name__ == '__main__':
    main()
