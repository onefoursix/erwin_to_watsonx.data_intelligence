"""Shared watsonx.data intelligence (SaaS) client for the erwin skills: settings, REST client, lookups.

This file is copied into each skill's scripts/ folder by tools/sync.py; edit shared/wxdi_common.py, then run
the sync. It needs only the Python standard library and curl.

Settings come from environment variables, then a .env file: --env FILE, else ./.env, else the .env of the
project home (the first folder above this script that has one). Keys used here:
  ibm_cloud_api_key  IBM Cloud API key (or DI_APIKEY)
  di_service_url     wxdi API URL, e.g. https://api.ca-tor.dai.cloud.ibm.com (or DI_SERVICE_URL)
"""
import argparse
import base64
import csv
import json
import os
import subprocess
import sys
import time

IAM_URL = 'https://iam.cloud.ibm.com/identity/token'
GLOSSARY_SEP = ' >> '


def project_home():
    """The folder that holds .env and work/: ERWIN_WXDI_HOME, else the first folder above this script with a
    .env (or a work/ folder), else the current directory."""
    if os.environ.get('ERWIN_WXDI_HOME'):
        return os.path.expanduser(os.environ['ERWIN_WXDI_HOME'])
    d = os.path.dirname(os.path.realpath(__file__))
    while d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, '.env')) or os.path.isdir(os.path.join(d, 'work')):
            return d
        d = os.path.dirname(d)
    return os.getcwd()


PROJECT_HOME = project_home()
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


def find_container(api, kind, name_or_id):
    """kind: 'project' or 'catalog'. Returns (id, name)."""
    items = [(c['id'], c['name']) for c in containers(api, kind)]
    hits = [i for i in items if name_or_id in i] or [i for i in items if i[1].lower() == name_or_id.lower()]
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


def platform_catalog(api):
    rows = api.get('/v2/catalogs?limit=100').get('catalogs', [])
    hit = [r for r in rows if r['entity'].get('uid') == 'ibm-global-catalog' or
           r['entity']['name'] == 'Platform assets catalog']
    if not hit:
        raise SystemExit('Platform assets catalog not found; data source definitions live there')
    return hit[0]['metadata']['guid']


def account_storage(api):
    """(cos guid, cos crn) for new projects: the Cloud Object Storage the account's projects already use."""
    guids = [p['storage'].get('guid') for p in containers(api, 'project') if p['storage'].get('guid')]
    code, inst = api.call('GET', 'https://resource-controller.cloud.ibm.com/v2/resource_instances?limit=100', write=False)
    cos = {r['guid']: r['crn'] for r in inst.get('resources', []) if r['crn'].split(':')[4] == 'cloud-object-storage'}
    for g in guids + list(cos):
        if g in cos:
            return g, cos[g]
    raise SystemExit('No Cloud Object Storage instance found in this account for the project; create the project in the UI')


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


def datasource_type_id(api, name):
    """Asset id of a connection type, e.g. 'postgresql' or 'sqlserver'."""
    offset = 0
    while True:  # pages of at most 100
        page = api.get(f'/v2/datasource_types?limit=100&offset={offset}').get('resources', [])
        if not page:
            raise SystemExit(f'No {name} datasource type in this wxdi account')
        hit = next((t['metadata']['asset_id'] for t in page if t['entity']['name'] == name), None)
        if hit:
            return hit
        offset += 100


def work_dir(schema):
    """work/<schema>/ in the project home: where the export skill leaves the SQL, viewer and metadata.json."""
    return os.path.join(PROJECT_HOME, 'work', schema)


def common_args(description):
    """An ArgumentParser with the options every script shares, after reading .env."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--env')
    load_env(pre.parse_known_args()[0].env)
    ap = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--env', help='settings file (default: ./.env, then the project home .env)')
    ap.add_argument('--url', default=setting('di_service_url'), help='wxdi API URL (or di_service_url in .env)')
    ap.add_argument('--apikey-file', help='IBM Cloud API key JSON file (or ibm_cloud_api_key in .env, or DI_APIKEY)')
    ap.add_argument('--dry-run', action='store_true', help='read and report only; make no changes')
    return ap


def connect(a):
    """The Api for parsed arguments from common_args()."""
    if not a.url:
        sys.exit(f'Set di_service_url in .env ({ENV_FILE or "none found"}) or pass --url')
    key = setting('DI_APIKEY', 'ibm_cloud_api_key')
    if a.apikey_file:
        key = json.load(open(os.path.expanduser(a.apikey_file)))['apikey']
    if not key:
        sys.exit('Set ibm_cloud_api_key in .env, or DI_APIKEY, or pass --apikey-file')
    api = Api(a.url, key, a.dry_run)
    if a.dry_run:
        print('(dry run: no changes will be made)')
    return api
