#!/usr/bin/env python3
"""Load an erwin model's governance metadata into watsonx.data intelligence (SaaS), from the
<schema>.metadata.json that erwin-model-export writes, onto the tables and views wxdi-metadata-import put
in a project.

Steps, in order (run-all does them in sequence; each is safe to re-run, and --dry-run shows what it would do):
  check           read-only: metadata file, project assets, catalog, glossary, classifications, workflow
  glossary        the top-level category (the model's name), one sub-category per subject area, the business
                  terms, and the extra secondary categories once the terms are published
  approve         complete this account user's Publish tasks for the model's categories and terms (--yes)
  publish         publish the import's connection and the schema's tables and views to the catalog
  assign-terms    put the model's terms on the tables, columns and view columns (catalog, and project)
  classify        put classifications (PII etc.) on the columns, from the model's classification suggestions
  status          what is published, pending, assigned
  run-all         all of the above; approves only with --approve
"""
import base64
import csv
import json
import os
import sys
import time

from wxdi_common import (GLOSSARY_SEP, categories_under, classification_ids, common_args, connect, containers, ctx,
                         find_connection, find_container, has_relation, ok, published_terms, schema_assets, search,
                         work_dir)

TERM_FIELDS = ['Name', 'Artifact Type', 'Category', 'Description', 'Abbreviations', 'Secondary Categories']
LEVELS = ['review', 'medium', 'high']
# the model's classification names -> names wxdi accounts also use for them
CLASS_ALIASES = {'Personally Identifiable Information': ['PII'], 'PII': ['Personally Identifiable Information']}
DERIVED_TAG = 'classifications-derived-not-modelled'


# ---------- the model ----------

class Model:
    def __init__(self, path):
        self.path = path
        self.doc = json.load(open(path, encoding='utf-8'))
        if not str(self.doc.get('format', '')).startswith('erwin-model-export/'):
            sys.exit(f'{path} is not an erwin-model-export metadata file')
        self.name, self.schema = self.doc['name'], self.doc['schema']
        g = self.doc['glossary']
        self.root = g['root_category']
        self.rows = g['rows']
        self.categories = [r for r in self.rows if r['Artifact Type'] == 'category']
        self.terms = [r for r in self.rows if r['Artifact Type'] == 'glossary_term']
        self.assignments = g['term_assignments']
        self.suggestions = g['classification_suggestions']
        self.extra_secondary = g['extra_secondary_categories']
        self.category_names = {r['Name'] for r in self.categories}
        self.term_names = {r['Name'] for r in self.terms}

    def category_path(self, r):
        return r['Category'] + GLOSSARY_SEP + r['Name'] if r['Category'] else r['Name']

    def classifications(self, min_confidence):
        """{term name: classification} for suggestions at or above min_confidence."""
        floor = LEVELS.index(min_confidence)
        return {r['Name']: r['Classifications'] for r in self.suggestions
                if r.get('Classifications') and LEVELS.index(r.get('confidence') or 'high') >= floor}


def load_model(a):
    path = a.metadata or os.path.join(work_dir(a.schema), f'{a.schema}.metadata.json')
    if not os.path.exists(path):
        sys.exit(f'No metadata file at {path}: run erwin-model-export first, or pass --metadata')
    m = Model(path)
    a.schema = a.schema or m.schema
    a.catalog = a.catalog or m.name
    return m


def iam_id(api):
    c = api.token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(c + '=' * (-len(c) % 4))).get('iam_id')


def write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


# ---------- glossary ----------

def import_rows(api, m, kind, rows):
    """Glossary CSV import of these rows; waits for it. Returns the import status."""
    folder = os.path.join(work_dir(m.schema), 'glossary_import')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{kind}.csv')
    write_csv(path, rows, TERM_FIELDS[:4] + (TERM_FIELDS[4:] if kind == 'glossary_term' else []))
    code, data = api.call('POST', f'/v3/governance_artifact_types/{kind}/import?merge_option=specified',
                          files={'file': (path, 'text/csv')})
    process = data.get('process_id') or data.get('processId')
    print(f'import {len(rows)} {kind} rows -> {code}' + ('' if ok(code) else ' ' + json.dumps(data)[:300]))
    if api.dry_run or not process:
        return {}
    for _ in range(100):
        st = api.get(f'/v3/governance_artifact_types/import/status/{process}')
        if st.get('status') in ('SUCCEEDED', 'FAILED', 'COMPLETED', 'ERROR'):
            break
        time.sleep(3)
    msgs = st.get('messages') or {}
    listed = msgs.get('resources', []) if isinstance(msgs, dict) else list(msgs)
    total = msgs.get('count', len(listed)) if isinstance(msgs, dict) else len(listed)
    print(f'  status {st.get("status")}; {json.dumps(st.get("operations_count", {}))}; {total} messages'
          + (f' (showing {len(listed)})' if total > len(listed) else ''))
    for msg in listed:
        print('   ', json.dumps(msg)[:200])
    return st


def cmd_glossary(api, a):
    m = load_model(a)
    cats = categories_under(api, m.root)
    terms = published_terms(api, cats.values()) if cats else {}
    new_cats = [r for r in m.categories if m.category_path(r) not in cats]
    new_terms = [r for r in m.terms if r['Name'] not in terms]
    pending = pending_workflows(api, m)
    print(f'{m.root}: {len(cats)} of {len(m.categories)} categories and {len(terms)} of {len(m.terms)} terms '
          f'published; {len(pending)} publish tasks waiting')
    if pending and (new_cats or new_terms):
        print('Drafts from an earlier import are waiting to be published: approve them first, or a second '
              'import would create duplicate drafts.')
        return 'pending'
    if new_cats:
        import_rows(api, m, 'category', new_cats)
        return 'pending'  # the categories must be published before terms can be filed under them
    if new_terms:
        import_rows(api, m, 'glossary_term', new_terms)
        return 'pending'
    added = 0
    for r in m.extra_secondary:
        t, cat_id = terms.get(r['term']), cats.get(r['secondary_category'])
        if not t or not cat_id:
            print(f'  skip {r["term"]} -> {r["secondary_category"]}: term or category not found')
            continue
        found, version = has_relation(api, t, 'categories', cat_id)
        if found:
            continue
        code, data = api.call('POST', f'/v3/glossary_terms/{t["artifact_id"]}/versions/{version}/relationships',
                              {'categories': [{'id': cat_id}]})
        print(f'  secondary category {r["secondary_category"]} on {r["term"]} -> {code}',
              '' if ok(code) else json.dumps(data)[:200])
        added += ok(code)
    if added:
        return 'pending'  # each change is a draft of the published term
    print(f'glossary {m.root} is complete: {len(cats)} categories, {len(terms)} terms published')
    return 'done'


def pending_workflows(api, m):
    """[(task, workflow)] of open Publish tasks for this user whose drafts are all this model's categories and
    terms. Tasks for other people, other glossaries, or mixed drafts are never included."""
    me = iam_id(api)
    out, offset = [], 0
    while True:
        code, d = api.call('GET', f'/v3/workflow_user_tasks?unassigned=false&completed=false&hide_authoring_tasks=false'
                                  f'&count_only=false&limit=100&offset={offset}', write=False)
        rows = d.get('resources', []) if ok(code) else []
        for t in rows:
            e, md = t['entity'], t['metadata']
            if me not in (e.get('candidate_users') or []) and e.get('assignee') != me:
                continue
            if md.get('workflow_type_id') != 'governance_artifacts':
                continue
            if '#publish' not in {v.get('id') for p in e.get('form_properties', []) if p.get('id') == 'action'
                                  for v in p.get('enum_values', [])}:
                continue
            arts = workflow_artifacts(api, md['workflow_id'])  # the workflow itself lists only the first 10
            if arts and all(ours(m, x['metadata']) for x in arts):
                out.append((t, {'artifacts': arts}))
        if len(rows) < 100:
            return out
        offset += 100


def workflow_artifacts(api, workflow_id):
    out, offset = [], 0
    while True:
        d = api.get(f'/v3/workflows/{workflow_id}/artifacts?limit=200&offset={offset}')
        out += d.get('resources', [])
        if len(out) >= d.get('total_count', 0) or not d.get('resources'):
            return out
        offset += 200


def ours(m, art):
    """Is this draft one of the model's categories or terms, filed under the model's root category?"""
    kind, name = art.get('artifact_type'), art.get('name')
    cat = art.get('primary_category') or {}
    path = [c.get('name') for c in cat.get('context') or []] + ([cat['name']] if cat.get('name') else [])
    under_root = bool(path) and path[0] == m.root and all(p in m.category_names for p in path)
    if kind == 'category':
        return name in m.category_names and (not path or under_root)
    if kind == 'glossary_term':
        return name in m.term_names and under_root
    return False


def cmd_approve(api, a):
    m = load_model(a)
    found = pending_workflows(api, m)
    if not found:
        print(f'no publish tasks waiting for {m.root}')
        return 0
    me = iam_id(api)
    for t, wf in found:
        arts = wf['artifacts']
        kinds = sorted({x['metadata']['artifact_type'] for x in arts})
        names = [x['metadata']['name'] for x in arts]
        label = f'task {t["metadata"]["task_id"]}: publish {len(arts)} {"/".join(kinds)} ({", ".join(names[:5])}' \
                + (', …' if len(names) > 5 else '') + ')'
        if not a.yes or api.dry_run:
            print(f'would complete {label}')
            continue
        if not t['entity'].get('assignee'):
            code, data = api.call('POST', f'/v3/workflow_user_tasks/{t["metadata"]["task_id"]}/actions',
                                  {'action': 'claim', 'assignee': me})
            if not ok(code):
                print(f'  claim {label} -> {code} {json.dumps(data)[:200]}')
                continue
        code, data = api.call('POST', f'/v3/workflow_user_tasks/{t["metadata"]["task_id"]}/actions',
                              {'action': 'complete', 'assignee': me,
                               'form_properties': [{'id': 'action', 'value': '#publish'},
                                                   {'id': 'comment', 'value': f'Published from the erwin model {m.name}'}]})
        print(f'completed {label} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    if not a.yes:
        print('Nothing was completed. Re-run with --yes once the user has agreed.')
        return len(found)
    for _ in range(40):  # publishing finishes in the background
        if not pending_workflows(api, m):
            break
        time.sleep(5)
    return 0


# ---------- catalog ----------

def cmd_publish(api, a):
    """Publishes the import's connection, then the schema's tables and views, to the catalog."""
    m = load_model(a)
    pid, _ = find_container(api, 'project', a.project)
    catid, catname = find_container(api, 'catalog', a.catalog)
    conn_id, conn = find_connection(api, 'project', pid, a.connection)
    user = (conn.get('properties') or {}).get('username')
    if a.expect_user and user != a.expect_user:
        sys.exit(f'connection {a.connection} logs in as "{user}", not "{a.expect_user}". A published connection '
                 'shares its credentials with catalog users, so publish through a read-only login.')
    if not any(r['entity']['name'] == conn['name']
               for r in api.get(f'/v2/connections?catalog_id={catid}&limit=200').get('resources', [])):
        code, data = api.call('POST', f'/v2/assets/{conn_id}/publish?project_id={pid}', {'catalog_id': catid, 'mode': 0})
        print(f'publish connection {conn["name"]} (login {user}) -> {code}', '' if ok(code) else json.dumps(data)[:300])
    else:
        print(f'connection {conn["name"]} is in catalog {catname}')
    assets = schema_assets(api, 'project', pid, m.schema)
    published = schema_assets(api, 'catalog', catid, m.schema)
    for table, aid in sorted(assets.items()):
        if table in published:
            continue
        code, data = api.call('POST', f'/v2/assets/{aid}/publish?project_id={pid}', {'catalog_id': catid, 'mode': 0})
        print(f'  publish {m.schema}.{table} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    now = len(schema_assets(api, 'catalog', catid, m.schema)) if not api.dry_run else len(published)
    print(f'catalog {catname}: {now} of {len(assets)} {m.schema} tables and views')


def targets(api, a, schema):
    out = []
    if a.catalog and not a.project_only:
        cid, _ = find_container(api, 'catalog', a.catalog)
        out.append(('catalog', cid, schema_assets(api, 'catalog', cid, schema)))
    if a.project:
        pid, _ = find_container(api, 'project', a.project)
        out.append(('project', pid, schema_assets(api, 'project', pid, schema)))
    return out


def cmd_assign_terms(api, a):
    m = load_model(a)
    terms = published_terms(api, categories_under(api, m.root).values())
    missing = sorted({r['term'] for r in m.assignments} - set(terms))
    if missing:
        sys.exit(f'{len(missing)} terms are not published yet (e.g. {missing[:5]}): run glossary and approve first')
    for kind, cid, assets in targets(api, a, m.schema):
        added = 0
        for table in sorted({r['table'] for r in m.assignments}):
            if table not in assets:
                print(f'  {kind}: no asset for {m.schema}.{table}')
                continue
            entity = api.get(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}')['entity']
            have = {t.get('term_id') for t in (entity.get('asset_terms') or {}).get('list', [])}
            colinfo = entity.get('column_info') or {}
            ops_asset, ops_cols = [], []
            for r in (x for x in m.assignments if x['table'] == table):
                term = {'term_id': terms[r['term']]['global_id'], 'term_display_name': r['term']}
                if not r['column']:
                    if term['term_id'] not in have:
                        ops_asset.append(term)
                    continue
                ci = colinfo.get(r['column'])
                if ci is None:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}', 'value': {'column_terms': [term]}})
                    colinfo[r['column']] = {'column_terms': [term]}
                elif 'column_terms' not in ci:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}/column_terms', 'value': [term]})
                    ci['column_terms'] = [term]
                elif term['term_id'] not in {t.get('term_id') for t in ci['column_terms']}:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}/column_terms/-', 'value': term})
                    ci['column_terms'].append(term)
            if ops_asset:
                if entity.get('asset_terms') is None:
                    code, _ = api.call('POST', f'/v2/assets/{assets[table]}/attributes?{ctx(kind, cid)}',
                                       {'name': 'asset_terms', 'entity': {'list': ops_asset}})
                else:
                    code, _ = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/asset_terms?{ctx(kind, cid)}',
                                       [{'op': 'add', 'path': '/list/-', 'value': t} for t in ops_asset])
                print(f'  {kind} {table}: +{len(ops_asset)} table term -> {code}')
            if ops_cols:
                code, _ = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/column_info?{ctx(kind, cid)}', ops_cols)
                print(f'  {kind} {table}: +{len(ops_cols)} column terms -> {code}')
            added += len(ops_asset) + len(ops_cols)
        print(f'{kind}: {added} term assignments added ({len(m.assignments)} in the model)')


def resolve_classifications(api, names):
    """{model name: (wxdi id, wxdi name)}, trying the known aliases (e.g. PII) when a name isn't in wxdi."""
    rows = search(api, {'size': 500, '_source': ['metadata.name', 'artifact_id'],
                        'query': {'bool': {'must': [{'term': {'metadata.artifact_type': 'classification'}}]}}})
    found = {r['metadata']['name']: r['artifact_id'].split('_')[1] if '_' in r['artifact_id'] else r['artifact_id']
             for r in rows}
    out, missing = {}, []
    for n in names:
        hit = next((x for x in [n] + CLASS_ALIASES.get(n, []) if x in found), None)
        if hit:
            out[n] = (found[hit], hit)
        else:
            missing.append(n)
    if missing:
        sys.exit(f'Classifications not found in wxdi: {missing}')
    return out


def cmd_classify(api, a):
    m = load_model(a)
    mapping = m.classifications(a.min_confidence)
    cls = resolve_classifications(api, set(mapping.values()))
    terms = published_terms(api, categories_under(api, m.root).values())
    repo = next((t['global_id'].split('_')[0] for t in terms.values()), None)
    if a.terms:
        for name, cname in sorted(mapping.items()):
            t = terms.get(name)
            if not t:
                continue
            found, version = has_relation(api, t, 'classifications', cls[cname][0])
            if not found:
                code, data = api.call('POST', f'/v3/glossary_terms/{t["artifact_id"]}/versions/{version}/relationships',
                                      {'classifications': [{'id': cls[cname][0]}]})
                print(f'  term {name}: + {cls[cname][1]} -> {code} (a draft to publish)')
    cols = {}
    for r in m.assignments:
        if r['column'] and r['term'] in mapping:
            cols.setdefault(r['table'], []).append((r['column'], mapping[r['term']]))
    for kind, cid, assets in targets(api, a, m.schema):
        added = 0
        for table, pairs in sorted(cols.items()):
            if table not in assets:
                continue
            asset = api.get(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}')
            ci = asset['entity'].get('column_info') or {}
            ops = []
            for col, cname in pairs:
                cid_, wname = cls[cname]
                entry = {'id': cid_, 'name': wname, 'global_id': f'{repo}_{cid_}'}
                cur = ci.setdefault(col, {})
                if 'column_classifications' not in cur:
                    ops.append({'op': 'add', 'path': f'/{col}/column_classifications', 'value': [entry]}
                               if col in (asset['entity'].get('column_info') or {}) else
                               {'op': 'add', 'path': f'/{col}', 'value': {'column_classifications': [entry]}})
                    cur['column_classifications'] = [entry]
                elif cid_ not in {x.get('id') for x in cur['column_classifications']}:
                    ops.append({'op': 'add', 'path': f'/{col}/column_classifications/-', 'value': entry})
                    cur['column_classifications'].append(entry)
            if ops:
                code, data = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/column_info?{ctx(kind, cid)}', ops)
                print(f'  {kind} {table}: +{len(ops)} column classifications -> {code}',
                      '' if ok(code) else json.dumps(data)[:200])
                added += len(ops)
            tags = asset['metadata'].get('tags')
            if a.tag and a.tag not in (tags or []):
                api.patch(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}',
                          [{'op': 'add', 'path': '/metadata/tags/-' if tags else '/metadata/tags',
                            'value': a.tag if tags else [a.tag]}])
        print(f'{kind}: {added} column classifications added')
    skipped = [r for r in m.suggestions if r['Name'] not in mapping]
    if skipped:
        print(f'not applied (below {a.min_confidence} confidence, or no classification): '
              + ', '.join(f'{r["Name"]} ({r.get("confidence")}: {r.get("Classifications") or "none"})' for r in skipped))


# ---------- reports ----------

def cmd_check(api, a):
    m = load_model(a)
    print(f'{m.name}: {len(m.doc["entities"])} tables, {len(m.doc["views"])} views, {len(m.categories)} categories, '
          f'{len(m.terms)} terms, {len(m.assignments)} term assignments, '
          f'{len(m.classifications(a.min_confidence))} classifications at {a.min_confidence} confidence')
    print(f'  wxdi account {api.account}, user {api.user}')
    pid, pname = find_container(api, 'project', a.project)
    n = len(schema_assets(api, 'project', pid, m.schema))
    print(f'  project {pname}: {n} {m.schema} tables and views' + ('' if n else ' -- run wxdi-metadata-import first'))
    hit = [c for c in containers(api, 'catalog') if c['name'].lower() == a.catalog.lower()]
    print(f'  catalog {hit[0]["name"]} exists' if hit else
          f'  catalog {a.catalog} does not exist: the user creates it in the UI (Catalogs > New catalog)')
    cats = categories_under(api, m.root)
    print(f'  glossary {m.root}: {len(cats)} of {len(m.categories)} categories, '
          f'{len(published_terms(api, cats.values())) if cats else 0} of {len(m.terms)} terms published; '
          f'{len(pending_workflows(api, m))} publish tasks waiting')
    resolve_classifications(api, set(m.classifications(a.min_confidence).values()))
    print('  classifications found in wxdi')
    code, wf = api.call('GET', '/v3/workflow_configurations?workflow_type_id=governance_artifacts&limit=50', write=False)
    keys = {r['entity'].get('workflow_template_key') for r in wf.get('resources', [])}
    print('  publishing: ' + ('automatic (one Publish task per import; approve completes it)' if keys <= {'autoPublish', None}
                              else f'approval workflows {sorted(k for k in keys if k)}: approvers act in the UI'))


def cmd_status(api, a):
    m = load_model(a)
    cats = categories_under(api, m.root)
    terms = published_terms(api, cats.values()) if cats else {}
    print(f'glossary {m.root}: {len(cats)} categories, {len(terms)} of {len(m.terms)} terms published, '
          f'{len(pending_workflows(api, m))} publish tasks waiting')
    for kind, cid, assets in targets(api, a, m.schema):
        nterm = ncls = 0
        for table, aid in assets.items():
            e = api.get(f'/v2/assets/{aid}?{ctx(kind, cid)}')['entity']
            nterm += len((e.get('asset_terms') or {}).get('list', []))
            for c in (e.get('column_info') or {}).values():
                nterm += len(c.get('column_terms', []))
                ncls += len(c.get('column_classifications', []))
        print(f'{kind}: {len(assets)} {m.schema} tables and views, {nterm} term assignments, {ncls} column classifications')


def cmd_run_all(api, a):
    m = load_model(a)
    print('== check')
    cmd_check(api, a)
    for _ in range(6):  # categories, terms, secondary categories: each import leaves drafts to publish
        print('== glossary')
        state = cmd_glossary(api, a)
        if state == 'done':
            break
        if api.dry_run:
            print('(dry run: stopping before publishing)')
            return
        print('== approve')
        left = cmd_approve(api, a)
        if left:
            print('Stopped: the publish tasks above wait for the user. When they agree, run again with --approve.')
            sys.exit(3)
        time.sleep(10)  # newly published artifacts take a moment to show in search
    else:
        sys.exit('the glossary is still not complete after several rounds; check the import messages above')
    print('== publish')
    cmd_publish(api, a)
    print('== assign-terms')
    cmd_assign_terms(api, a)
    print('== classify')
    cmd_classify(api, a)
    print('== status')
    cmd_status(api, a)


def main():
    ap = common_args(__doc__)
    sub = ap.add_subparsers(dest='step', required=True)

    def p(name, help, project=True, connection=False):
        s = sub.add_parser(name, help=help)
        s.add_argument('--schema', help="the schema (default: the metadata file's)")
        s.add_argument('--metadata', help='default: work/<schema>/<schema>.metadata.json')
        s.add_argument('--catalog', help="catalog name (default: the model's name)")
        if project:
            s.add_argument('--project', required=True, help='project that holds the imported tables and views')
        if connection:
            s.add_argument('--connection', required=True, help='project connection the assets were imported through')
            s.add_argument('--expect-user', help='refuse to publish unless the connection logs in as this user')
        return s

    def conf(s):
        s.add_argument('--min-confidence', choices=LEVELS, default='high',
                       help='apply classification suggestions at or above this confidence (default high)')

    conf(p('check', 'read-only readiness check'))
    p('glossary', 'categories, terms and secondary categories', project=False)
    p('approve', 'complete the publish tasks', project=False).add_argument(
        '--yes', action='store_true', help='really complete them (the user has agreed)')
    p('publish', 'publish the connection and assets to the catalog', connection=True)
    for name in ('assign-terms', 'classify', 'status'):
        s = p(name, {'assign-terms': 'terms on tables, columns and view columns', 'classify': 'column classifications',
                     'status': 'what is published and assigned'}[name], project=False)
        s.add_argument('--project', help='also (or, with --project-only, only) the project assets')
        s.add_argument('--project-only', action='store_true')
        if name == 'classify':
            conf(s)
            s.add_argument('--terms', action='store_true', help='also classify the terms (drafts to publish)')
            s.add_argument('--tag', default=DERIVED_TAG, help='tag for tables with classified columns ("" for none)')
    s = p('run-all', 'everything in order', connection=True)
    conf(s)
    s.add_argument('--approve', action='store_true', help='complete the publish tasks (the user has agreed)')
    s.add_argument('--tag', default=DERIVED_TAG)
    a = ap.parse_args()
    if a.step == 'run-all':
        a.yes, a.terms, a.project_only = a.approve, False, False
    api = connect(a)
    {'check': cmd_check, 'glossary': cmd_glossary, 'approve': cmd_approve, 'publish': cmd_publish,
     'assign-terms': cmd_assign_terms, 'classify': cmd_classify, 'status': cmd_status,
     'run-all': cmd_run_all}[a.step](api, a)


if __name__ == '__main__':
    main()
