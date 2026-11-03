#!/usr/bin/env python3
"""Load an erwin model's metadata into IBM watsonx.data intelligence (SaaS), step by step.

Works on the files written by erwin2postgres.py (--glossary DIR and the SQL script) after the
schema exists in Postgres. Every step is safe to re-run: it reads the current state first and
only adds what is missing. Use --dry-run to see what a step would do.

Steps, in the usual order:
  check           read-only readiness check: tools, Postgres, account, names, storage, workflow, lineage
  status          what exists: categories, terms, project/catalog assets, connections
  run-sql         run a SQL script (or stdin) against the Postgres database in .env
  reader-sql      print SQL for a Postgres login that can only read the schema
  connection      create a project connection to the .env Postgres (or copying an existing one) for a login
  import          create (if needed) and run a metadata import of the schema, with primary/foreign keys
  glossary        import glossary.csv (categories, then terms), add extra secondary categories
  assign-terms    put the terms from term_assignments.csv on the project (and/or catalog) assets
  descriptions    make asset and column descriptions match the COMMENTs in the SQL script
  classify        add classifications to terms (as drafts) and columns, and tag those assets
  publish         publish the import's connection and the schema's assets to a catalog
  project, datasource, lineage-check, publish-status, teardown
                  creation and checks used by run-all; teardown lists or removes a load
  run-all         everything above in order for one model; stops when the user must act, resumes when re-run

Settings come from the command line, then environment variables, then a .env file (--env FILE,
otherwise ./.env, otherwise the .env at the root of this project):
  ibm_cloud_api_key  IBM Cloud API key (or DI_APIKEY, or --apikey-file with the JSON IBM Cloud downloads)
  di_service_url     wxdi API URL, e.g. https://api.ca-tor.dai.cloud.ibm.com (or DI_SERVICE_URL, or --url)
  postgres_host, postgres_port, postgres_database, postgres_user, postgres_password, postgres_sslmode
                     the Postgres the schema lives in (run-sql, reader-sql, connection)
"""
import argparse
import base64
import csv
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time

IAM_URL = 'https://iam.cloud.ibm.com/identity/token'
GLOSSARY_SEP = ' >> '
DERIVED_TAG = 'classifications-derived-not-modelled'
# Lineage settings as the wxdi UI stores them on an import whose scan parses view SQL (read from one on
# 2026-11-02). The MCP server's defaults (input_reader scenarioReader, five phases) scanned but produced no view lineage.
LINEAGE_SETTINGS = {
    'advanced_lineage_option': {'enable_extended_attributes': True, 'enable_transformation_logic': True,
                                'input_reader': 'parallel'},
    'lineage_scanning_phases': ['dictionary_processing', 'extraction_of_transformations',
                                'processing_of_external_inputs', 'processing_of_extracted_inputs'],
    'extraction_info': {'connection_type': 'direct'},
    'scanner': 'PostgreSQL',
}
PROJECT_HOME = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = {}  # the .env file, keys lower case
ENV_FILE = None  # its path


def load_env(path=None):
    """Reads KEY=value lines; # comments and blank lines are skipped, surrounding quotes removed.
    A value starting with -----BEGIN (a PEM certificate) continues until its -----END line."""
    for p in [path] if path else [os.path.join(os.getcwd(), '.env'), os.path.join(PROJECT_HOME, '.env')]:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            pem = None  # [key, lines] while inside a multi-line PEM value
            for line in open(p, encoding='utf-8'):
                line = line.strip()
                if pem:
                    pem[1].append(line.strip('"\''))
                    if line.startswith('-----END'):
                        ENV[pem[0]] = '\n'.join(pem[1]) + '\n'
                        pem = None
                elif line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    k = k[7:].strip() if k.startswith('export ') else k
                    v = v.strip().strip('"\'')
                    if v.startswith('-----BEGIN') and '-----END' not in v:
                        pem = [k.lower(), [v]]
                    else:
                        ENV[k.lower()] = v.replace('\\n', '\n') if v.startswith('-----BEGIN') else v
            if pem:
                sys.exit(f'{p}: {pem[0]} has no -----END line')
            global ENV_FILE
            ENV_FILE = p
            return p
        if path:
            sys.exit(f'No such .env file: {path}')


def setting(*names, default=None):
    """First of names found in the environment (as given or upper case), then in .env."""
    for n in names:
        for v in (os.environ.get(n), os.environ.get(n.upper()), ENV.get(n.lower())):
            if v:
                return v
    return default


def pg_ca():
    """The CA certificate (PEM) wxdi and the TLS check should trust: postgres_cert inline, or a file."""
    cert = setting('postgres_cert', 'postgres_ssl_certificate')
    path = setting('postgres_ssl_certificate_file')
    if not cert and path:
        cert = open(os.path.expanduser(path), encoding='utf-8').read()
    return cert


def pg_settings():
    return {'host': setting('postgres_host', 'PGHOST'), 'port': setting('postgres_port', 'PGPORT', default='5432'),
            'database': setting('postgres_database', 'PGDATABASE', default='postgres'),
            'user': setting('postgres_user', 'PGUSER'), 'password': setting('postgres_password', 'PGPASSWORD'),
            'sslmode': setting('postgres_sslmode', 'PGSSLMODE', default='require')}


class Api:
    """Minimal REST client. Uses curl so it works with any Python's SSL setup."""

    def __init__(self, url, apikey, dry_run=False):
        self.url, self.dry_run, self.apikey = url.rstrip('/'), dry_run, apikey
        self.login()
        claims = self.token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))
        # the key's IBM Cloud account; the projects API also lists other accounts' projects
        self.account, self.user = claims['account']['bss'], claims.get('email') or claims.get('sub')

    def login(self):
        out = subprocess.run(['curl', '-s', '-X', 'POST', IAM_URL, '-d',
                              f'grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey={self.apikey}'],
                             capture_output=True, text=True).stdout
        try:
            data = json.loads(out)
            self.token = data['access_token']
        except (ValueError, KeyError):
            sys.exit(f'Could not get an IAM token: {out[:300]}')
        # IAM tokens last an hour; long imports outlive them, so renew 5 minutes early
        self.expires = data.get('expiration', time.time() + 3600) - 300

    def call(self, method, path, body=None, content_type='application/json', files=None, write=None):
        """Returns (status, parsed body). write=False marks a read; writes are skipped in dry-run."""
        is_write = method != 'GET' if write is None else write
        if is_write and self.dry_run:
            return 0, {'dry_run': True}
        if time.time() > self.expires:
            self.login()
        url = path if path.startswith('https://') else self.url + path
        cmd = ['curl', '-s', '-m', '120', '-w', '\n%{http_code}', '-X', method, url,
               '-H', f'Authorization: Bearer {self.token}']
        if files:
            for field, (path_on_disk, mime) in files.items():
                cmd += ['-F', f'{field}=@{path_on_disk};type={mime}']
        elif body is not None:
            cmd += ['-H', f'Content-Type: {content_type}', '-d', json.dumps(body)]
        text, code = subprocess.run(cmd, capture_output=True, text=True).stdout.rsplit('\n', 1)
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            data = {'raw': text}
        return int(code), data

    def get(self, path):
        for attempt in range(5):  # the service returns the odd 5xx (connection resets); reads are safe to retry
            code, data = self.call('GET', path)
            if code < 500 and code != 0:
                break
            time.sleep(5 * (attempt + 1))
        if code >= 300:
            raise SystemExit(f'GET {path} -> {code}: {json.dumps(data)[:300]}')
        return data

    def patch(self, path, ops):
        return self.call('PATCH', path, ops, 'application/json-patch+json')


def ok(code):
    return code == 0 or 200 <= code < 300


# ---------- lookups ----------

def find_container(api, kind, name_or_id):
    """kind: 'project' or 'catalog'. Returns (id, name)."""
    items = [(c['id'], c['name']) for c in containers(api, kind)]
    hits = [i for i in items if name_or_id in i]
    if len(hits) != 1:
        raise SystemExit(f'{kind} "{name_or_id}": found {len(hits)} matches among {[n for _, n in items]}')
    return hits[0]


def containers(api, kind):
    """Projects or catalogs of the API key's account only: [{id, name, storage}]."""
    if kind == 'project':
        rows = api.get('/v2/projects?limit=100&include=everything').get('resources', [])
        return [{'id': r['metadata']['guid'], 'name': r['entity']['name'], 'storage': r['entity'].get('storage', {})}
                for r in rows if r['entity'].get('scope', {}).get('bss_account_id') in (None, api.account)]
    rows = api.get('/v2/catalogs?limit=100').get('catalogs', [])
    return [{'id': r['metadata']['guid'], 'name': r['entity']['name'], 'storage': r['entity'].get('bucket') or {}}
            for r in rows if r['entity'].get('bss_account_id') in (None, api.account)]


def ctx(kind, cid):
    return f'{kind}_id={cid}'


def schema_assets(api, kind, cid, schema, connection_id=None):
    """{table_name: asset_id} for data assets of this schema (optionally from one connection)."""
    code, data = api.call('POST', f'/v2/asset_types/data_asset/search?{ctx(kind, cid)}',
                          {'query': '*:*', 'limit': 500}, write=False)
    out = {}
    for r in data.get('results', []):
        key = r['metadata'].get('resource_key', '')
        if not key.endswith(f':/{schema}/{r["metadata"]["name"]}'):
            continue
        if connection_id:
            a = api.get(f'/v2/assets/{r["metadata"]["asset_id"]}?{ctx(kind, cid)}')
            if connection_id not in {x.get('connection_id') for x in a.get('attachments', [])}:
                continue
        if r['metadata']['name'] in out:
            raise SystemExit(f'Two {schema}.{r["metadata"]["name"]} assets in this {kind}; pass --connection to pick one')
        out[r['metadata']['name']] = r['metadata']['asset_id']
    return out


def find_connection(api, kind, cid, name_or_id):
    rows = api.get(f'/v2/connections?{ctx(kind, cid)}&limit=200').get('resources', [])
    hits = [r for r in rows if name_or_id in (r['metadata']['asset_id'], r['entity']['name'])]
    if len(hits) != 1:
        raise SystemExit(f'connection "{name_or_id}": {len(hits)} matches in this {kind}')
    return hits[0]['metadata']['asset_id'], hits[0]['entity']


def search(api, query):
    code, data = api.call('POST', '/v3/search?auth_scope=category', query, write=False)
    if not ok(code):
        raise SystemExit(f'search failed {code}: {json.dumps(data)[:300]}')
    return data.get('rows', [])


def categories_under(api, root):
    """{path: artifact_id} for the root category and its direct children ('Root >> Child')."""
    rows = search(api, {'size': 500, '_source': ['metadata.name', 'artifact_id', 'categories'],
                        'query': {'bool': {'must': [{'term': {'metadata.artifact_type': 'category'}}]}}})
    ids = {r['artifact_id'].split('_')[1]: r for r in rows}
    out = {}
    for aid, r in ids.items():
        name, parent = r['metadata']['name'], r['categories'].get('primary_category_id')
        if name == root and parent not in ids:
            out[root] = aid
    if root not in out:
        return out
    for aid, r in ids.items():
        if r['categories'].get('primary_category_id') == out[root]:
            out[root + GLOSSARY_SEP + r['metadata']['name']] = aid
    return out


def published_terms(api, category_ids):
    """{name: {'global_id', 'artifact_id', 'version_id'}} for published terms in these categories."""
    rows = search(api, {'size': 2000, '_source': ['metadata.name', 'entity.artifacts', 'categories'],
                        'query': {'bool': {'must': [{'term': {'metadata.artifact_type': 'glossary_term'}},
                                                    {'terms': {'categories.primary_category_id': list(category_ids)}}]}}})
    out = {}
    for r in rows:
        a = r['entity']['artifacts']
        if r['metadata']['name'] in out:
            raise SystemExit(f'Two published terms named "{r["metadata"]["name"]}" in these categories')
        out[r['metadata']['name']] = {'global_id': a['global_id'], 'artifact_id': a['artifact_id']}
    return out


def published_version(api, term):
    """The published version id of a term (search results don't carry it)."""
    if 'version_id' not in term:
        versions = api.get(f'/v3/glossary_terms/{term["artifact_id"]}/versions?limit=50').get('resources', [])
        pub = [v['metadata']['version_id'] for v in versions if v['metadata'].get('state') == 'PUBLISHED']
        term['version_id'] = pub[0] if pub else None
    return term['version_id']


def editable_versions(api, term):
    """The versions to check and change: a pending draft if there is one (a published term with a
    draft can't get a second draft), otherwise the published version."""
    published_version(api, term)
    code, d = api.call('GET', f'/v3/glossary_terms/{term["artifact_id"]}/versions?status=DRAFT&limit=10')
    drafts = [v['metadata']['version_id'] for v in d.get('resources', [])] if ok(code) else []
    return drafts[:1] + ([term['version_id']] if term.get('version_id') else [])


def has_relation(api, term, kind, target_id):
    """True if the published version or the pending draft already links to target_id.
    Returns (found, version to add to)."""
    versions = editable_versions(api, term)
    for v in versions:
        rel = api.get(f'/v3/glossary_terms/{term["artifact_id"]}/versions/{v}/relationships?type={kind}')
        if target_id in {x['entity'].get('target_id') for x in rel.get(kind, {}).get('resources', [])}:
            return True, None
    return False, versions[0] if versions else None


def classification_ids(api, names):
    rows = search(api, {'size': 500, '_source': ['metadata.name', 'artifact_id'],
                        'query': {'bool': {'must': [{'term': {'metadata.artifact_type': 'classification'}}]}}})
    # search returns artifact_id as '<repository>_<artifact id>_<version>'
    found = {r['metadata']['name']: r['artifact_id'].split('_')[1] if '_' in r['artifact_id'] else r['artifact_id']
             for r in rows}
    missing = set(names) - set(found)
    if missing:
        raise SystemExit(f'Classifications not found in wxdi: {sorted(missing)}')
    return {n: found[n] for n in names}


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


# ---------- steps ----------

def pg_tls(host, port, cadata=None):
    """Does the server's certificate verify against cadata, or else this machine's trusted CAs? (verified, detail)"""
    try:
        with socket.create_connection((host, int(port)), timeout=15) as sock:
            sock.sendall((8).to_bytes(4, 'big') + (80877103).to_bytes(4, 'big'))  # Postgres SSLRequest
            if sock.recv(1) != b'S':
                return False, 'server does not offer SSL'
            try:
                ctx = ssl.create_default_context(cadata=cadata) if cadata else ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    return True, f'certificate from {dict(x[0] for x in tls.getpeercert()["issuer"]).get("commonName")}'
            except ssl.SSLCertVerificationError as e:
                return False, e.verify_message
    except OSError as e:
        return False, f'cannot reach {host}:{port}: {e}'


def cmd_check(api, a):
    """Read-only readiness check of everything the load needs. Changes nothing."""
    name = a.name
    schema = a.schema or re.sub(r'[^0-9A-Za-z]+', '_', name).strip('_').lower()  # as erwin2postgres.snake
    results = []

    def note(level, area, text):
        results.append(level)
        print(f'  {level:4}  {area:12} {text}')

    print(f'Checking for model "{name}" (schema {schema}, project/catalog/category {name}); nothing is changed\n')
    # local tools
    note('ok' if shutil.which('curl') else 'FAIL', 'tools', 'curl ' + ('found' if shutil.which('curl') else 'missing'))
    try:
        import psycopg2  # noqa: F401
        driver = 'psycopg2'
    except ImportError:
        driver = 'psql' if shutil.which('psql') else None
    note('ok' if driver else 'FAIL', 'tools', f'SQL runner: {driver}' if driver else
         'no psql or psycopg2: python3 -m venv venv && venv/bin/pip install psycopg2-binary')

    # Postgres
    pg = pg_settings()
    if not (pg['host'] and pg['user'] and pg['password']):
        note('FAIL', 'postgres', 'postgres_host / postgres_user / postgres_password not set')
    elif driver == 'psycopg2':
        import psycopg2
        try:
            conn = psycopg2.connect(host=pg['host'], port=pg['port'], dbname=pg['database'], user=pg['user'],
                                    password=pg['password'], sslmode=pg['sslmode'], connect_timeout=20)
            cur = conn.cursor()
            cur.execute("select current_setting('server_version'), rolcreaterole or rolsuper from pg_roles where rolname = current_user")
            version, can_role = cur.fetchone()
            note('ok', 'postgres', f'{pg["user"]}@{pg["host"]}:{pg["port"]}/{pg["database"]}, PostgreSQL {version}')
            cur.execute("select current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int, "
                        "(select count(*) from pg_stat_activity where datname is not null)")
            room, used = cur.fetchone()
            note('ok' if room >= 25 else 'WARN', 'postgres', f'{room} connections allowed, {used} in use' +
                 ('' if room >= 25 else ': small for wxdi, whose lineage extraction opens many at once. The read-only '
                  'login gets most of them, so close other tools (e.g. DBeaver) during the import'))
            cur.execute("select count(c.oid) from pg_namespace n left join pg_class c on c.relnamespace = n.oid "
                        "where n.nspname = %s group by n.oid", (schema,))
            row = cur.fetchone()
            if row is None:
                note('ok', 'postgres', f'schema {schema} does not exist yet; the SQL script creates it')
            elif row[0] == 0:
                note('ok', 'postgres', f'schema {schema} exists and is empty')
            else:
                note('WARN', 'postgres', f'schema {schema} already holds {row[0]} objects; the SQL script will fail on them')
            cur.execute('select 1 from pg_roles where rolname = %s', (f'{schema}_reader',))
            exists = cur.fetchone()
            note('ok' if can_role else 'FAIL', 'postgres', f'{pg["user"]} {"can" if can_role else "cannot"} create the '
                 f'read-only login {schema}_reader' + (' (it already exists)' if exists else ''))
            conn.close()
        except psycopg2.Error as e:
            note('FAIL', 'postgres', f'cannot connect: {str(e).strip()[:200]}')
    else:
        note('WARN', 'postgres', 'connection not tested (needs psycopg2)')
    if pg['host'] and pg['sslmode'] not in ('disable', 'allow'):
        try:
            ca = pg_ca()
        except OSError as e:
            ca = None
            note('FAIL', 'postgres', f'cannot read postgres_ssl_certificate_file: {e}')
        verified, detail = pg_tls(pg['host'], pg['port'])
        if verified:
            note('ok', 'postgres', f'TLS {detail}, publicly trusted: wxdi needs no certificate')
        elif ca:
            try:
                verified, detail = pg_tls(pg['host'], pg['port'], ca)
            except ssl.SSLError as e:
                verified, detail = False, f'the CA certificate in .env does not parse ({e.reason})'
            note('ok' if verified else 'FAIL', 'postgres', f'TLS {detail}, verified against the CA certificate in .env: '
                 'wxdi will be given it' if verified else f'TLS {detail}, not verified by the CA certificate in .env')
        else:
            note('WARN', 'postgres', f'TLS {detail}: wxdi will not trust it. Download the CA certificate from the '
                 'database provider and set postgres_cert (or postgres_ssl_certificate_file) in .env')

    # wxdi account, services
    note('ok', 'wxdi', f'API key works: {api.user}, account {api.account}, {api.url}')
    code, inst = api.call('GET', 'https://resource-controller.cloud.ibm.com/v2/resource_instances?limit=100', write=False)
    services = {}
    for r in inst.get('resources', []) if ok(code) else []:
        kind = r['crn'].split(':')[4]
        c2, plan = api.call('GET', f'https://globalcatalog.cloud.ibm.com/api/v1/{r["resource_plan_id"]}', write=False)
        services.setdefault(kind, []).append(f'{r["name"]} ({plan.get("name", "?")} plan, {r.get("region_id")})')
    if not ok(code):
        note('WARN', 'wxdi', f'cannot list the account\'s services ({code})')
    else:
        for kind, label in (('datacatalog', 'catalog service (IKC)'), ('cloud-object-storage', 'Cloud Object Storage')):
            note('ok' if kind in services else 'FAIL', 'wxdi', f'{label}: ' + (', '.join(services[kind]) if kind in services else 'none'))
        trial = [s for v in services.values() for s in v if 'trial plan' in s.lower() or 'lite plan' in s.lower()]
        if trial:
            note('WARN', 'wxdi', f'trial/lite plans have limits and expire: {", ".join(trial)}')

    # projects and catalogs: name collisions, and where new ones would get storage
    projects = containers(api, 'project')
    code, allp = api.call('GET', '/v2/projects?limit=100', write=False)
    others = len(allp.get('resources', [])) - len(projects)
    note('ok', 'project', f'{len(projects)} projects in this account' +
         (f' ({others} more visible from other accounts are ignored)' if others > 0 else ''))
    hit = [p for p in projects if p['name'].lower() == name.lower()]
    if hit:
        assets = schema_assets(api, 'project', hit[0]['id'], schema)
        note('WARN' if assets else 'ok', 'project', f'project {hit[0]["name"]} already exists' +
             (f' and holds {len(assets)} {schema} assets: the load adds to it' if assets else ' and is empty; it will be reused'))
    else:
        cos = {p['storage'].get('guid') for p in projects if p['storage'].get('guid')}
        note('ok' if cos else 'WARN', 'project', f'project {name} will be created' +
             (f', on the storage the other projects use ({", ".join(sorted(cos))})' if cos else
              '; no existing project shows which storage to use, so create it in the UI'))
    catalogs = containers(api, 'catalog')
    hit = [c for c in catalogs if c['name'].lower() == name.lower()]
    if hit:
        note('ok', 'catalog', f'catalog {hit[0]["name"]} exists and will be used')
    else:
        code, pol = api.call('GET', f'https://iam.cloud.ibm.com/v1/policies?account_id={api.account}&type=authorization',
                             write=False)
        auth = any({x['value'] for s in p['subjects'] for x in s['attributes']} >= {'datacatalog'} and
                   {x['value'] for r in p['resources'] for x in r['attributes']} >= {'cloud-object-storage'}
                   for p in pol.get('policies', []))
        note('ok' if auth else 'WARN', 'catalog', f'catalog {name} does not exist; ' +
             ('the catalog service may use the account\'s storage, so the API can try to create it' if auth else
              'no catalog-to-storage authorization in this account, so create the catalog in the UI '
              f'(Catalogs > View all catalogs > New catalog, name {name}; see SKILL.md "Manual step"), then run check again'))

    # glossary, classifications, workflow
    def names_of(artifact_type):
        return [r['metadata']['name'] for r in search(api, {'size': 500, '_source': ['metadata.name'],
                'query': {'bool': {'must': [{'term': {'metadata.artifact_type': artifact_type}}]}}})]
    root = [n for n in names_of('category') if n.lower() == name.lower()]
    note('WARN' if root else 'ok', 'glossary', f'root category {name} ' +
         (f'already exists ({root[0]}): terms are added under it, existing ones skipped' if root else
          'does not exist yet; the glossary import creates it'))
    wanted = ['Personally Identifiable Information', 'Personal Information', 'Sensitive Personal Information']
    missing = [w for w in wanted if w not in names_of('classification')]
    note('FAIL' if missing else 'ok', 'glossary', 'classifications ' + (f'missing: {", ".join(missing)}' if missing else
         'PII, Personal Information and Sensitive Personal Information are published'))
    code, wf = api.call('GET', '/v3/workflow_configurations?workflow_type_id=governance_artifacts&limit=50', write=False)
    templates = {r['entity'].get('workflow_template_key') for r in wf.get('resources', [])}
    if not ok(code):
        note('WARN', 'workflow', f'cannot read the publishing workflow ({code})')
    elif templates <= {'autoPublish'}:
        note('ok', 'workflow', 'publishing is "Automatic publishing": one Publish action per import, no approver')
    else:
        note('WARN', 'workflow', f'publishing goes through approval workflows ({", ".join(sorted(t for t in templates if t))}): '
             'each draft may need an approver')

    # lineage
    code, lin = api.call('POST', '/gov_lineage/v2/search_lineage_assets', {'query': '*', 'limit': 1}, write=False)
    note('ok' if ok(code) else 'WARN', 'lineage', 'lineage service available' if ok(code) else
         f'lineage service not available ({code}): the import will run discovery only')
    code, dsd = api.call('GET', '/v2/data_source_definitions', write=False)
    pg_dsd = ok(code) and 'postgresql' in dsd.get('data_source_definition_id_to_datasource_types', {})
    note('ok' if pg_dsd else 'WARN', 'lineage', 'PostgreSQL data source definitions are supported' if pg_dsd else
         'cannot confirm PostgreSQL data source definitions')

    print(f'\n{results.count("ok")} ok, {results.count("WARN")} warnings, {results.count("FAIL")} failures')
    if 'FAIL' in results:
        sys.exit(1)


def cmd_status(api, a):
    pid, pname = find_container(api, 'project', a.project)
    print(f'project {pname} ({pid})')
    for name, aid in sorted(schema_assets(api, 'project', pid, a.schema).items()):
        print(f'  asset {a.schema}.{name}  {aid}')
    if a.catalog:
        catid, catname = find_container(api, 'catalog', a.catalog)
        assets = schema_assets(api, 'catalog', catid, a.schema)
        print(f'catalog {catname} ({catid}): {len(assets)} {a.schema} assets')
        for r in api.get(f'/v2/connections?catalog_id={catid}&limit=100').get('resources', []):
            print(f'  connection {r["entity"]["name"]}  user={r["entity"].get("properties", {}).get("username")}')
    if a.category:
        cats = categories_under(api, a.category)
        print(f'categories: {sorted(cats)}')
        if cats:
            print(f'published terms: {len(published_terms(api, cats.values()))}')


def cmd_run_sql(api, a):
    """Runs the script as-is (autocommit), so its own BEGIN/COMMIT decide what is one transaction."""
    pg = pg_settings()
    if a.user:
        pg.update(user=a.user, password=setting(a.password_env) if a.password_env else None)
    missing = [k for k in ('host', 'user', 'password') if not pg[k]]
    if missing:
        sys.exit(f'Postgres settings missing: {", ".join("postgres_" + k for k in missing)} (in .env or the environment)')
    sql = sys.stdin.read() if a.file == '-' else open(os.path.expanduser(a.file), encoding='utf-8').read()
    target = f'{pg["user"]}@{pg["host"]}:{pg["port"]}/{pg["database"]}'
    if a.dry_run:
        print(f'(dry run) would run {len(sql)} characters of SQL as {target}')
        return
    try:
        import psycopg2 as driver
    except ImportError:
        try:
            import psycopg as driver
        except ImportError:
            driver = None
    if driver:
        conn = driver.connect(host=pg['host'], port=pg['port'], dbname=pg['database'], user=pg['user'],
                              password=pg['password'], sslmode=pg['sslmode'], connect_timeout=20)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
            for r in rows:
                print(*r, sep='\t')
        conn.close()
    elif shutil.which('psql'):
        env = dict(os.environ, PGPASSWORD=pg['password'], PGSSLMODE=pg['sslmode'])
        res = subprocess.run(['psql', '-h', pg['host'], '-p', str(pg['port']), '-U', pg['user'], '-d', pg['database'],
                              '-v', 'ON_ERROR_STOP=1', '-X', '-q'], input=sql, text=True, env=env)
        if res.returncode:
            sys.exit(res.returncode)
    else:
        sys.exit('run-sql needs psql or the psycopg2 module: python3 -m venv venv && venv/bin/pip install psycopg2-binary')
    print(f'ran {a.file} as {target}')


def cmd_reader_sql(api, a):
    role = a.role or f'{a.schema}_reader'
    database = a.database or pg_settings()['database']
    password = setting(a.password_env) if a.password_env else None
    if a.password_env and not password:
        sys.exit(f'{a.password_env} is not set in the environment or .env')
    print(f"""-- Read-only login for schema {a.schema}.{'' if password else ' Replace the password before running.'}
CREATE ROLE {role} LOGIN PASSWORD '{(password or 'CHANGE_ME').replace("'", "''")}' CONNECTION LIMIT {a.connection_limit};
GRANT CONNECT ON DATABASE {database} TO {role};
GRANT USAGE ON SCHEMA {a.schema} TO {role};
GRANT SELECT ON ALL TABLES IN SCHEMA {a.schema} TO {role};
ALTER DEFAULT PRIVILEGES IN SCHEMA {a.schema} GRANT SELECT ON TABLES TO {role};""")


def cmd_connection(api, a):
    """A platform connection (in the Platform assets catalog), added to the project as a reference to it.
    Lineage scans need it that way: through a project-only connection the SQL Server lineage extractor
    failed before it ever connected ("Job failed during extraction"), while the same import through a
    platform connection worked (2026-11-03)."""
    pid, _ = find_container(api, 'project', a.project)
    rows = api.get(f'/v2/connections?project_id={pid}&limit=200').get('resources', [])
    if any(r['entity']['name'] == a.name for r in rows):
        print(f'connection {a.name} already exists in the project; nothing to do')
        return
    pac = platform_catalog(api)
    platform = [r for r in api.get(f'/v2/connections?catalog_id={pac}&limit=200').get('resources', [])
                if r['entity']['name'] == a.name]
    if platform:
        conn_id = platform[0]['metadata']['asset_id']
        print(f'platform connection {a.name} exists ({conn_id})')
    else:
        conn_id = create_platform_connection(api, a, pid, pac)
        if not conn_id:
            if api.dry_run:
                print(f'would add it to project {a.project}')
            return
    code, data = api.call('POST', f'/v2/connections?project_id={pid}&test=true',
                          {'ref_catalog_id': pac, 'ref_asset_id': conn_id})
    print(f'add platform connection {a.name} to project {a.project} -> {code}', '' if ok(code) else json.dumps(data)[:300])


def create_platform_connection(api, a, pid, pac):
    """Creates the connection in the Platform assets catalog; returns its id (None if it failed, or in dry-run)."""
    password = setting(a.password_env)
    if not password:
        raise SystemExit(f'{a.password_env} is not set in the environment or .env')
    if a.copy_from:
        src_pid = find_container(api, 'project', a.from_project)[0] if a.from_project else pid
        _, src = find_connection(api, 'project', src_pid, a.copy_from)
        props = {k: v for k, v in src['properties'].items() if k not in ('username', 'password')}
        dtype, origin, where = src['datasource_type'], src.get('origin_country', 'us'), f'copied from {src["name"]}'
    else:  # the Postgres in .env
        pg = pg_settings()
        if not pg['host']:
            raise SystemExit('Pass --copy-from, or set postgres_host (and port, database) in .env')
        dtype, offset = None, 0
        while not dtype:  # pages of at most 100
            page = api.get(f'/v2/datasource_types?limit=100&offset={offset}').get('resources', [])
            if not page:
                raise SystemExit('No postgresql datasource type in this wxdi account')
            dtype = next((t['metadata']['asset_id'] for t in page if t['entity']['name'] == 'postgresql'), None)
            offset += 100
        props = {'host': pg['host'], 'port': str(pg['port']), 'database': pg['database'],
                 'ssl': 'false' if pg['sslmode'] in ('disable', 'allow') else 'true'}
        cert = pg_ca()
        if cert:
            props['ssl_certificate'] = cert
        origin, where = 'us', f'{pg["host"]}:{pg["port"]}/{pg["database"]}'
    props.update(username=a.username, password=password)
    body = {'name': a.name, 'description': a.description or f'{a.username} login, {where}',
            'datasource_type': dtype, 'origin_country': origin, 'properties': props}
    code, data = api.call('POST', f'/v2/connections?catalog_id={pac}&test=true', body)
    print(f'create platform connection {a.name} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    return data['metadata']['asset_id'] if ok(code) and not api.dry_run else None


def cmd_import(api, a):
    pid, _ = find_container(api, 'project', a.project)
    conn_id, _ = find_connection(api, 'project', pid, a.connection)
    code, data = api.call('POST', f'/v2/asset_types/metadata_import/search?project_id={pid}',
                          {'query': f'asset.name:"{a.name}"'}, write=False)
    existing = [r for r in data.get('results', []) if r['metadata']['name'] == a.name]
    if existing:
        mdi_id = existing[0]['metadata']['asset_id']
        print(f'metadata import {a.name} exists ({mdi_id})')
        entity = api.get(f'/v2/metadata_imports/{mdi_id}?project_id={pid}')['entity']
        info = entity.get('datasource_definition_info')
        if (getattr(a, 'lineage', False) or getattr(a, 'lineage_only', False)) and info is not None:
            fix = {k: v for k, v in LINEAGE_SETTINGS.items() if entity.get(k) != v}
            if not info.get('data_source_asset_id') or not entity.get('data_source_asset_id'):
                pg = pg_settings()
                dsd = info.get('data_source_asset_id') or a.datasource or find_datasource(
                    api, platform_catalog(api), pg['host'], pg['port'], pg['database'])[0]
                fix.update(datasource_definition_info=dict(info, data_source_asset_id=dsd), data_source_asset_id=dsd)
            if fix:
                code, _ = api.call('PATCH', f'/v2/metadata_imports/{mdi_id}?project_id={pid}', fix)  # a merge object
                print(f'  updated its lineage settings ({", ".join(sorted(fix))}) -> {code}')
    else:
        body = {'name': a.name, 'description': f'erwin model schema {a.schema} via {a.connection}',
                'import_type': 'metadata', 'connection_id': conn_id, 'target_project_id': pid,
                'unified_lineage': True, 'scope': {'paths': [f'/{a.schema}']},
                'import_options': {'include_primary_key': True, 'include_foreign_key': True}}
        if getattr(a, 'lineage', False) or getattr(a, 'lineage_only', False):
            pg = pg_settings()
            dsd = a.datasource or find_datasource(api, platform_catalog(api), pg['host'], pg['port'], pg['database'])[0]
            if not dsd:
                raise SystemExit('Lineage needs a data source definition for this database: run "datasource" first')
            database = getattr(a, 'database', None) or pg['database']
            body.update(LINEAGE_SETTINGS, **{
                'import_type': 'lineage_and_discovery',
                # snake case: the API silently drops dataSourceAssetId (the name in the MCP server's model)
                'datasource_definition_info': {'data_source_asset_id': dsd, 'technology': 'PostgreSQL', 'scanner': 'PostgreSQL'},
                'data_source_asset_id': dsd,
                'scope_lineage': {'paths': [f'/{database}/{a.schema}']},
                'reimport_options': {'update_name': True, 'update_description': True, 'update_column_descriptions': True,
                                     'delete_when_deleted_at_source': False, 'delete_when_removed_from_scope': False}})
            if getattr(a, 'lineage_only', False):  # no assets: only lineage, e.g. through a login that may read view SQL
                body['import_type'] = 'lineage'
                body.pop('import_options')
        code, data = api.call('POST', f'/v2/metadata_imports?project_id={pid}&job_name={a.name}_job&create_job=true', body)
        print(f'create metadata import {a.name} -> {code}', '' if ok(code) else json.dumps(data)[:300])
        if api.dry_run or not ok(code):
            return None
        mdi_id = data['metadata']['asset_id']
    job_id = api.get(f'/v2/metadata_imports/{mdi_id}?project_id={pid}')['entity'].get('job_id')
    active = ('Queued', 'Starting', 'Running')
    code, runs = api.call('GET', f'/v2/jobs/{job_id}/runs?project_id={pid}')
    running = [r for r in (runs.get('results') or [])
               if r.get('entity', {}).get('job_run', {}).get('state') in active]
    if running:  # a run is already going (e.g. an earlier run-all lost track of it): wait for it
        run_id = running[0]['metadata']['asset_id']
        print(f'run {run_id} already in progress')
    else:
        code, data = api.call('POST', f'/v2/jobs/{job_id}/runs?project_id={pid}', {'job_run': {}})
        if api.dry_run:
            print('would run the import job')
            return None
        run_id = data['metadata']['asset_id']
        print(f'run {run_id} started')
    state, misses = 'Running', 0
    for _ in range(1080):  # up to three hours: lineage scans take much longer than discovery
        code, data = api.call('GET', f'/v2/jobs/{job_id}/runs/{run_id}?project_id={pid}')
        if code < 300:
            state, misses = data['entity']['job_run']['state'], 0
            if state not in active:
                break
        else:  # the run status call fails now and then while the job is busy; give up only if it persists
            misses += 1
            if misses >= 30:
                sys.exit(f'run {run_id}: status unavailable ({code}): {json.dumps(data)[:300]}')
        time.sleep(10)
    print(f'run {run_id}: {state}')
    return state


def cmd_glossary(api, a):
    rows = read_csv(os.path.join(a.glossary_dir, 'glossary.csv'))
    root = next(r['Name'] for r in rows if r['Artifact Type'] == 'category' and not r['Category'])
    cats = categories_under(api, root)
    terms = published_terms(api, cats.values()) if cats else {}
    new_cats = [r for r in rows if r['Artifact Type'] == 'category'
                and (r['Category'] + GLOSSARY_SEP + r['Name'] if r['Category'] else r['Name']) not in cats]
    new_terms = [r for r in rows if r['Artifact Type'] == 'glossary_term' and r['Name'] not in terms]
    print(f'categories to create: {[r["Name"] for r in new_cats]}; terms to create: {len(new_terms)} '
          f'({len(terms)} already published)')
    if new_terms and terms:
        print('  note: some terms already exist. Importing creates draft terms; make sure no earlier import of '
              'these terms is still waiting as drafts, or they will be duplicated.')
    for kind, subset in (('category', new_cats), ('glossary_term', new_terms)):
        if not subset:
            continue
        path = os.path.join(a.glossary_dir, f'_import_{kind}.csv')
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['Name', 'Artifact Type', 'Category', 'Description'] +
                               (['Abbreviations', 'Secondary Categories'] if kind == 'glossary_term' else []),
                               extrasaction='ignore')
            w.writeheader()
            w.writerows(subset)
        code, data = api.call('POST', f'/v3/governance_artifact_types/{kind}/import?merge_option=specified',
                              files={'file': (path, 'text/csv')})
        process = data.get('process_id') or data.get('processId')
        print(f'import {len(subset)} {kind} rows -> {code} process={process}')
        if api.dry_run or not process:
            continue
        for _ in range(60):
            st = api.get(f'/v3/governance_artifact_types/import/status/{process}')
            if st.get('status') in ('SUCCEEDED', 'FAILED', 'COMPLETED', 'ERROR'):
                break
            time.sleep(3)
        msgs = st.get('messages') or {}
        rows = msgs.get('resources', []) if isinstance(msgs, dict) else list(msgs)
        total = msgs.get('count', len(rows)) if isinstance(msgs, dict) else len(rows)
        print(f'  status {st.get("status")}; created/modified: {json.dumps(st.get("operations_count", {}))}; '
              f'{total} messages' + (f' (showing {len(rows)})' if total > len(rows) else ''))
        for m in rows:
            print('   ', json.dumps(m)[:200])
        if kind == 'category':
            time.sleep(30)  # new categories take a moment to become searchable before terms can use them
            cats = categories_under(api, root)
    if new_terms:
        print('Terms were imported as drafts: publish them in wxdi, then run "glossary" again to add '
              'extra secondary categories, and then "assign-terms".')
        return
    extra = os.path.join(a.glossary_dir, 'extra_secondary_categories.csv')
    if not os.path.exists(extra):
        return
    for r in read_csv(extra):
        t = terms.get(r['term'])
        cat_id = cats.get(r['secondary_category'])
        if not t or not cat_id:
            print(f'  skip {r["term"]} -> {r["secondary_category"]}: term or category not found')
            continue
        found, version = has_relation(api, t, 'categories', cat_id)
        if found:
            continue
        code, data = api.call('POST', f'/v3/glossary_terms/{t["artifact_id"]}/versions/{version}/relationships',
                              {'categories': [{'id': cat_id}]})
        print(f'  secondary category {r["secondary_category"]} on {r["term"]} -> {code}',
              '(in a draft to publish)' if ok(code) else json.dumps(data)[:200])


def target_containers(api, a):
    out = []
    if a.project:
        pid, _ = find_container(api, 'project', a.project)
        conn = find_connection(api, 'project', pid, a.connection)[0] if a.connection else None
        out.append(('project', pid, schema_assets(api, 'project', pid, a.schema, conn)))
    if a.catalog:
        cid, _ = find_container(api, 'catalog', a.catalog)
        out.append(('catalog', cid, schema_assets(api, 'catalog', cid, a.schema)))
    return out


def cmd_assign_terms(api, a):
    rows = read_csv(os.path.join(a.glossary_dir, 'term_assignments.csv'))
    root = next(r['Name'] for r in read_csv(os.path.join(a.glossary_dir, 'glossary.csv'))
                if r['Artifact Type'] == 'category' and not r['Category'])
    terms = published_terms(api, categories_under(api, root).values())
    missing = sorted({r['term'] for r in rows} - set(terms))
    if missing:
        raise SystemExit(f'{len(missing)} terms are not published yet (e.g. {missing[:5]}); publish them first')
    for kind, cid, assets in target_containers(api, a):
        added = 0
        for table in sorted({r['table'] for r in rows}):
            if table not in assets:
                print(f'  {kind}: no asset for {a.schema}.{table}')
                continue
            asset = api.get(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}')['entity']
            have = {t.get('term_id') for t in (asset.get('asset_terms') or {}).get('list', [])}
            colinfo = asset.get('column_info') or {}
            ops_asset, ops_cols = [], []
            for r in (x for x in rows if x['table'] == table):
                term = {'term_id': terms[r['term']]['global_id'], 'term_display_name': r['term']}
                if not r['column']:
                    if term['term_id'] not in have:
                        ops_asset.append(term)
                    continue
                ci = colinfo.get(r['column'])
                if ci is None:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}', 'value': {'column_terms': [term]}})
                elif 'column_terms' not in ci:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}/column_terms', 'value': [term]})
                elif term['term_id'] not in {t.get('term_id') for t in ci['column_terms']}:
                    ops_cols.append({'op': 'add', 'path': f'/{r["column"]}/column_terms/-', 'value': term})
            if ops_asset:
                if asset.get('asset_terms') is None:
                    code, _ = api.call('POST', f'/v2/assets/{assets[table]}/attributes?{ctx(kind, cid)}',
                                       {'name': 'asset_terms', 'entity': {'list': ops_asset}})
                else:
                    code, _ = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/asset_terms?{ctx(kind, cid)}',
                                       [{'op': 'add', 'path': '/list/-', 'value': t} for t in ops_asset])
                print(f'  {kind} {table}: +{len(ops_asset)} asset term -> {code}')
            if ops_cols:
                code, _ = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/column_info?{ctx(kind, cid)}', ops_cols)
                print(f'  {kind} {table}: +{len(ops_cols)} column terms -> {code}')
            added += len(ops_asset) + len(ops_cols)
        print(f'{kind}: {added} term assignments added')
        if a.remove_other_terms:
            remove_other_terms(api, kind, cid, assets, {t['global_id'] for t in terms.values()})


def remove_other_terms(api, kind, cid, assets, keep):
    for table, aid in sorted(assets.items()):
        ci = api.get(f'/v2/assets/{aid}?{ctx(kind, cid)}')['entity'].get('column_info') or {}
        ops, names = [], []
        for col, v in ci.items():
            terms = v.get('column_terms', [])
            for i in sorted((i for i, t in enumerate(terms) if t.get('term_id') not in keep), reverse=True):
                ops += [{'op': 'test', 'path': f'/{col}/column_terms/{i}/term_id', 'value': terms[i]['term_id']},
                        {'op': 'remove', 'path': f'/{col}/column_terms/{i}'}]
                names.append(f'{col}: {terms[i].get("term_display_name")}')
        if ops:
            code, _ = api.call('PATCH', f'/v2/assets/{aid}/attributes/column_info?{ctx(kind, cid)}', ops)
            print(f'  {kind} {table}: removed other terms {names} -> {code}')


def sql_comments(path, schema):
    """{(table, column or ''): text} from COMMENT ON TABLE/VIEW/COLUMN statements in the SQL script."""
    out = {}
    q = r'"((?:[^"]|"")+)"'
    text = open(path, encoding='utf-8').read()
    for m in re.finditer(rf"^COMMENT ON (?:TABLE|VIEW) {q}\.{q} IS '((?:[^']|'')*)';$", text, re.M):
        if m.group(1) == schema:
            out[(m.group(2), '')] = m.group(3).replace("''", "'")
    for m in re.finditer(rf"^COMMENT ON COLUMN {q}\.{q}\.{q} IS '((?:[^']|'')*)';$", text, re.M):
        if m.group(1) == schema:
            out[(m.group(2), m.group(3))] = m.group(4).replace("''", "'")
    return out


def cmd_descriptions(api, a):
    want = sql_comments(a.sql, a.schema)
    for kind, cid, assets in target_containers(api, a):
        changed = 0
        for table, aid in sorted(assets.items()):
            asset = api.get(f'/v2/assets/{aid}?{ctx(kind, cid)}')
            desc = want.get((table, ''), '')
            if (asset['metadata'].get('description') or '') != desc:
                code, _ = api.patch(f'/v2/assets/{aid}?{ctx(kind, cid)}',
                                    [{'op': 'replace', 'path': '/metadata/description', 'value': desc}])
                print(f'  {kind} {table}: description {"set" if desc else "cleared"} -> {code}')
                changed += 1
            ci = asset['entity'].get('column_info') or {}
            ops = []
            for c in asset['entity']['data_asset'].get('columns', []):
                col, text = c['name'], want.get((table, c['name']))
                if text and ci.get(col, {}).get('column_description') != text:
                    ops.append({'op': 'add', 'path': f'/{col}' if col not in ci else f'/{col}/column_description',
                                'value': {'column_description': text} if col not in ci else text})
            if ops:
                code, _ = api.call('PATCH', f'/v2/assets/{aid}/attributes/column_info?{ctx(kind, cid)}', ops)
                print(f'  {kind} {table}: {len(ops)} column descriptions -> {code}')
                changed += len(ops)
        print(f'{kind}: {changed} descriptions changed')


def cmd_classify(api, a):
    levels = ['review', 'medium', 'high']
    floor = levels.index(getattr(a, 'min_confidence', None) or 'review')
    mapping = {r['Name']: r['Classifications'] for r in read_csv(a.classifications) if r.get('Classifications')
               and levels.index(r.get('confidence') or 'high') >= floor}
    cls = classification_ids(api, set(mapping.values()))
    repo = None
    root = next(r['Name'] for r in read_csv(os.path.join(a.glossary_dir, 'glossary.csv'))
                if r['Artifact Type'] == 'category' and not r['Category'])
    terms = published_terms(api, categories_under(api, root).values())
    if not a.skip_terms:
        for name, cname in sorted(mapping.items()):
            t = terms.get(name)
            if not t:
                print(f'  term {name}: not published, skipped')
                continue
            found, version = has_relation(api, t, 'classifications', cls[cname])
            if found:
                continue
            code, data = api.call('POST', f'/v3/glossary_terms/{t["artifact_id"]}/versions/{version}/relationships',
                                  {'classifications': [{'id': cls[cname]}]})
            print(f'  term {name}: + {cname} -> {code}', '(in a draft to publish)' if ok(code) else json.dumps(data)[:200])
    for t in terms.values():
        repo = t['global_id'].split('_')[0]
        break
    cols = {}
    for r in read_csv(os.path.join(a.glossary_dir, 'term_assignments.csv')):
        if r['column'] and r['term'] in mapping:
            cols.setdefault(r['table'], []).append((r['column'], mapping[r['term']]))
    for kind, cid, assets in target_containers(api, a):
        added = 0
        for table, pairs in sorted(cols.items()):
            if table not in assets:
                continue
            asset = api.get(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}')
            ci = asset['entity'].get('column_info') or {}
            ops = []
            for col, cname in pairs:
                entry = {'id': cls[cname], 'name': cname, 'global_id': f'{repo}_{cls[cname]}'}
                cur = ci.get(col, {})
                if 'column_classifications' not in cur:
                    ops.append({'op': 'add', 'path': f'/{col}/column_classifications', 'value': [entry]})
                elif entry['id'] not in {x.get('id') for x in cur['column_classifications']}:
                    ops.append({'op': 'add', 'path': f'/{col}/column_classifications/-', 'value': entry})
            if ops:
                code, _ = api.call('PATCH', f'/v2/assets/{assets[table]}/attributes/column_info?{ctx(kind, cid)}', ops)
                print(f'  {kind} {table}: +{len(ops)} column classifications -> {code}')
                added += len(ops)
            tags = asset['metadata'].get('tags')
            if a.tag and a.tag not in (tags or []):
                code, _ = api.patch(f'/v2/assets/{assets[table]}?{ctx(kind, cid)}',
                                    [{'op': 'add', 'path': '/metadata/tags/-' if tags else '/metadata/tags',
                                      'value': a.tag if tags else [a.tag]}])
                print(f'  {kind} {table}: tag {a.tag} -> {code}')
        print(f'{kind}: {added} column classifications added')


def cmd_publish(api, a):
    pid, _ = find_container(api, 'project', a.project)
    catid, catname = find_container(api, 'catalog', a.catalog)
    conn_id, conn = find_connection(api, 'project', pid, a.connection)
    user = conn['properties'].get('username')
    if a.expect_user and user != a.expect_user:
        raise SystemExit(f'connection {a.connection} logs in as "{user}", not "{a.expect_user}"; refusing to publish. '
                         'A published connection shares its credentials with catalog users and always follows the '
                         'project connection, so use a read-only login (see reader-sql and connection).')
    cat_conns = api.get(f'/v2/connections?catalog_id={catid}&limit=200').get('resources', [])
    if not any(r['entity']['name'] == conn['name'] for r in cat_conns):
        code, data = api.call('POST', f'/v2/assets/{conn_id}/publish?project_id={pid}', {'catalog_id': catid, 'mode': 0})
        print(f'publish connection {conn["name"]} (user {user}) -> {code}', '' if ok(code) else json.dumps(data)[:300])
    else:
        print(f'connection {conn["name"]} already in {catname}')
    assets = schema_assets(api, 'project', pid, a.schema, conn_id)
    published = schema_assets(api, 'catalog', catid, a.schema)
    for table, aid in sorted(assets.items()):
        if table in published:
            continue
        code, data = api.call('POST', f'/v2/assets/{aid}/publish?project_id={pid}', {'catalog_id': catid, 'mode': 0})
        print(f'  publish {a.schema}.{table} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    print(f'{len(assets)} assets in the project, {len(published)} were already in {catname}')
    if a.note:
        cat = api.get(f'/v2/catalogs/{catid}')
        if not cat['entity'].get('description'):
            code, _ = api.patch(f'/v2/catalogs/{catid}', [{'op': 'add', 'path': '/entity/description', 'value': a.note}])
            print(f'catalog description set -> {code}')


# ---------- project, data source definition, lineage ----------

def account_storage(api):
    """(cos guid, cos crn) for new projects: the Cloud Object Storage the account's projects already use."""
    guids = [p['storage'].get('guid') for p in containers(api, 'project') if p['storage'].get('guid')]
    code, inst = api.call('GET', 'https://resource-controller.cloud.ibm.com/v2/resource_instances?limit=100', write=False)
    cos = {r['guid']: r['crn'] for r in inst.get('resources', []) if r['crn'].split(':')[4] == 'cloud-object-storage'}
    for g in guids + list(cos):
        if g in cos:
            return g, cos[g]
    raise SystemExit('No Cloud Object Storage instance found in this account for the project; create the project in the UI')


def cmd_project(api, a):
    hit = [p for p in containers(api, 'project') if p['name'].lower() == a.name.lower()]
    if hit:
        print(f'project {hit[0]["name"]} exists ({hit[0]["id"]})')
        return hit[0]['id']
    guid, crn = account_storage(api)
    body = {'name': a.name, 'description': a.description or f'erwin model {a.name}', 'generator': 'erwin-wxdi-import',
            'public': False, 'storage': {'type': 'bmcos_object_storage', 'guid': guid, 'resource_crn': crn}}
    code, data = api.call('POST', '/transactional/v2/projects', body)
    print(f'create project {a.name} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    if not ok(code):
        raise SystemExit(1)
    return None if api.dry_run else data.get('location', '').rstrip('/').split('/')[-1]


def platform_catalog(api):
    rows = api.get('/v2/catalogs?limit=100').get('catalogs', [])
    hit = [r for r in rows if r['entity'].get('uid') == 'ibm-global-catalog' or
           r['entity']['name'] == 'Platform assets catalog']
    if not hit:
        raise SystemExit('Platform assets catalog not found; data source definitions live there')
    return hit[0]['metadata']['guid']


def postgres_type_id(api):
    offset = 0
    while True:  # pages of at most 100
        page = api.get(f'/v2/datasource_types?limit=100&offset={offset}').get('resources', [])
        if not page:
            raise SystemExit('No postgresql datasource type in this wxdi account')
        hit = next((t['metadata']['asset_id'] for t in page if t['entity']['name'] == 'postgresql'), None)
        if hit:
            return hit
        offset += 100


def find_datasource(api, pac, host, port, database):
    code, data = api.call('POST', f'/v2/asset_types/ibm_data_source/search?catalog_id={pac}',
                          {'query': '*:*', 'limit': 200, 'include': 'entity'}, write=False)
    for r in data.get('results', []) if ok(code) else []:
        ds = r.get('entity', {}).get('ibm_data_source') or api.get(
            f'/v2/assets/{r["metadata"]["asset_id"]}?catalog_id={pac}')['entity'].get('ibm_data_source', {})
        for e in ds.get('data_source_endpoints', {}).get('values', []):
            if e.get('host') == host and str(e.get('port')) == str(port) and e.get('physical_collection', database) == database:
                return r['metadata']['asset_id'], r['metadata']['name']
    return None, None


def cmd_datasource(api, a):
    """A data source definition (DSD) for the .env Postgres: lineage imports need one."""
    pg, pac = pg_settings(), platform_catalog(api)
    dsd, name = find_datasource(api, pac, pg['host'], pg['port'], pg['database'])
    if dsd:
        print(f'data source definition {name} exists for {pg["host"]}:{pg["port"]}/{pg["database"]} ({dsd})')
        return dsd
    body = {'metadata': {'name': a.name, 'asset_type': 'ibm_data_source', 'origin_country': 'us',
                         'description': f'PostgreSQL {pg["host"]}:{pg["port"]}/{pg["database"]}'},
            'entity': {'ibm_data_source': {
                'data_source_type_id': postgres_type_id(api), 'data_source_state': 'active',
                'data_source_endpoints': {'values': [{'host': pg['host'], 'port': int(pg['port']),
                                                      'physical_collection': pg['database']}]}}}}
    code, data = api.call('POST', f'/v2/assets?catalog_id={pac}', body)
    print(f'create data source definition {a.name} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    if not ok(code):
        raise SystemExit('Create the data source definition in the UI (Administration > Data source definitions), '
                         'then run this again')
    return None if api.dry_run else data['metadata']['asset_id']


def lineage_search(api, name, dsd=None):
    body = {'query': name, 'filters': [{'type': 'data_source_definition_asset_id', 'values': [dsd]}] if dsd else []}
    code, data = api.call('POST', '/gov_lineage/v2/search_lineage_assets', body, write=False)
    return data.get('lineage_assets', []) if ok(code) else []


def cmd_lineage_check(api, a):
    """For each view in the SQL script: does lineage show the tables feeding it?"""
    views = re.findall(rf'CREATE (?:OR REPLACE )?VIEW "?{re.escape(a.schema)}"?\."?(\w+)"?',
                       open(a.sql, encoding='utf-8').read(), re.I)
    pac = platform_catalog(api)
    pg = pg_settings()
    dsd, _ = find_datasource(api, pac, pg['host'], pg['port'], pg['database'])
    good = 0
    for v in views:
        hits = [x for x in lineage_search(api, v, dsd) if x['name'] == v and x.get('type') == 'View']
        hits.sort(key=lambda x: a.schema not in [h['name'] for h in x.get('hierarchical_path', [])])
        if not hits:
            print(f'  view {v}: not in lineage yet')
            continue
        vid = hits[0]['id']
        code, g = api.call('POST', '/gov_lineage/v2/query_lineage', {
            'initial_asset_ids': [vid], 'visible_asset_ids': [vid], 'allow_lineage_cache': 'false',
            'expansion': {'starting_asset_ids': [vid], 'incoming_steps': '3', 'outgoing_steps': '0'}}, write=False)
        assets = {x['id']: x for x in g.get('assets_in_view', [])}
        tables = sorted({x['name'] for x in assets.values() if x.get('type') == 'Table'})
        edges = len(g.get('edges_in_view', []))
        good += bool(tables)
        print(f'  view {v}: {edges} edges, from tables {", ".join(tables) or "none"}')
    print(f'{good} of {len(views)} views have table lineage')
    return good, len(views)


# ---------- glossary publishing ----------

def term_states(api, glossary_dir):
    """Publishing state of this model's terms: (published names, names with a pending draft, names not found)."""
    rows = read_csv(os.path.join(glossary_dir, 'glossary.csv'))
    root = next(r['Name'] for r in rows if r['Artifact Type'] == 'category' and not r['Category'])
    wanted = [r['Name'] for r in rows if r['Artifact Type'] == 'glossary_term']
    cats = categories_under(api, root)
    published = published_terms(api, cats.values()) if cats else {}
    # editable_versions lists a pending draft first, then the published version
    drafts = [n for n in wanted if n in published and len(editable_versions(api, published[n])) > 1]
    missing = [n for n in wanted if n not in published]
    return sorted(published), drafts, missing


def cmd_publish_status(api, a):
    published, drafts, missing = term_states(api, a.glossary_dir)
    print(f'{len(published)} terms published, {len(drafts)} with changes waiting to be published, '
          f'{len(missing)} not published yet')
    for n in drafts[:10]:
        print(f'  waiting: {n}')
    for n in missing[:10]:
        print(f'  not published: {n}')
    return published, drafts, missing


# ---------- teardown ----------

def cmd_teardown(api, a):
    """Removes what a load created, narrowest first. Without --yes it only lists what it would delete."""
    do = a.yes and not api.dry_run
    say = (lambda s: print('  delete ' + s)) if do else (lambda s: print('  would delete ' + s))
    pg = pg_settings()
    schema = a.schema
    for c in containers(api, 'catalog'):
        if c['name'].lower() != a.name.lower():
            continue
        assets = schema_assets(api, 'catalog', c['id'], schema)
        for table, aid in sorted(assets.items()):
            say(f'catalog asset {schema}.{table}')
            if do:
                api.call('DELETE', f'/v2/assets/{aid}?catalog_id={c["id"]}&purge_on_delete=true')
        for r in api.get(f'/v2/connections?catalog_id={c["id"]}&limit=200').get('resources', []):
            if r['entity']['name'] == f'{schema}_reader':
                say(f'catalog connection {r["entity"]["name"]}')
                if do:
                    api.call('DELETE', f'/v2/connections/{r["metadata"]["asset_id"]}?catalog_id={c["id"]}')
        print(f'  (the catalog {c["name"]} itself is kept; delete it in the UI if you want)')
    project_left = False
    for p in containers(api, 'project'):
        if p['name'].lower() == a.name.lower():
            say(f'project {p["name"]} ({p["id"]}) with its connections, metadata imports and assets')
            if do:
                code, data = api.call('DELETE', f'/transactional/v2/projects/{p["id"]}')
                print(f'    -> {code}', '' if ok(code) else json.dumps(data)[:200])
                project_left = project_left or not ok(code)
    if not project_left:  # the project only held a reference; the platform connection itself outlives it
        pac = platform_catalog(api)
        for r in api.get(f'/v2/connections?catalog_id={pac}&limit=200').get('resources', []):
            if r['entity']['name'] == f'{schema}_reader':
                say(f'platform connection {r["entity"]["name"]}')
                if do:
                    api.call('DELETE', f'/v2/connections/{r["metadata"]["asset_id"]}?catalog_id={pac}')
    if a.datasource and project_left:
        # the project's connections and imports still point at it; without it the UI can't delete them either
        print('  kept the data source definition: the project is still there (cancel any running import, then '
              'run teardown again)')
    elif a.datasource:
        pac = platform_catalog(api)
        dsd, name = find_datasource(api, pac, pg['host'], pg['port'], pg['database'])
        if dsd:
            say(f'data source definition {name}')
            if do:
                api.call('DELETE', f'/v2/assets/{dsd}?catalog_id={pac}&purge_on_delete=true')
    if a.glossary_dir:
        published, drafts, missing = term_states(api, a.glossary_dir)
        print(f'  glossary: {len(published)} published terms under the root category. Deleting them creates one '
              'deletion task per term for a person to approve, so it is not done here; '
              'see references/wxdi-end-to-end.md section 12.')
    say(f'Postgres schema {schema} (CASCADE) and login {schema}_reader on {pg["host"]}/{pg["database"]}')
    if do and a.drop_schema:
        run_sql_text(f'DROP SCHEMA IF EXISTS {schema} CASCADE;\n'
                     f'DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = \'{schema}_reader\') THEN '
                     f'EXECUTE \'DROP OWNED BY {schema}_reader\'; EXECUTE \'DROP ROLE {schema}_reader\'; END IF; END $$;')
    elif do:
        print('    (skipped: pass --drop-schema too, it deletes the data)')
    if not do:
        print('Nothing was deleted. Re-run with --yes to delete these.')


# ---------- run-all ----------

def run_sql_text(sql, user=None, password=None):
    """Runs SQL against the .env Postgres and returns the rows of the last statement."""
    import psycopg2
    pg = pg_settings()
    conn = psycopg2.connect(host=pg['host'], port=pg['port'], dbname=pg['database'], user=user or pg['user'],
                            password=password or pg['password'], sslmode=pg['sslmode'], connect_timeout=20)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def reader_connection_limit():
    """Room for wxdi: its lineage extractor opens many connections and keeps a pool of idle ones afterwards.
    Leaves 3 for the admin login and the user's own tools; at most 30."""
    total, reserved = run_sql_text("select current_setting('max_connections')::int, "
                                   "current_setting('superuser_reserved_connections')::int")[0]
    return max(5, min(30, total - reserved - 3))


def end_idle_sessions(role):
    """wxdi keeps idle pooled sessions open after an import; on a small server they block the next run."""
    n = run_sql_text(f"select count(*) filter (where pg_terminate_backend(pid)) from pg_stat_activity "
                     f"where usename = '{role}' and state = 'idle'")[0][0]
    if n:
        print(f'  ended {n} idle {role} sessions left from an earlier run')


def env_secret(key):
    """A password kept in the .env file (created there if missing), so it never shows in chat or logs."""
    if setting(key):
        return setting(key)
    import secrets
    value = secrets.token_hex(16)
    path = ENV_FILE or os.path.join(PROJECT_HOME, '.env')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'\n{key}={value}\n')
    ENV[key.lower()] = value
    print(f'  generated {key} into {path}')
    return value


class State:
    """work/<schema>/state.json: which steps are done, so run-all can stop for the user and resume."""

    def __init__(self, path):
        self.path = path
        self.data = json.load(open(path)) if os.path.exists(path) else {'done': {}}

    def done(self, step):
        return step in self.data['done']

    def mark(self, step, info=True):
        self.data['done'][step] = info
        with open(self.path, 'w') as f:
            json.dump(self.data, f, indent=1)


def ns(**kw):
    return argparse.Namespace(**kw)


def cmd_run_all(api, a):
    name = a.name
    schema = a.schema or re.sub(r'[^0-9A-Za-z]+', '_', name).strip('_').lower()
    work = os.path.join(PROJECT_HOME, 'work', schema)
    sql = os.path.join(work, f'{schema}.postgres.sql')
    gdir = os.path.join(work, 'glossary')
    reader, conn_name, pw_key = f'{schema}_reader', f'{schema}_reader', f'{schema}_reader_password'
    os.makedirs(work, exist_ok=True)
    st = State(os.path.join(work, 'state.json'))
    pg = pg_settings()

    def step(title):
        print(f'\n== {title}')

    def pause(msg, code=3):
        print(f'\nPAUSED: {msg}\nRun run-all again with the same arguments when that is done.')
        sys.exit(code)

    # 1. generate
    step('1. extract the model')
    if not st.done('generate') or a.regenerate:
        if not a.model:
            sys.exit('Pass --model PATH/TO/MODEL.erwin')
        res = subprocess.run([sys.executable, '-I', os.path.join(PROJECT_HOME, 'scripts', 'erwin2postgres.py'),
                              os.path.expanduser(a.model), '--schema', schema, '--out', sql, '--glossary', gdir,
                              '--metadata', os.path.join(work, 'metadata'), '--category', name],
                             capture_output=True, text=True)
        print(res.stdout + res.stderr)
        if res.returncode:
            sys.exit('extraction failed')
        st.mark('generate', os.path.abspath(os.path.expanduser(a.model)))
    else:
        print(f'  done earlier: {sql}')

    # 2. confirm the plan once
    if not st.done('confirmed'):
        step('2. plan')
        cats = [r['Name'] for r in read_csv(os.path.join(gdir, 'glossary.csv')) if r['Artifact Type'] == 'category']
        nterms = sum(1 for r in read_csv(os.path.join(gdir, 'glossary.csv')) if r['Artifact Type'] == 'glossary_term')
        sugg = read_csv(os.path.join(gdir, 'classification_suggestions.csv'))
        high = [r for r in sugg if r['confidence'] == 'high' and r['Classifications']]
        print(f'  Postgres  {pg["user"]}@{pg["host"]}:{pg["port"]}/{pg["database"]}: schema {schema}, '
              f'read-only login {reader}\n'
              f'  wxdi      account {api.account} ({api.user}), {api.url}\n'
              f'  project   {name}; connection {conn_name}; data source definition; metadata import with lineage\n'
              f'  catalog   {name} (must exist; created in the UI)\n'
              f'  glossary  root category {name} with {", ".join(c for c in cats if c != name)}; {nterms} terms\n'
              f'  classify  {len(high)} high-confidence terms automatically; '
              f'{len(sugg) - len(high)} others are listed, not applied')
        if api.dry_run:
            pass  # a dry run never records a confirmation
        elif not a.confirm:
            pause('confirm this plan with the user, then pass --confirm', 2)
        else:
            st.mark('confirmed')

    if api.dry_run:
        print('\n(dry run: stopping before the first change. The steps below create the schema, the read-only login, '
              f'the project {name}, the connection and data source definition, and run the import.)')
        cmd_check(api, ns(name=name, schema=schema))
        return

    # 3. schema and objects
    step('3. Postgres schema and objects')
    if not st.done('database'):
        n = run_sql_text("select count(c.oid) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                         f"where n.nspname = '{schema}'")[0][0]
        if n:
            sys.exit(f'schema {schema} already holds {n} objects; drop it or use another --schema')
        run_sql_text(open(sql, encoding='utf-8').read())
        st.mark('database')
    print('  ' + ', '.join(f'{k} {v}' for k, v in run_sql_text(
        "select case c.relkind when 'r' then 'tables' when 'v' then 'views' end, count(*) from pg_class c "
        f"join pg_namespace n on n.oid = c.relnamespace where n.nspname = '{schema}' and c.relkind in ('r','v') "
        "group by 1 order by 1")))

    # 4. read-only login, tested
    step('4. read-only login')
    password = env_secret(pw_key)
    if not run_sql_text(f"select 1 from pg_roles where rolname = '{reader}'"):
        limit = reader_connection_limit()
        role_sql = (f"CREATE ROLE {reader} LOGIN PASSWORD '{password}' CONNECTION LIMIT {limit};\n"
                    f"GRANT CONNECT ON DATABASE {pg['database']} TO {reader};\n"
                    f"GRANT USAGE ON SCHEMA {schema} TO {reader};\n"
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader};\n"
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {reader};")
        run_sql_text(role_sql)
        print(f'  created {reader}')
    import psycopg2
    table = run_sql_text(f"select table_name from information_schema.tables where table_schema = '{schema}' "
                         "and table_type = 'BASE TABLE' order by 1 limit 1")[0][0]
    run_sql_text(f'select count(*) from {schema}.{table}', reader, password)
    try:
        run_sql_text(f'create table {schema}.erwin_wxdi_write_test (i int)', reader, password)
        run_sql_text(f'drop table {schema}.erwin_wxdi_write_test')
        sys.exit(f'{reader} could create a table: it is not read-only. Fix its grants before going on.')
    except psycopg2.Error:
        print(f'  {reader} can read {schema} and cannot create objects')

    # 5. wxdi project, connection, data source definition
    step('5. project, connection, data source definition')
    cmd_project(api, ns(name=name, description=f'erwin model {name}, loaded by erwin-wxdi-import'))
    project = name
    cmd_connection(api, ns(project=project, copy_from=None, from_project=None, name=conn_name, username=reader,
                           password_env=pw_key, description=None))
    dsd = cmd_datasource(api, ns(name=f'{pg["host"].split(".")[0]} {pg["database"]}'))

    # 6. metadata import with lineage
    step('6. metadata import with lineage')
    if not st.done('import'):
        end_idle_sessions(reader)
        state = cmd_import(api, ns(project=project, schema=schema, connection=conn_name, name=f'{schema}_mdi',
                                   lineage=True, datasource=dsd, database=pg['database']))
        if state not in ('Completed', 'CompletedWithWarnings'):
            sys.exit(f'The metadata import run ended {state}. Look at the run in the project (Assets > Metadata '
                     f'imports > {schema}_mdi), fix it, and run run-all again: it re-runs the import.')
        st.mark('import')
    else:
        print('  done earlier')
    good, nviews = cmd_lineage_check(api, ns(schema=schema, sql=sql))
    if nviews and good < nviews:
        print('  NOTE: not every view has table lineage. It can lag the import by a few minutes (run lineage-check '
              'later); if it stays empty, see "View lineage and the read-only login" in references/wxdi-end-to-end.md. '
              'The load goes on without it.')

    # 7. catalog (made by the user)
    step('7. catalog')
    hit = [c for c in containers(api, 'catalog') if c['name'].lower() == name.lower()]
    if not hit:
        pause(f'create the catalog "{name}" in the UI (see SKILL.md, Manual step)')
    catalog = hit[0]['name']
    print(f'  catalog {catalog} exists')

    # 8. glossary: categories and terms, then the user publishes
    step('8. glossary categories and terms')
    if not st.done('glossary'):
        cmd_glossary(api, ns(glossary_dir=gdir))
        if not api.dry_run:
            st.mark('glossary')
    published, drafts, missing = term_states(api, gdir)
    if missing:
        pause(f'{len(missing)} imported terms are drafts. Ask the user to publish them in wxdi '
              '(task inbox: the glossary import task, Publish). Don\'t publish or approve for them.')
    print(f'  {len(published)} terms published')

    # 9. term changes: secondary categories, high-confidence classifications; then the user publishes
    step('9. secondary categories and classifications on terms')
    mapping = os.path.join(work, 'classifications_high.csv')
    sugg = read_csv(os.path.join(gdir, 'classification_suggestions.csv'))
    with open(mapping, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['Name', 'Classifications'], extrasaction='ignore')
        w.writeheader()
        w.writerows(r for r in sugg if r['confidence'] == 'high' and r['Classifications'])
    if not st.done('term-changes'):
        cmd_glossary(api, ns(glossary_dir=gdir))  # terms are published now: adds the extra secondary categories
        cmd_classify(api, ns(classifications=mapping, glossary_dir=gdir, project=None, catalog=None, schema=schema,
                             connection=None, tag=DERIVED_TAG, skip_terms=False))
        if not api.dry_run:
            st.mark('term-changes')
    published, drafts, missing = term_states(api, gdir)
    if drafts:
        pause(f'{len(drafts)} terms have changes waiting (secondary categories, classifications). Ask the user to '
              'publish them in wxdi; each changed term is its own task.')
    others = [r for r in sugg if not (r['confidence'] == 'high' and r['Classifications'])]
    if others:
        print(f'  not applied ({len(others)}, for the user to decide): ' +
              '; '.join(f'{r["Name"]} ({r["confidence"]}{": " + r["Classifications"] if r["Classifications"] else ""})'
                        for r in others))

    # 10. project assets: terms, column classifications, descriptions
    step('10. terms, classifications and descriptions on the project assets')
    common = dict(project=project, catalog=None, schema=schema, connection=conn_name, glossary_dir=gdir)
    cmd_assign_terms(api, ns(remove_other_terms=False, **common))
    cmd_classify(api, ns(classifications=mapping, tag=DERIVED_TAG, skip_terms=True, **common))
    cmd_descriptions(api, ns(sql=sql, **{k: v for k, v in common.items() if k != 'glossary_dir'}))

    # 11. publish to the catalog
    step('11. publish to the catalog')
    cmd_publish(api, ns(project=project, catalog=catalog, schema=schema, connection=conn_name, expect_user=reader,
                        note=f'Assets from the erwin model {name}. Terms, categories and descriptions come from the '
                             f'model; classifications on assets tagged {DERIVED_TAG} are derived from term names, '
                             'not modelled.'))

    # 12. report
    step('12. result')
    cmd_status(api, ns(project=project, catalog=catalog, schema=schema, category=name))
    st.mark('complete')
    print('\nDone.')


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--env')
    env_file = load_env(pre.parse_known_args()[0].env)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--env', help='settings file (default: ./.env, then the project root .env)')
    ap.add_argument('--url', default=setting('di_service_url'), help='wxdi API URL (or di_service_url in .env)')
    ap.add_argument('--apikey-file', help='IBM Cloud API key JSON file (or ibm_cloud_api_key in .env, or DI_APIKEY)')
    ap.add_argument('--dry-run', action='store_true', help='read and report only; make no changes')
    sub = ap.add_subparsers(dest='step', required=True)

    def common(p, project=True, catalog=False, glossary=False):
        p.add_argument('--schema', required=True)
        if project:
            p.add_argument('--project', required=True, help='project name or id')
        if catalog:
            p.add_argument('--catalog', help='catalog name or id')
        if glossary:
            p.add_argument('--glossary-dir', required=True, help='directory written by erwin2postgres.py --glossary')
        return p

    p = sub.add_parser('check', help='read-only readiness check; run it first')
    p.add_argument('--name', required=True, help='model name: the project, catalog and root category, e.g. eMovies')
    p.add_argument('--schema', help='Postgres schema (default: the name in snake_case)')
    p = common(sub.add_parser('status'), catalog=True)
    p.add_argument('--category', help='root glossary category to report on')
    p = sub.add_parser('run-sql')
    p.add_argument('--file', required=True, help='SQL script, or - for stdin')
    p.add_argument('--user', help='log in as this user instead of postgres_user (with --password-env)')
    p.add_argument('--password-env', help='environment or .env variable holding that user\'s password')
    p = sub.add_parser('reader-sql')
    p.add_argument('--schema', required=True)
    p.add_argument('--database', help='default: postgres_database in .env, else postgres')
    p.add_argument('--role')
    p.add_argument('--password-env', help='environment or .env variable holding the new login\'s password')
    p.add_argument('--connection-limit', type=int, default=20,
                   help='the login\'s connection limit (wxdi lineage extraction opens many; default 20)')
    p = sub.add_parser('connection')
    p.add_argument('--project', required=True)
    p.add_argument('--copy-from', help='existing connection to copy host/port/database from (default: the .env Postgres)')
    p.add_argument('--from-project', help='project that holds --copy-from (default: the same project)')
    p.add_argument('--name', required=True)
    p.add_argument('--username', required=True)
    p.add_argument('--password-env', required=True, help='environment or .env variable holding the password')
    p.add_argument('--description')
    p = sub.add_parser('project', help='create the project in this account (on its existing storage)')
    p.add_argument('--name', required=True)
    p.add_argument('--description')
    p = sub.add_parser('datasource', help='create a data source definition for the .env Postgres (lineage needs it)')
    p.add_argument('--name', required=True)
    p = common(sub.add_parser('import'))
    p.add_argument('--connection', required=True)
    p.add_argument('--name', required=True, help='metadata import name')
    p.add_argument('--lineage', action='store_true', help='lineage and discovery, through the data source definition')
    p.add_argument('--lineage-only', action='store_true', help='lineage without creating assets')
    p.add_argument('--datasource', help='data source definition id (default: the one for the .env Postgres)')
    p = sub.add_parser('lineage-check', help='do the views have lineage from their tables?')
    p.add_argument('--schema', required=True)
    p.add_argument('--sql', required=True, help='the SQL script, for the view names')
    sub.add_parser('publish-status', help='which of the model\'s terms are published or wait for publishing'
                   ).add_argument('--glossary-dir', required=True)
    p = sub.add_parser('run-all', help='the whole load, in order; stops for the user and resumes')
    p.add_argument('--name', required=True, help='model name: project, catalog and root category')
    p.add_argument('--model', help='the .erwin file (needed the first time)')
    p.add_argument('--schema', help='Postgres schema (default: the name in snake_case)')
    p.add_argument('--confirm', action='store_true', help='the user has confirmed the plan run-all printed')
    p.add_argument('--regenerate', action='store_true', help='extract the model again')
    p = sub.add_parser('teardown', help='list (or with --yes delete) what a load created')
    p.add_argument('--name', required=True)
    p.add_argument('--schema', required=True)
    p.add_argument('--glossary-dir', help='also report the glossary terms (they are not deleted)')
    p.add_argument('--datasource', action='store_true', help='also delete the data source definition')
    p.add_argument('--drop-schema', action='store_true', help='also drop the Postgres schema and the read-only login')
    p.add_argument('--yes', action='store_true', help='really delete')
    sub.add_parser('glossary').add_argument('--glossary-dir', required=True)
    p = common(sub.add_parser('assign-terms'), catalog=True, glossary=True)
    p.add_argument('--connection', help='only project assets imported through this connection')
    p.add_argument('--remove-other-terms', action='store_true',
                   help='also remove column terms that are not from this glossary (e.g. earlier automatic ones)')
    p = common(sub.add_parser('descriptions'), catalog=True)
    p.add_argument('--sql', required=True, help='the SQL script from erwin2postgres.py')
    p.add_argument('--connection')
    p = common(sub.add_parser('classify'), catalog=True, glossary=True)
    p.add_argument('--classifications', required=True, help='CSV with Name and Classifications columns (reviewed)')
    p.add_argument('--connection')
    p.add_argument('--tag', default=DERIVED_TAG, help=f'tag for assets with classified columns ("" for none)')
    p.add_argument('--skip-terms', action='store_true', help='only classify columns, not the terms')
    p.add_argument('--min-confidence', choices=['high', 'medium', 'review'],
                   help='only rows with this confidence or higher (rows without one count as high)')
    p = common(sub.add_parser('publish'))
    p.add_argument('--catalog', required=True)
    p.add_argument('--connection', required=True, help='project connection the assets were imported through')
    p.add_argument('--expect-user', help='refuse unless the connection logs in as this user')
    p.add_argument('--note', help='set the catalog description to this text if it is empty')
    a = ap.parse_args()

    if a.step in ('reader-sql', 'run-sql'):
        return {'reader-sql': cmd_reader_sql, 'run-sql': cmd_run_sql}[a.step](None, a)
    if not a.url:
        sys.exit(f'Set di_service_url in .env ({env_file or "none found"}) or pass --url')
    key = setting('DI_APIKEY', 'ibm_cloud_api_key')
    if a.apikey_file:
        key = json.load(open(os.path.expanduser(a.apikey_file)))['apikey']
    if not key:
        sys.exit('Set ibm_cloud_api_key in .env, or DI_APIKEY, or pass --apikey-file')
    api = Api(a.url, key, a.dry_run)
    if a.dry_run:
        print('(dry run: no changes will be made)')
    {'check': cmd_check, 'project': cmd_project, 'datasource': cmd_datasource, 'lineage-check': cmd_lineage_check,
     'publish-status': cmd_publish_status, 'run-all': cmd_run_all, 'teardown': cmd_teardown,
     'status': cmd_status, 'connection': cmd_connection, 'import': cmd_import, 'glossary': cmd_glossary,
     'assign-terms': cmd_assign_terms, 'descriptions': cmd_descriptions, 'classify': cmd_classify,
     'publish': cmd_publish}[a.step](api, a)


if __name__ == '__main__':
    main()
