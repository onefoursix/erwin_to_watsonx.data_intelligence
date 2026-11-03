#!/usr/bin/env python3
"""Put a schema's tables and views into a watsonx.data intelligence (SaaS) project with a metadata import,
with or without lineage, from PostgreSQL or SQL Server.

Steps, in the usual order (run-all does them in sequence; every step is safe to re-run and --dry-run shows
what it would do):
  check       read-only: the database login, the schema, the wxdi key, project, connection and lineage support
  deploy      create the schema's tables and views from a SQL script (e.g. erwin-model-export's), all or nothing
  reader      create a read-only login for the schema (password generated into .env), and test it
  project     create the wxdi project (on the account's existing Cloud Object Storage)
  connection  a platform connection to the database through the read-only login, added to the project
  datasource  a data source definition for the database (lineage needs one)
  import      create and run the metadata import (primary and foreign keys); --lineage adds lineage
  lineage-check  for each view: lineage edges and the tables they come from
  status      what the project holds for the schema
  run-all     check, [deploy], reader, project, connection, [datasource], import, [lineage-check]

Settings come from .env in the project home (see wxdi_common.py): ibm_cloud_api_key, di_service_url, and for
the database either postgres_host/port/database/user/password/sslmode (+ postgres_cert for a private CA) or
sqlserver_host/port/db/user/password. The user in .env is the admin login; the import itself goes through the
read-only login.
"""
import argparse
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time

from wxdi_common import (common_args, connect, containers, datasource_type_id, env_secret, find_connection,
                         find_container, find_datasource, ok, platform_catalog, account_storage, schema_assets,
                         setting, work_dir)

# Lineage settings per database. Found on real accounts (2026-11): PostgreSQL view lineage needed the
# parallel reader; with it, the SQL Server extractor fails before connecting ("Job failed during extraction"),
# and the UI default (scenarioReaderPrefetch) works.
LINEAGE = {
    'postgres': {'technology': 'PostgreSQL', 'input_reader': 'parallel'},
    'sqlserver': {'technology': 'Microsoft SQL Server', 'input_reader': 'scenarioReaderPrefetch'},
}
SCAN_PHASES = ['dictionary_processing', 'extraction_of_transformations', 'processing_of_external_inputs',
               'processing_of_extracted_inputs']
ACTIVE = ('Queued', 'Starting', 'Running')


# ---------- the database ----------

class Db:
    """The database in .env: kind 'postgres' or 'sqlserver'."""

    def __init__(self, kind):
        self.kind = kind
        if kind == 'postgres':
            self.host, self.port = setting('postgres_host'), setting('postgres_port', default='5432')
            self.database = setting('postgres_database', 'postgres_db', default='postgres')
            self.user, self.password = setting('postgres_user'), setting('postgres_password')
            self.sslmode = setting('postgres_sslmode', default='require')
            self.type_name = 'postgresql'
        else:
            self.host, self.port = setting('sqlserver_host'), setting('sqlserver_port', default='1433')
            self.database = setting('sqlserver_db', 'sqlserver_database')
            self.user, self.password = setting('sqlserver_user'), setting('sqlserver_password')
            self.sslmode = None
            self.type_name = 'sqlserver'
        self.technology = LINEAGE[kind]['technology']

    def describe(self, user=None):
        return f'{user or self.user}@{self.host}:{self.port}/{self.database}'

    def missing(self):
        prefix = 'postgres_' if self.kind == 'postgres' else 'sqlserver_'
        names = {'host': self.host, 'user': self.user, 'password': self.password}
        if self.kind == 'sqlserver':
            names['db'] = self.database
        return [prefix + k for k, v in names.items() if not v]

    def connect(self, user=None, password=None, database=None):
        if self.kind == 'postgres':
            try:
                import psycopg2
            except ImportError:
                sys.exit('Needs psycopg2: python3 -m venv venv && venv/bin/pip install psycopg2-binary')
            conn = psycopg2.connect(host=self.host, port=self.port, dbname=database or self.database,
                                    user=user or self.user, password=password or self.password,
                                    sslmode=self.sslmode, connect_timeout=20)
        else:
            try:
                import pymssql
            except ImportError:
                sys.exit('Needs pymssql: python3 -m venv venv && venv/bin/pip install pymssql')
            conn = pymssql.connect(server=self.host, port=int(self.port), user=user or self.user,
                                   password=password or self.password, database=database or self.database,
                                   login_timeout=20)
        if self.kind == 'sqlserver':
            conn.autocommit(True)
        else:
            conn.autocommit = True
        return conn

    def query(self, sql, params=(), user=None, password=None, database=None):
        conn = self.connect(user, password, database)
        try:
            cur = conn.cursor()
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() if cur.description else []
        finally:
            conn.close()

    def schema_objects(self, schema):
        """None if the schema doesn't exist, else how many tables and views it holds."""
        if self.kind == 'postgres':
            rows = self.query("select count(c.oid) from pg_namespace n left join pg_class c on c.relnamespace = n.oid "
                              "and c.relkind in ('r', 'v', 'p') where n.nspname = %s group by n.oid", (schema,))
        else:
            if not self.query('select 1 from sys.databases where name = %s', (self.database,), database='master'):
                return None
            rows = self.query("select count(o.object_id) from sys.schemas s left join sys.objects o on "
                              "o.schema_id = s.schema_id and o.type in ('U', 'V') where s.name = %s group by s.schema_id",
                              (schema,))
        return rows[0][0] if rows else None

    def login_exists(self, login):
        if self.kind == 'postgres':
            return bool(self.query('select 1 from pg_roles where rolname = %s', (login,)))
        return bool(self.query('select 1 from sys.server_principals where name = %s', (login,), database='master'))

    def reader_sql(self, schema, login, password):
        """SQL that creates a login that can read the schema (and its view definitions) and nothing else."""
        pw = password.replace("'", "''")
        if self.kind == 'postgres':
            return [f"CREATE ROLE {login} LOGIN PASSWORD '{pw}' CONNECTION LIMIT 20",
                    f'GRANT CONNECT ON DATABASE {self.database} TO {login}',
                    f'GRANT USAGE ON SCHEMA {schema} TO {login}',
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {login}',
                    f'ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {login}']
        return [f"IF SUSER_ID(N'{login}') IS NULL CREATE LOGIN [{login}] WITH PASSWORD = N'{pw}', "
                f"DEFAULT_DATABASE = [{self.database}], CHECK_POLICY = OFF",
                f"IF USER_ID(N'{login}') IS NULL CREATE USER [{login}] FOR LOGIN [{login}]",
                f'GRANT SELECT ON SCHEMA::[{schema}] TO [{login}]',
                f'GRANT VIEW DEFINITION ON SCHEMA::[{schema}] TO [{login}]']

    def connection_props(self):
        props = {'host': self.host, 'port': str(self.port), 'database': self.database}
        if self.kind == 'postgres':
            props['ssl'] = 'false' if self.sslmode in ('disable', 'allow') else 'true'
            cert = setting('postgres_cert', 'postgres_ssl_certificate')
            path = setting('postgres_ssl_certificate_file')
            if not cert and path:
                cert = open(os.path.expanduser(path), encoding='utf-8').read()
            if cert:
                props['ssl_certificate'] = cert
        else:
            props['ssl'] = 'false'
        return props


def pg_tls(host, port, cadata=None):
    """Does a Postgres server's certificate verify against cadata, or this machine's trusted CAs? (ok, detail)"""
    try:
        with socket.create_connection((host, int(port)), timeout=15) as sock:
            sock.sendall((8).to_bytes(4, 'big') + (80877103).to_bytes(4, 'big'))  # SSLRequest
            if sock.recv(1) != b'S':
                return False, 'server does not offer SSL'
            try:
                c = ssl.create_default_context(cadata=cadata) if cadata else ssl.create_default_context()
                with c.wrap_socket(sock, server_hostname=host) as tls:
                    return True, f'certificate from {dict(x[0] for x in tls.getpeercert()["issuer"]).get("commonName")}'
            except ssl.SSLCertVerificationError as e:
                return False, e.verify_message
    except OSError as e:
        return False, f'cannot reach {host}:{port}: {e}'


# ---------- steps ----------

def cmd_check(api, a):
    db, schema, results = Db(a.db), a.schema, []

    def note(level, area, text):
        results.append(level)
        print(f'  {level:4}  {area:10} {text}')

    print(f'Checking {a.db} schema {schema} for project {a.project}; nothing is changed\n')
    note('ok' if shutil.which('curl') else 'FAIL', 'tools', 'curl ' + ('found' if shutil.which('curl') else 'missing'))
    if db.missing():
        note('FAIL', 'database', f'not set in .env: {", ".join(db.missing())}')
    else:
        try:
            n = db.schema_objects(schema)
            note('ok', 'database', f'{db.describe()} connects')
            if n is None:
                note('ok', 'database', f'schema {schema} does not exist yet: deploy creates it')
            elif n == 0:
                note('ok', 'database', f'schema {schema} exists and is empty: deploy fills it')
            else:
                note('ok', 'database', f'schema {schema} holds {n} tables and views (deploy is not needed)')
            reader = a.reader or f'{schema}_reader'
            note('ok', 'database', f'read-only login {reader} ' + ('exists' if db.login_exists(reader) else
                                                                     'does not exist yet: reader creates it'))
        except SystemExit:
            raise
        except Exception as e:  # driver errors differ by database
            note('FAIL', 'database', f'cannot connect as {db.describe()}: {str(e).strip()[:200]}')
        if a.db == 'postgres' and db.sslmode not in ('disable', 'allow'):
            verified, detail = pg_tls(db.host, db.port)
            cert = setting('postgres_cert', 'postgres_ssl_certificate', 'postgres_ssl_certificate_file')
            note('ok' if verified or cert else 'WARN', 'database',
                 f'TLS {detail}' + ('' if verified else ' (wxdi gets the CA certificate from .env)' if cert else
                                    ': wxdi will not trust it; set postgres_cert in .env to the provider\'s CA certificate'))
    note('ok', 'wxdi', f'API key works: {api.user}, account {api.account}, {api.url}')
    hit = [p for p in containers(api, 'project') if p['name'].lower() == a.project.lower()]
    if hit:
        n = len(schema_assets(api, 'project', hit[0]['id'], schema))
        note('ok', 'project', f'project {hit[0]["name"]} exists' + (f' and holds {n} {schema} assets' if n else ''))
        conns = [r['entity']['name'] for r in api.get(f'/v2/connections?project_id={hit[0]["id"]}&limit=200')
                 .get('resources', [])]
        note('ok', 'project', f'connections: {", ".join(conns) or "none yet"}')
    else:
        note('ok', 'project', f'project {a.project} does not exist yet: project creates it')
    pac = platform_catalog(api)
    dsd = find_datasource(api, pac, db.host, db.port, db.database)[0] if db.host else None
    note('ok', 'lineage', f'data source definition for {db.host}:{db.port}/{db.database}: '
         + (dsd if dsd else 'none yet (needed only for lineage; datasource creates it)'))
    fails, warns = results.count('FAIL'), results.count('WARN')
    print(f'\n{results.count("ok")} ok, {warns} warnings, {fails} failures')
    if fails:
        sys.exit(1)


def cmd_deploy(api, a):
    """Runs the SQL script as written; its own BEGIN/COMMIT make it all or nothing."""
    db = Db(a.db)
    if db.missing():
        sys.exit(f'not set in .env: {", ".join(db.missing())}')
    sql = open(os.path.expanduser(a.sql), encoding='utf-8').read()
    n = db.schema_objects(a.schema)
    if n:
        print(f'schema {a.schema} already holds {n} tables and views; nothing to deploy')
        return
    if a.dry_run:
        print(f'would run {a.sql} ({len(sql)} characters) as {db.describe()}')
        return
    if a.db == 'sqlserver' and n is None and not db.query('select 1 from sys.databases where name = %s',
                                                          (db.database,), database='master'):
        db.query(f'CREATE DATABASE [{db.database}]', database='master')
        print(f'created database {db.database}')
    conn = db.connect()
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()
    print(f'ran {os.path.basename(a.sql)} as {db.describe()}: schema {a.schema} now holds '
          f'{db.schema_objects(a.schema)} tables and views')


def cmd_reader(api, a):
    """Creates the read-only login if needed, then proves it can read the schema and cannot create objects."""
    db, login = Db(a.db), a.login or f'{a.schema}_reader'
    key = a.password_env or f'{a.schema}_reader_password'
    if db.login_exists(login):
        if not setting(key):
            sys.exit(f'login {login} exists but {key} is not in .env; add its password there (or pass --password-env)')
        print(f'login {login} exists')
    elif a.dry_run:
        print(f'would create login {login} (password generated into .env as {key})')
        return
    else:
        password = env_secret(key)
        for stmt in db.reader_sql(a.schema, login, password):
            db.query(stmt)
        print(f'created read-only login {login} (password in .env as {key})')
    if a.dry_run:
        return
    password = setting(key)
    if a.db == 'postgres':
        n = db.query("select count(*) from information_schema.tables where table_schema = %s", (a.schema,),
                     user=login, password=password)[0][0]
        probe = f'CREATE TABLE {a.schema}.mdi_write_test (i int)'
    else:
        n = db.query("select count(*) from information_schema.tables where table_schema = %s", (a.schema,),
                     user=login, password=password)[0][0]
        probe = f'CREATE TABLE [{a.schema}].mdi_write_test (i int)'
    try:
        db.query(probe, user=login, password=password)
    except Exception:
        print(f'  {login} sees {n} tables and views in {a.schema} and cannot create objects')
        return
    db.query(f'DROP TABLE {a.schema}.mdi_write_test')
    sys.exit(f'{login} could create a table: it is not read-only. Fix its grants before going on.')


def cmd_project(api, a):
    hit = [p for p in containers(api, 'project') if p['name'].lower() == a.project.lower()]
    if hit:
        print(f'project {hit[0]["name"]} exists ({hit[0]["id"]})')
        return hit[0]['id']
    guid, crn = account_storage(api)
    body = {'name': a.project, 'description': a.description or f'{a.project}: metadata import', 'public': False,
            'generator': 'wxdi-metadata-import',
            'storage': {'type': 'bmcos_object_storage', 'guid': guid, 'resource_crn': crn}}
    code, data = api.call('POST', '/transactional/v2/projects', body)
    print(f'create project {a.project} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    if not ok(code):
        sys.exit(1)
    return None if api.dry_run else data.get('location', '').rstrip('/').split('/')[-1]


def cmd_connection(api, a):
    """A platform connection (Platform assets catalog) through the read-only login, added to the project as a
    reference to it, the way the UI adds a platform connection to a project."""
    db = Db(a.db)
    pid, _ = find_container(api, 'project', a.project)
    name, login = a.connection, a.login or f'{a.schema}_reader'
    key = a.password_env or f'{a.schema}_reader_password'
    if any(r['entity']['name'] == name for r in api.get(f'/v2/connections?project_id={pid}&limit=200').get('resources', [])):
        print(f'connection {name} is in project {a.project}')
        return
    pac = platform_catalog(api)
    hit = [r for r in api.get(f'/v2/connections?catalog_id={pac}&limit=200').get('resources', [])
           if r['entity']['name'] == name]
    if hit:
        conn_id = hit[0]['metadata']['asset_id']
        print(f'platform connection {name} exists ({conn_id})')
    else:
        if not setting(key):
            sys.exit(f'{key} is not in .env: run the reader step first, or pass --password-env')
        props = dict(db.connection_props(), username=login, password=setting(key))
        body = {'name': name, 'description': f'{login} (read-only), {db.host}:{db.port}/{db.database}',
                'datasource_type': datasource_type_id(api, db.type_name), 'origin_country': 'us', 'properties': props}
        code, data = api.call('POST', f'/v2/connections?catalog_id={pac}&test=true', body)
        print(f'create platform connection {name} -> {code}', '' if ok(code) else json.dumps(data)[:400])
        if not ok(code) or api.dry_run:
            if not ok(code):
                sys.exit(1)
            print(f'would add it to project {a.project}')
            return
        conn_id = data['metadata']['asset_id']
    code, data = api.call('POST', f'/v2/connections?project_id={pid}&test=true', {'ref_catalog_id': pac, 'ref_asset_id': conn_id})
    print(f'add {name} to project {a.project} -> {code}', '' if ok(code) else json.dumps(data)[:300])


def cmd_datasource(api, a):
    """The data source definition for the database: lineage imports need one. Create it before changing a
    connection's host or port: a connection links to the definition whose endpoint matches it."""
    db, pac = Db(a.db), platform_catalog(api)
    dsd, name = find_datasource(api, pac, db.host, db.port, db.database)
    if dsd:
        print(f'data source definition {name} exists for {db.host}:{db.port}/{db.database} ({dsd})')
        return dsd
    name = a.name or f'{a.schema}_{a.db}'
    body = {'metadata': {'name': name, 'asset_type': 'ibm_data_source', 'origin_country': 'us',
                         'description': f'{db.technology} {db.host}:{db.port}/{db.database}'},
            'entity': {'ibm_data_source': {
                'data_source_type_id': datasource_type_id(api, db.type_name), 'data_source_state': 'active',
                'data_source_endpoints': {'values': [{'host': db.host, 'port': int(db.port),
                                                      'physical_collection': db.database}]}}}}
    code, data = api.call('POST', f'/v2/assets?catalog_id={pac}', body)
    print(f'create data source definition {name} -> {code}', '' if ok(code) else json.dumps(data)[:300])
    if not ok(code):
        sys.exit('Create it in the UI instead (Administration > Data source definitions), then run this again')
    return None if api.dry_run else data['metadata']['asset_id']


def lineage_body(db, a, dsd):
    t = LINEAGE[a.db]
    lineage_db = db.database
    return {
        'import_type': 'lineage_and_discovery', 'scanner': t['technology'], 'data_source_asset_id': dsd,
        # snake case: the API silently drops dataSourceAssetId (the name in the MCP server's model)
        'datasource_definition_info': {'data_source_asset_id': dsd, 'technology': t['technology'],
                                       'scanner': t['technology']},
        'scope_lineage': {'paths': [f'/{lineage_db}/{a.schema}']},
        'advanced_lineage_option': {'input_reader': t['input_reader'], 'enable_extended_attributes': True,
                                    'enable_transformation_logic': True},
        'lineage_scanning_phases': SCAN_PHASES, 'extraction_info': {'connection_type': 'direct'},
    }


def cmd_import(api, a):
    db = Db(a.db)
    pid, _ = find_container(api, 'project', a.project)
    conn_id, _ = find_connection(api, 'project', pid, a.connection)
    name = a.name or f'{a.schema}_mdi' + ('l' if a.lineage else '')
    code, data = api.call('POST', f'/v2/asset_types/metadata_import/search?project_id={pid}',
                          {'query': f'asset.name:"{name}"'}, write=False)
    hit = [r for r in data.get('results', []) if r['metadata']['name'] == name]
    if hit:
        mdi = hit[0]['metadata']['asset_id']
        entity = api.get(f'/v2/metadata_imports/{mdi}?project_id={pid}')['entity']
        kind = entity.get('import_type')
        print(f'metadata import {name} exists ({mdi}, {kind})')
        if a.lineage and kind == 'metadata':
            sys.exit(f'{name} imports metadata only; give the lineage import another --name')
    else:
        body = {'name': name, 'description': f'{a.schema} on {db.technology} {db.host}:{db.port}/{db.database}',
                'import_type': 'metadata', 'connection_id': conn_id, 'target_project_id': pid, 'unified_lineage': True,
                'scope': {'paths': [f'/{a.schema}']},
                'import_options': {'include_primary_key': True, 'include_foreign_key': True},
                'reimport_options': {'update_name': True, 'update_description': True, 'update_column_descriptions': True,
                                     'delete_when_deleted_at_source': False, 'delete_when_removed_from_scope': False}}
        if a.lineage:
            dsd = find_datasource(api, platform_catalog(api), db.host, db.port, db.database)[0]
            if not dsd:
                sys.exit('Lineage needs a data source definition for this database: run the datasource step first')
            body.update(lineage_body(db, a, dsd))
        code, data = api.call('POST', f'/v2/metadata_imports?project_id={pid}&job_name={name}_job&create_job=true', body)
        print(f'create metadata import {name} ({"with" if a.lineage else "without"} lineage) -> {code}',
              '' if ok(code) else json.dumps(data)[:400])
        if not ok(code):
            if 'already exists' in json.dumps(data):
                print('  wxdi allows one lineage import per data source definition and scanner; '
                      'use or delete the existing one')
            sys.exit(1)
        if api.dry_run:
            return None
        mdi = data['metadata']['asset_id']
    job = api.get(f'/v2/metadata_imports/{mdi}?project_id={pid}')['entity'].get('job_id')
    runs = api.get(f'/v2/jobs/{job}/runs?project_id={pid}&limit=5').get('results') or []
    running = [r for r in runs if r['entity']['job_run']['state'] in ACTIVE]
    if a.dry_run:
        print('would run it' + (f' (a run is already {running[0]["entity"]["job_run"]["state"]})' if running else ''))
        return None
    if running:
        run = running[0]['metadata']['asset_id']
        print(f'run {run} already in progress; waiting for it')
    else:
        code, data = api.call('POST', f'/v2/jobs/{job}/runs?project_id={pid}', {'job_run': {}})
        if not ok(code):
            sys.exit(f'cannot start the run ({code}): {json.dumps(data)[:300]}')
        run = data['metadata']['asset_id']
        print(f'run {run} started')
    state, misses, lineage = 'Running', 0, {}
    for _ in range(1080):  # up to three hours: lineage scans take far longer than discovery
        code, data = api.call('GET', f'/v2/jobs/{job}/runs/{run}?project_id={pid}')
        if code < 300:
            jr = data['entity']['job_run']
            state, misses = jr['state'], 0
            lineage = jr.get('configuration', {}).get('lineage', {}).get('entity', {})
            # a failed lineage part can leave the job Running for good; report it instead of waiting
            if state not in ACTIVE or lineage.get('status') == 'failed':
                break
        else:
            misses += 1
            if misses >= 30:
                sys.exit(f'run {run}: status unavailable ({code})')
        time.sleep(10)
    print(f'run {run}: {state}' + (f'; lineage {lineage.get("status")}' if lineage else ''))
    for e in lineage.get('error_details') or []:
        print(f'  lineage error: {e.get("message")}')
    if lineage.get('status') == 'failed' and state in ACTIVE:
        print('  the job stays "Running" after its lineage part fails. To clear it: PATCH '
              f'/v2/jobs/{job}/runs/{run}?project_id={pid} with [{{"op": "replace", "path": "/entity/job_run/state", '
              '"value": "Failed"}] (Content-Type application/json)')
    assets = schema_assets(api, 'project', pid, a.schema)
    print(f'project {a.project}: {len(assets)} {a.schema} assets')
    return state


def cmd_lineage_check(api, a):
    """For each view: does lineage show the tables feeding it?"""
    db = Db(a.db)
    dsd = find_datasource(api, platform_catalog(api), db.host, db.port, db.database)[0]
    views = ([v['name'] for v in json.load(open(a.metadata, encoding='utf-8')).get('views', [])]
             if a.metadata else a.view)
    good = 0
    for v in views:
        body = {'query': v, 'filters': [{'type': 'data_source_definition_asset_id', 'values': [dsd]}] if dsd else []}
        code, d = api.call('POST', '/gov_lineage/v2/search_lineage_assets', body, write=False)
        hits = [h for h in d.get('lineage_assets', []) if h['name'] == v]
        if not hits:
            print(f'  view {v}: not in lineage')
            continue
        vid = hits[0]['id']
        kids = api.get(f'/gov_lineage/v2/lineage_assets/{vid}/children?limit=200')
        ids = [vid] + [k['id'] for k in kids.get('children') or kids.get('lineage_assets') or []]
        code, g = api.call('POST', '/gov_lineage/v2/query_lineage', {
            'initial_asset_ids': ids, 'visible_asset_ids': ids, 'allow_lineage_cache': 'false',
            'expansion': {'starting_asset_ids': ids, 'incoming_steps': '5', 'outgoing_steps': '0'}}, write=False)
        sources = sorted({(x['hierarchical_path'][0]['name'] if x.get('hierarchical_path') else x['name'])
                          for x in g.get('assets_in_view', [])} - {v})
        good += bool(sources)
        print(f'  view {v}: {len(g.get("edges_in_view", []))} edges, from {", ".join(sources) or "nothing"}')
    print(f'{good} of {len(views)} views have lineage from their tables')
    return good, len(views)


def cmd_status(api, a):
    pid, pname = find_container(api, 'project', a.project)
    assets = schema_assets(api, 'project', pid, a.schema)
    print(f'project {pname} ({pid}): {len(assets)} {a.schema} assets')
    for name, aid in sorted(assets.items()):
        print(f'  {a.schema}.{name}  {aid}')
    for r in api.get(f'/v2/connections?project_id={pid}&limit=200').get('resources', []):
        print(f'  connection {r["entity"]["name"]}  login {r["entity"].get("properties", {}).get("username")}')
    code, data = api.call('POST', f'/v2/asset_types/metadata_import/search?project_id={pid}', {'query': '*:*'}, write=False)
    for r in data.get('results', []):
        e = api.get(f'/v2/metadata_imports/{r["metadata"]["asset_id"]}?project_id={pid}')['entity']
        print(f'  metadata import {r["metadata"]["name"]} ({e.get("import_type")}, scope {e.get("scope", {}).get("paths")})')


def cmd_run_all(api, a):
    meta = a.metadata or os.path.join(work_dir(a.schema), f'{a.schema}.metadata.json')
    sql = a.sql or os.path.join(work_dir(a.schema), f'{a.schema}.{a.db}.sql')
    steps = [('check', cmd_check, {})]
    if a.deploy:
        steps.append(('deploy', cmd_deploy, {'sql': sql}))
    steps += [('reader', cmd_reader, {}), ('project', cmd_project, {'description': None}),
              ('connection', cmd_connection, {})]
    if a.lineage:
        steps.append(('datasource', cmd_datasource, {'name': None}))
    steps.append(('import', cmd_import, {}))
    if a.lineage and os.path.exists(meta):
        steps.append(('lineage-check', cmd_lineage_check, {'metadata': meta, 'view': None}))
    for name, fn, extra in steps:
        print(f'== {name}')
        if name == 'reader' and not a.deploy and not Db(a.db).schema_objects(a.schema):
            sys.exit(f'schema {a.schema} has no tables or views: pass --deploy to create them from {sql}')
        fn(api, argparse_ns(a, **extra))


def argparse_ns(a, **kw):
    d = dict(vars(a))
    d.update(kw)
    return argparse.Namespace(**d)


def main():
    ap = common_args(__doc__)
    sub = ap.add_subparsers(dest='step', required=True)

    def p(name, help, db=True, schema=True, project=False):
        s = sub.add_parser(name, help=help)
        if db:
            s.add_argument('--db', choices=['postgres', 'sqlserver'], required=True, help='which database in .env')
        if schema:
            s.add_argument('--schema', required=True)
        if project:
            s.add_argument('--project', required=True, help='project name or id')
        return s

    def reader_args(s):
        s.add_argument('--login', help='read-only login (default: <schema>_reader)')
        s.add_argument('--password-env', help='.env key with its password (default: <schema>_reader_password)')

    s = p('check', 'read-only readiness check', project=True)
    s.add_argument('--reader', help='read-only login to look for (default: <schema>_reader)')
    p('deploy', 'create the tables and views from a SQL script').add_argument('--sql', required=True)
    reader_args(p('reader', 'create and test the read-only login'))
    s = p('project', 'create the project', db=False, schema=False, project=True)
    s.add_argument('--description')
    s = p('connection', 'platform connection through the read-only login, added to the project', project=True)
    s.add_argument('--connection', required=True, help='connection name, e.g. emovies_reader')
    reader_args(s)
    p('datasource', 'data source definition (for lineage)').add_argument('--name')
    s = p('import', 'create and run the metadata import', project=True)
    s.add_argument('--connection', required=True)
    s.add_argument('--name', help='metadata import name (default: <schema>_mdi, or <schema>_mdil with --lineage)')
    s.add_argument('--lineage', action='store_true', help='lineage and discovery, through the data source definition')
    s = p('lineage-check', 'which views have lineage from their tables')
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--metadata', help="erwin-model-export's <schema>.metadata.json, for the view names")
    g.add_argument('--view', action='append', help='a view name (repeatable)')
    p('status', 'what the project holds', db=False, project=True)
    s = p('run-all', 'all steps in order', project=True)
    s.add_argument('--connection', help='connection name (default: <schema>_reader)')
    s.add_argument('--lineage', action='store_true')
    s.add_argument('--deploy', action='store_true', help='create the tables and views first, from --sql')
    s.add_argument('--sql', help='default: work/<schema>/<schema>.<db>.sql')
    s.add_argument('--metadata', help='default: work/<schema>/<schema>.metadata.json')
    s.add_argument('--name', help='metadata import name')
    reader_args(s)
    a = ap.parse_args()
    if a.step == 'run-all':
        a.connection = a.connection or f'{a.schema}_reader'
        a.reader = a.login
    api = connect(a)
    {'check': cmd_check, 'deploy': cmd_deploy, 'reader': cmd_reader, 'project': cmd_project,
     'connection': cmd_connection, 'datasource': cmd_datasource, 'import': cmd_import,
     'lineage-check': cmd_lineage_check, 'status': cmd_status, 'run-all': cmd_run_all}[a.step](api, a)


if __name__ == '__main__':
    main()
