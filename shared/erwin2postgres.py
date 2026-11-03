#!/usr/bin/env python3
"""Export a PostgreSQL DDL script (tables, keys, comments and views) from an erwin .erwin model.

Reads the binary erwin Data Modeler file directly, so no erwin installation is needed.
The script it writes creates, inside one transaction:
  - a schema
  - one table per entity, with primary keys, alternate (unique) keys and foreign keys
  - COMMENT ON TABLE / COLUMN from the erwin definitions
  - one view per erwin view, built from the view's source tables and column mappings

erwin stores which tables and columns a view uses, but not its join conditions, so joins are
inferred from the model's migrated keys (the foreign key columns linking the view's tables).
Anything that cannot be inferred is flagged with a REVIEW comment in the script.

With --glossary DIR it also writes the model's governance metadata for a data catalog:
  glossary.csv            business terms (logical entity/attribute names + definitions) and categories
                          (one per subject area), in the IBM watsonx.data intelligence CSV import format
  term_assignments.csv    which term belongs on which table, view and column
  reference_data/*.csv    valid-value lists (code, value), plus reference_data_usage.csv
  custom_properties.csv   user-defined property values, e.g. Data Steward

Usage: erwin2postgres.py MODEL.erwin [--schema NAME] [--out FILE] [--metadata DIR] [--glossary DIR [--category NAME]]
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import erwinbin  # noqa: E402

C_ENTITY, C_ATTRIBUTE, C_DOMAIN = 0x40200003, 0x40200005, 0x40200007
C_KEY_GROUP, C_KEY_MEMBER, C_RELATIONSHIP = 0x40200009, 0x4020000A, 0x40200028

P_DEFINITION, P_NAME, P_LONG_ID = 0x4000012D, 0x4000012E, 0x40000130
P_TYPE = 0x40202712                 # attribute: 0 = primary key, 100 = non-key
P_PHYSICAL_NAME = 0x402027D8
P_PHYSICAL_DATATYPE = 0x40202760
P_LOGICAL_DATATYPE = 0x402027F1
P_PARENT_DOMAIN = 0x402028EC
P_DOMAIN_NAME_MACRO = 0x40202BD7    # domain: name given to attributes that use it, e.g. %Lower(%EntityName %AttDomain)
P_PARENT_ATTRIBUTE = 0x402027B0     # FK attribute -> attribute it migrated from
P_PARENT_RELATIONSHIP = 0x402027B1
P_COLUMN_ORDER = (0x40202C80, 0x40202BD5, 0x40202BD6)
P_KEY_TYPE = 0x4020272C             # PK, AK1, IE1, IF1 ...
P_KEY_RELATIONSHIP = 0x40202735
P_MEMBER_ATTRIBUTE = 0x40202739
P_MEMBER_ORDER = 0x40202FCA
P_REL_PARENT, P_REL_CHILD = 0x40202A23, 0x40202A24
P_REL_PHRASE, P_REL_INVERSE_PHRASE = 0x402029E9, 0x402027C6

PG_TYPES = [
    (r'^LARGE BINARY$|^BLOB$|^IMAGE$|^VARBINARY', 'bytea'),
    (r'^DATETIME', 'timestamp'),
    (r'^(N?VARCHAR2?|NCHAR VARYING)\b', 'varchar'),
    (r'^(NUMBER|DECIMAL)\b', 'numeric'),
    (r'^MONEY$', 'numeric(19,4)'),
    (r'^(TINYINT|BYTE)$', 'smallint'),
    (r'^(LONG VARCHAR|CLOB|NTEXT|TEXT|MEMO)$', 'text'),
    (r'^(BIT|BOOL(EAN)?)$', 'boolean'),
    (r'^(FLOAT|DOUBLE)$', 'double precision'),
    (r'^STRING$', 'text'),
]
PG_MAX_IDENT = 63

# SQL Server (--dialect sqlserver). A bare varchar is varchar(1) there and a bare numeric has no decimals,
# so both get an explicit size instead.
MSSQL_TYPES = [
    (r'^(LARGE BINARY|BLOB|IMAGE|BYTEA)$', 'varbinary(max)'),
    (r'^(DATETIME|TIMESTAMP)$', 'datetime2'),
    (r'^(N?VARCHAR2?|NCHAR VARYING|CHARACTER VARYING)$', 'varchar(255)'),
    (r'^(N?VARCHAR2?|NCHAR VARYING|CHARACTER VARYING)\b', 'varchar'),
    (r'^(NUMBER|NUMERIC|DECIMAL)$', 'decimal(19,4)'),
    (r'^NUMBER\b', 'decimal'),
    (r'^(LONG VARCHAR|CLOB|TEXT|MEMO|STRING)$', 'varchar(max)'),
    (r'^NTEXT$', 'nvarchar(max)'),
    (r'^BOOL(EAN)?$', 'bit'),
    (r'^(DOUBLE|DOUBLE PRECISION)$', 'float'),
]
MSSQL_MAX_IDENT = 128


def expand(macro, values):
    """Expand the subset of erwin naming macros used for inherited attribute names."""
    def fn(m):
        inner = expand(m.group(2), values)
        return inner.lower() if m.group(1) == 'Lower' else inner.upper()
    while re.search(r'%(Lower|Upper)\(([^()]*)\)', macro):
        macro = re.sub(r'%(Lower|Upper)\(([^()]*)\)', fn, macro)
    macro = re.sub(r'%(\w+)(\(\))?', lambda m: values.get(m.group(1), ''), macro)
    return re.sub(r'\s+', ' ', macro).strip()


def snake(name):
    return re.sub(r'[^0-9A-Za-z]+', '_', name).strip('_').lower() or 'unnamed'


def unique(name, taken):
    """name, or name_2, name_3 ... trimmed to fit Postgres' identifier limit."""
    n, out = 1, name
    while out in taken:
        n += 1
        out = name[:PG_MAX_IDENT - len(str(n)) - 1] + '_' + str(n)
    return out


def pg_type(dt):
    if not dt:
        return 'varchar(255)'
    dt = re.sub(r'\(\s*\)', '', dt.strip())  # erwin allows DECIMAL() with no precision
    for pat, repl in PG_TYPES:
        if re.match(pat, dt, re.I):
            dt = re.sub(pat, repl, dt, flags=re.I)
            break
    dt = dt.lower()
    m = re.match(r'^(varchar|char|character)\((\d+)\)$', dt)
    if m and int(m.group(2)) > 10485760:  # Postgres' maximum declared length
        return 'text'
    return dt


def mssql_type(dt):
    if not dt:
        return 'varchar(255)'
    dt = re.sub(r'\(\s*\)', '', dt.strip())
    for pat, repl in MSSQL_TYPES:
        if re.match(pat, dt, re.I):
            dt = re.sub(pat, repl, dt, flags=re.I)
            break
    dt = dt.lower()
    m = re.match(r'^(n?varchar|n?char|varbinary)\((\d+)\)$', dt)
    if m and int(m.group(2)) > 8000:  # SQL Server's largest declared length
        return m.group(1) + '(max)'
    return dt


def qi(ident):
    return '"' + ident.replace('"', '""') + '"'


def ql(text):
    return "'" + text.replace("'", "''") + "'"


class Model:
    def __init__(self, path):
        self.objs, self.failed = erwinbin.read(path)
        for o in self.objs.values():
            # erwin escapes commas in definitions as '\,'
            if isinstance(o.props.get(P_DEFINITION), str):
                o.props[P_DEFINITION] = o.props[P_DEFINITION].replace('\\,', ',')
        root = self.objs.get(1)
        self.name = (root.props.get(P_NAME) if root else None) or os.path.splitext(os.path.basename(path))[0]

    def of(self, cls):
        return sorted((o for o in self.objs.values() if o.cls == cls), key=lambda o: o.id)

    def domain_attr(self, dom_id, prop, depth=0):
        dom = self.objs.get(dom_id)
        if dom is None or depth > 20:
            return None
        return dom.props.get(prop) or self.domain_attr(dom.props.get(P_PARENT_DOMAIN), prop, depth + 1)

    def domain_datatype(self, dom_id, depth=0):
        """Nearest domain in the chain that sets a datatype (physical before logical at each level)."""
        dom = self.objs.get(dom_id)
        if dom is None or depth > 20:
            return None
        return (dom.props.get(P_PHYSICAL_DATATYPE) or dom.props.get(P_LOGICAL_DATATYPE)
                or self.domain_datatype(dom.props.get(P_PARENT_DOMAIN), depth + 1))

    def migrated_from(self, a):
        return self.objs.get(a.props.get(P_PARENT_ATTRIBUTE))

    def attr_name(self, a, depth=0):
        if a.props.get(P_NAME):
            return a.props[P_NAME]
        parent = self.migrated_from(a)
        if parent is not None and depth < 20:
            return self.attr_name(parent, depth + 1)
        dom_id = a.props.get(P_PARENT_DOMAIN)
        dom = self.objs.get(dom_id)
        if dom is None:
            return f'attribute_{a.id}'
        macro = self.domain_attr(dom_id, P_DOMAIN_NAME_MACRO) or '%AttDomain'
        owner = self.objs.get(a.owner)
        return expand(macro, {'AttDomain': dom.props.get(P_NAME, ''), 'DomainName': dom.props.get(P_NAME, ''),
                              'EntityName': owner.props.get(P_NAME, '') if owner else '',
                              'OwnerEntity': owner.props.get(P_NAME, '') if owner else ''})

    def attr_definition(self, a, depth=0):
        if a.props.get(P_DEFINITION):
            return a.props[P_DEFINITION]
        parent = self.migrated_from(a)
        if parent is not None and depth < 20:
            return self.attr_definition(parent, depth + 1)
        return self.domain_attr(a.props.get(P_PARENT_DOMAIN), P_DEFINITION) or ''

    def attr_datatype(self, a, depth=0):
        dt = a.props.get(P_PHYSICAL_DATATYPE) or a.props.get(P_LOGICAL_DATATYPE)
        if dt:
            return dt
        parent = self.migrated_from(a)
        if parent is not None and depth < 20:
            dt = self.attr_datatype(parent, depth + 1)
            if dt:
                return dt
        return self.domain_datatype(a.props.get(P_PARENT_DOMAIN))

    def build(self):
        entities, rels, tables = [], [], set()
        col_by_attr = {}
        for e in self.of(C_ENTITY):
            ename = e.props.get(P_NAME) or f'entity_{e.id}'
            table = unique(snake(e.props.get(P_PHYSICAL_NAME) or ename)[:PG_MAX_IDENT], tables)
            tables.add(table)
            attrs = {c.id: c for c in e.children if c.cls == C_ATTRIBUTE}
            order = next((e.props[p] for p in P_COLUMN_ORDER if isinstance(e.props.get(p), list)), [])
            ordered = [attrs[i] for i in order if i in attrs] + [a for i, a in attrs.items() if i not in order]
            keys = []
            for kg in (c for c in e.children if c.cls == C_KEY_GROUP):
                members = sorted((m for m in kg.children if m.cls == C_KEY_MEMBER),
                                 key=lambda m: m.props.get(P_MEMBER_ORDER, 0))
                keys.append({'type': kg.props.get(P_KEY_TYPE, ''),
                             'relationship_id': kg.props.get(P_KEY_RELATIONSHIP),
                             'attribute_ids': [m.props.get(P_MEMBER_ATTRIBUTE) for m in members]})
            pk_ids = next((k['attribute_ids'] for k in keys if k['type'] == 'PK'), [])
            cols, by_col = [], {}
            for a in ordered:
                aname = self.attr_name(a)
                col = snake(a.props.get(P_PHYSICAL_NAME) or aname)[:PG_MAX_IDENT]
                if col in by_col and by_col[col]['attribute'] != aname:
                    col = unique(col, by_col)
                if col in by_col:
                    # same-named migrated keys unify into one column, as erwin does by default
                    c = by_col[col]
                    c['primary_key'] |= a.id in pk_ids
                    c['foreign_key'] |= P_PARENT_ATTRIBUTE in a.props
                    col_by_attr[a.id] = (table, col)
                    continue
                c = {'id': a.id, 'attribute': aname, 'column': col,
                     'datatype': self.attr_datatype(a) or '',
                     'primary_key': a.id in pk_ids or a.props.get(P_TYPE) == 0,
                     'foreign_key': P_PARENT_ATTRIBUTE in a.props,
                     'definition': self.attr_definition(a)}
                c['pg_type'] = pg_type(c['datatype'])
                cols.append(c)
                by_col[col] = c
                col_by_attr[a.id] = (table, col)
            for k in keys:
                k['columns'] = list(dict.fromkeys(col_by_attr[i][1] for i in k['attribute_ids'] if i in col_by_attr))
            entities.append({'id': e.id, 'entity': ename, 'table': table,
                             'definition': e.props.get(P_DEFINITION, ''),
                             'erwin_long_id': e.props.get(P_LONG_ID, ''),
                             'columns': cols, 'keys': keys})
        by_id = {e['id']: e for e in entities}
        for r in self.of(C_RELATIONSHIP):
            parent, child = by_id.get(r.props.get(P_REL_PARENT)), by_id.get(r.props.get(P_REL_CHILD))
            if not parent or not child:
                continue
            fk = [a for a in self.objs.values()
                  if a.cls == C_ATTRIBUTE and a.owner == child['id'] and a.props.get(P_PARENT_RELATIONSHIP) == r.id]
            pairs = []
            for a in fk:
                src = self.objs.get(a.props.get(P_PARENT_ATTRIBUTE))
                # walk back to the attribute that actually lives in the parent entity
                while src is not None and src.owner != parent['id'] and P_PARENT_ATTRIBUTE in src.props:
                    src = self.objs.get(src.props[P_PARENT_ATTRIBUTE])
                if src is not None and a.id in col_by_attr and src.id in col_by_attr:
                    pairs.append((col_by_attr[a.id][1], col_by_attr[src.id][1]))
            rels.append({'id': r.id, 'name': r.props.get(P_NAME, ''),
                         'parent': parent['table'], 'child': child['table'],
                         'phrase': r.props.get(P_REL_PHRASE, ''),
                         'inverse_phrase': r.props.get(P_REL_INVERSE_PHRASE, ''),
                         'definition': r.props.get(P_DEFINITION, ''),
                         'child_columns': [p[0] for p in pairs], 'parent_columns': [p[1] for p in pairs]})
        domains = [{'id': d.id, 'name': d.props.get(P_NAME, ''),
                    'parent_id': d.props.get(P_PARENT_DOMAIN),
                    'datatype': self.domain_attr(d.id, P_PHYSICAL_DATATYPE) or self.domain_attr(d.id, P_LOGICAL_DATATYPE)}
                   for d in self.of(C_DOMAIN)]
        self.col_by_attr = col_by_attr
        return {'model': self.name, 'entities': entities, 'relationships': rels, 'domains': domains,
                'views': build_views(self, entities, col_by_attr)}



# erwin views
C_VIEW = 0x40200053
P_VIEW_SOURCES = 0x40202BD0        # view: its view relationships, one per source table
P_VIEW_SOURCE_ORDER = 0x402027C9
P_VIEW_EXPRESSION = 0x4020278E     # view column: derived expression, e.g. rental_rate * 1.5
P_VIEW_SQL = 0x402027BB            # view: user-defined SQL, written against erwin's own names
PLACEHOLDER_EXPR = re.compile(r'^Expr_\d+$')
SQL_WORDS = {'and', 'or', 'not', 'null', 'is', 'case', 'when', 'then', 'else', 'end', 'as', 'in',
             'like', 'between', 'true', 'false', 'cast', 'coalesce', 'nullif', 'distinct'}


def build_views(model, entities, col_by_attr):
    """Resolve each erwin view into source tables, joins and output columns."""
    objs = model.objs
    by_id = {e['id']: e for e in entities}
    attrs_of = {}
    for o in objs.values():
        if o.cls == C_ATTRIBUTE and o.owner in by_id:
            attrs_of.setdefault(o.owner, []).append(o)

    def ancestors(a):
        seen, cur = set(), objs.get(a.props.get(P_PARENT_ATTRIBUTE))
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            yield cur
            cur = objs.get(cur.props.get(P_PARENT_ATTRIBUTE))

    def link(a_ent, b_ent):
        """Column pairs where a key migrated from one entity into the other (either direction)."""
        pairs = []
        for child, parent, flip in ((a_ent, b_ent, False), (b_ent, a_ent, True)):
            for a in attrs_of.get(child, []):
                for anc in ancestors(a):
                    if anc.owner == parent and a.id in col_by_attr and anc.id in col_by_attr:
                        p = (col_by_attr[a.id][1], col_by_attr[anc.id][1])
                        pairs.append(p[::-1] if flip else p)
                        break
        return list(dict.fromkeys(pairs))

    views, names = [], {e['table'] for e in entities}
    for v in model.of(C_VIEW):
        vname = unique(snake(v.props.get(P_PHYSICAL_NAME) or v.props.get(P_NAME) or f'view_{v.id}')[:PG_MAX_IDENT], names)
        names.add(vname)
        notes = []
        rels = [objs[i] for i in v.props.get(P_VIEW_SOURCES, []) if i in objs]
        rels.sort(key=lambda r: r.props.get(P_VIEW_SOURCE_ORDER, 0))
        sources, aliases = [], set()
        for r in rels:
            ent = by_id.get(r.props.get(P_REL_PARENT))
            if ent is None or any(s['entity_id'] == ent['id'] for s in sources):
                continue
            alias = ''.join(w[0] for w in ent['table'].split('_') if w) or 't'
            if alias in aliases:
                alias = unique(ent['table'], aliases)
            aliases.add(alias)
            sources.append({'entity_id': ent['id'], 'table': ent['table'], 'alias': alias})

        # join each source to one already in the FROM clause through migrated key columns
        joined, joins, pending = sources[:1], [], sources[1:]
        while pending:
            progress = False
            for s in list(pending):
                for j in joined:
                    pairs = link(j['entity_id'], s['entity_id'])
                    if pairs:
                        joins.append({'source': s, 'on': [(j['alias'], jc, s['alias'], sc) for jc, sc in pairs]})
                        joined.append(s)
                        pending.remove(s)
                        progress = True
                        break
            if not progress:
                s = pending.pop(0)
                joins.append({'source': s, 'on': []})
                joined.append(s)
                notes.append(f'REVIEW: no key in the model links {s["table"]} to the other tables, so it is CROSS JOINed')

        alias_of = {s['entity_id']: s['alias'] for s in sources}
        src_cols = {}
        for s in sources:
            for c in by_id[s['entity_id']]['columns']:
                src_cols.setdefault(c['column'], []).append(s['alias'])

        mapped = {(alias_of[objs[c.props[P_PARENT_ATTRIBUTE]].owner], col_by_attr[c.props[P_PARENT_ATTRIBUTE]][1])
                  for c in v.children
                  if c.cls == C_ATTRIBUTE and c.props.get(P_PARENT_ATTRIBUTE) in col_by_attr
                  and objs[c.props[P_PARENT_ATTRIBUTE]].owner in alias_of}

        def qualify(expr):
            def fn(m):
                word = m.group(0)
                col = word.lower()
                hits = src_cols.get(col, [])
                if col in SQL_WORDS or not hits:
                    return word
                if len(hits) > 1:
                    # several source tables have this column: prefer the one the view already selects it from
                    used = [a for a in hits if (a, col) in mapped]
                    note = f'REVIEW: "{col}" in expression "{expr.strip()}" exists in {", ".join(hits)}; used {(used or hits)[0]}'
                    if not used and note not in notes:
                        notes.append(note)
                    hits = used or hits
                return f'{hits[0]}.{qi(col)}'
            # leave quoted strings alone
            parts = re.split(r"('(?:[^']|'')*')", expr)
            return ''.join(p if i % 2 else re.sub(r'(?<![\w.])[A-Za-z_]\w*(?![\w(])', fn, p) for i, p in enumerate(parts))

        cols, taken = [], set()
        children = {c.id: c for c in v.children if c.cls == C_ATTRIBUTE}
        order = next((v.props[p] for p in P_COLUMN_ORDER if isinstance(v.props.get(p), list)), [])
        ordered = [children[i] for i in order if i in children] + [c for i, c in children.items() if i not in order]
        for c in ordered:
            own = c.props.get(P_PHYSICAL_NAME) or c.props.get(P_NAME)
            src = objs.get(c.props.get(P_PARENT_ATTRIBUTE))
            expr = c.props.get(P_VIEW_EXPRESSION)
            definition = c.props.get(P_DEFINITION, '')
            if src is not None and src.id in col_by_attr and src.owner in alias_of:
                table, col = col_by_attr[src.id]
                sql = f'{alias_of[src.owner]}.{qi(col)}'
                name = snake(own) if own else col
                if not definition:
                    definition = next((x['definition'] for x in by_id[src.owner]['columns'] if x['column'] == col), '')
            elif expr and not PLACEHOLDER_EXPR.match(expr.strip()):
                sql = qualify(expr.strip())
                name = snake(own or f'expr_{c.id}')
                definition = definition or f'Derived: {expr.strip()}'
            else:
                continue
            name = unique(name[:PG_MAX_IDENT], taken)
            taken.add(name)
            cols.append({'column': name, 'sql': sql, 'definition': definition})

        if not sources or not cols:
            notes.append('skipped: the model gives this view no source tables or resolvable columns')
        views.append({'id': v.id, 'view': v.props.get(P_NAME, vname), 'name': vname,
                      'definition': v.props.get(P_DEFINITION, ''), 'sources': joined, 'joins': joins,
                      'columns': cols, 'user_sql': v.props.get(P_VIEW_SQL, ''), 'notes': notes})
    return views


def ddl(m, schema, dialect='postgres'):
    if dialect == 'sqlserver':
        return ddl_sqlserver(m, schema)
    s = qi(schema)
    out = [f'-- Generated from erwin model "{m["model"]}" by erwin2postgres.py',
           '-- Runs in one transaction: if any statement fails, nothing is created.',
           'BEGIN;', '', f'CREATE SCHEMA IF NOT EXISTS {s};', '']
    for e in m['entities']:
        t = f'{s}.{qi(e["table"])}'
        lines = [f'    {qi(c["column"])} {c["pg_type"]}{" NOT NULL" if c["primary_key"] else ""}' for c in e['columns']]
        pk = [c['column'] for c in e['columns'] if c['primary_key']]
        if pk:
            lines.append(f'    PRIMARY KEY ({", ".join(map(qi, pk))})')
        for k in e['keys']:
            if k['type'].startswith('AK') and k['columns'] and sorted(k['columns']) != sorted(pk):
                lines.append(f'    CONSTRAINT {qi(snake(k["type"] + "_" + str(e["id"]) + "_" + e["table"])[:PG_MAX_IDENT])} '
                             f'UNIQUE ({", ".join(map(qi, k["columns"]))})')
        out.append(f'CREATE TABLE {t} (\n' + ',\n'.join(lines) + '\n);')
        if e['definition']:
            out.append(f'COMMENT ON TABLE {t} IS {ql(e["definition"])};')
        for c in e['columns']:
            if c['definition']:
                out.append(f'COMMENT ON COLUMN {t}.{qi(c["column"])} IS {ql(c["definition"])};')
        out.append('')
    by_table = {e['table']: e for e in m['entities']}
    for r in m['relationships']:
        parent = by_table[r['parent']]
        unique_sets = [sorted(c['column'] for c in parent['columns'] if c['primary_key'])]
        unique_sets += [sorted(k['columns']) for k in parent['keys'] if k['type'].startswith('AK')]
        # only emit FKs that reference the parent's full primary or alternate key
        if r['child_columns'] and sorted(r['parent_columns']) in unique_sets:
            name = snake(f'fk_{r["id"]}_{r["child"]}_{r["parent"]}')[:PG_MAX_IDENT]
            out.append(f'ALTER TABLE {s}.{qi(r["child"])} ADD CONSTRAINT {qi(name)} '
                       f'FOREIGN KEY ({", ".join(map(qi, r["child_columns"]))}) '
                       f'REFERENCES {s}.{qi(r["parent"])} ({", ".join(map(qi, r["parent_columns"]))});')
    for v in m['views']:
        out.append('')
        out.append(f'-- erwin view {v["view"]}')
        for n in v['notes']:
            out.append(f'-- {n}')
        if v['user_sql']:
            out.append('-- erwin also stores this user-defined SQL for the view (uses erwin names; kept for reference):')
            out += ['--   ' + ln for ln in v['user_sql'].replace('\r', '').strip().splitlines()]
        if not v['sources'] or not v['columns']:
            continue
        t = f'{s}.{qi(v["name"])}'
        first = v['sources'][0]
        sql = [f'CREATE VIEW {t} AS', 'SELECT',
               ',\n'.join(f'    {c["sql"]} AS {qi(c["column"])}' for c in v['columns']),
               f'FROM {s}.{qi(first["table"])} {first["alias"]}']
        for j in v['joins']:
            src = f'{s}.{qi(j["source"]["table"])} {j["source"]["alias"]}'
            if j['on']:
                cond = ' AND '.join(f'{a}.{qi(ac)} = {b}.{qi(bc)}' for a, ac, b, bc in j['on'])
                sql.append(f'JOIN {src} ON {cond}')
            else:
                sql.append(f'CROSS JOIN {src}')
        out.append('\n'.join(sql) + ';')
        if v['definition']:
            out.append(f'COMMENT ON VIEW {t} IS {ql(v["definition"])};')
        for c in v['columns']:
            if c['definition']:
                out.append(f'COMMENT ON COLUMN {t}.{qi(c["column"])} IS {ql(c["definition"])};')
    out += ['', 'COMMIT;']
    return '\n'.join(out) + '\n'


def ddl_sqlserver(m, schema):
    """The same objects as ddl(), as one T-SQL batch in one transaction (run it with sqlcmd, SSMS or a driver).
    Definitions become MS_Description extended properties; views go through EXEC because CREATE VIEW must
    otherwise start its own batch."""
    s = qi(schema)
    nq = lambda text: "N'" + text.replace("'", "''") + "'"

    def describe(text, kind, obj, column=None):
        args = [f"@name = N'MS_Description', @value = {nq(text)}", f"@level0type = N'SCHEMA', @level0name = {nq(schema)}",
                f"@level1type = N'{kind}', @level1name = {nq(obj)}"]
        if column:
            args.append(f"@level2type = N'COLUMN', @level2name = {nq(column)}")
        return 'EXEC sys.sp_addextendedproperty ' + ', '.join(args) + ';'

    out = [f'-- Generated from erwin model "{m["model"]}" by erwin2postgres.py --dialect sqlserver',
           '-- Runs in one transaction: if any statement fails, nothing is created.',
           'SET ANSI_NULLS ON;', 'SET QUOTED_IDENTIFIER ON;', 'SET XACT_ABORT ON;', 'BEGIN TRANSACTION;', '',
           f"IF SCHEMA_ID({nq(schema)}) IS NULL EXEC(N'CREATE SCHEMA {s.replace(chr(39), chr(39) * 2)}');", '']
    for e in m['entities']:
        t = f'{s}.{qi(e["table"])}'
        lines = [f'    {qi(c["column"])} {mssql_type(c["datatype"])}{" NOT NULL" if c["primary_key"] else ""}'
                 for c in e['columns']]
        pk = [c['column'] for c in e['columns'] if c['primary_key']]
        if pk:
            lines.append(f'    PRIMARY KEY ({", ".join(map(qi, pk))})')
        for k in e['keys']:
            if k['type'].startswith('AK') and k['columns'] and sorted(k['columns']) != sorted(pk):
                lines.append(f'    CONSTRAINT {qi(snake(k["type"] + "_" + str(e["id"]) + "_" + e["table"])[:MSSQL_MAX_IDENT])} '
                             f'UNIQUE ({", ".join(map(qi, k["columns"]))})')
        out.append(f'CREATE TABLE {t} (\n' + ',\n'.join(lines) + '\n);')
        if e['definition']:
            out.append(describe(e['definition'], 'TABLE', e['table']))
        for c in e['columns']:
            if c['definition']:
                out.append(describe(c['definition'], 'TABLE', e['table'], c['column']))
        out.append('')
    by_table = {e['table']: e for e in m['entities']}
    for r in m['relationships']:
        parent = by_table[r['parent']]
        unique_sets = [sorted(c['column'] for c in parent['columns'] if c['primary_key'])]
        unique_sets += [sorted(k['columns']) for k in parent['keys'] if k['type'].startswith('AK')]
        if r['child_columns'] and sorted(r['parent_columns']) in unique_sets:
            name = snake(f'fk_{r["id"]}_{r["child"]}_{r["parent"]}')[:MSSQL_MAX_IDENT]
            out.append(f'ALTER TABLE {s}.{qi(r["child"])} ADD CONSTRAINT {qi(name)} '
                       f'FOREIGN KEY ({", ".join(map(qi, r["child_columns"]))}) '
                       f'REFERENCES {s}.{qi(r["parent"])} ({", ".join(map(qi, r["parent_columns"]))});')
    for v in m['views']:
        out.append('')
        out.append(f'-- erwin view {v["view"]}')
        for n in v['notes']:
            out.append(f'-- {n}')
        if not v['sources'] or not v['columns']:
            continue
        first = v['sources'][0]
        sql = [f'CREATE VIEW {s}.{qi(v["name"])} AS', 'SELECT',
               ',\n'.join(f'    {c["sql"]} AS {qi(c["column"])}' for c in v['columns']),
               f'FROM {s}.{qi(first["table"])} {first["alias"]}']
        for j in v['joins']:
            src = f'{s}.{qi(j["source"]["table"])} {j["source"]["alias"]}'
            if j['on']:
                sql.append(f'JOIN {src} ON ' + ' AND '.join(f'{a}.{qi(ac)} = {b}.{qi(bc)}' for a, ac, b, bc in j['on']))
            else:
                sql.append(f'CROSS JOIN {src}')
        out.append(f'EXEC({nq(chr(10).join(sql))});')
        if v['definition']:
            out.append(describe(v['definition'], 'VIEW', v['name']))
        for c in v['columns']:
            if c['definition']:
                out.append(describe(c['definition'], 'VIEW', v['name'], c['column']))
    out += ['', 'COMMIT TRANSACTION;']
    return '\n'.join(out) + '\n'


# governance metadata: subject areas, valid values, user-defined properties, naming standards
C_SUBJECT_AREA = 0x40200026
P_SUBJECT_AREA_ENTITIES = 0x40203015
P_SUBJECT_AREA_ORDER = 0x40202AB9
C_VALID_VALUE_LIST = 0x4020002C     # e.g. Movie_genre
C_VALID_VALUE = 0x40200008          # child of the list: name = code
P_VALID_VALUE_DESCRIPTION = 0x4020271D
C_CONSTRAINT_USAGE = 0x402000C1     # owned by an attribute, points at the list it uses
P_USAGE_TARGET = 0x40202765
C_UDP = 0x40000004                  # UDP definition; its id is the property id used on objects
P_UDP_TYPE = 0x40204E63
UDP_TYPE_DATE = 3
C_NAMING_STANDARD = 0x40200163
P_NAMING_ENTRIES = 0x402033C0       # 'WORD\x1fABBREV\x1f...'
GLOSSARY_COLUMNS = ['Name', 'Artifact Type', 'Category', 'Description', 'Abbreviations', 'Secondary Categories']


def term_name(name):
    """erwin logical names are often all upper or all lower case; give those title case, keep mixed case as is."""
    name = re.sub(r'\s+', ' ', name.replace('_', ' ')).strip()
    if name == name.upper() or name == name.lower():
        return ' '.join(w[:1].upper() + w[1:].lower() for w in name.split(' '))
    return name


def build_glossary(model, m, schema, root):
    """Business terms, categories, term assignments, reference data and custom properties from the model."""
    objs = model.objs
    entities = {e['id']: e for e in m['entities']}
    col_of = model.col_by_attr
    sep = ' >> '

    # categories: the model's root category plus one per subject area
    areas = sorted((o for o in objs.values() if o.cls == C_SUBJECT_AREA and not str(o.props.get(P_NAME, '')).startswith('<')),
                   key=lambda o: o.props.get(P_SUBJECT_AREA_ORDER, 0))
    categories = [{'name': root, 'path': '', 'description': f'Business terms from the erwin model {m["model"]}'}]
    areas_of = {}
    for a in areas:
        categories.append({'name': a.props[P_NAME], 'path': root,
                           'description': a.props.get(P_DEFINITION) or f'erwin subject area {a.props[P_NAME]}'})
        for eid in a.props.get(P_SUBJECT_AREA_ENTITIES, []):
            areas_of.setdefault(eid, []).append(root + sep + a.props[P_NAME])

    abbrev = {}
    for o in objs.values():
        if o.cls == C_NAMING_STANDARD:
            for entry in o.props.get(P_NAMING_ENTRIES) or []:
                parts = entry.split('\x1f')
                if len(parts) > 1 and parts[0] and parts[1]:
                    abbrev[parts[0].strip().lower()] = parts[1].strip()

    # an entity in several subject areas is filed under the one named like it, else the first;
    # the others become secondary categories
    for eid, paths in areas_of.items():
        ename = entities[eid]['entity'].lower() if eid in entities else ''
        named = [p for p in paths if p.split(sep)[-1].lower() in ename]
        if named:
            paths.insert(0, paths.pop(paths.index(named[0])))

    terms, notes = {}, []

    def add_term(name, definition, cats, source):
        key = name.lower()
        t = terms.get(key)
        if t is None:
            terms[key] = {'name': name, 'definition': definition, 'categories': list(cats),
                          'abbreviation': abbrev.get(key, ''), 'source': source}
        else:
            if not t['definition'] and definition:
                t['definition'] = definition
            elif definition and t['definition'] and definition.strip() != t['definition'].strip():
                notes.append(f'"{name}" has different definitions in {t["source"]} and {source}; kept the first')
            t['categories'] += [c for c in cats if c not in t['categories']]
        return terms[key]['name']

    assignments = []
    term_of_attr, evidence = {}, {}
    for e in m['entities']:
        cats = areas_of.get(e['id']) or [root]
        ent_term = add_term(term_name(e['entity']), e['definition'], cats, f'entity {e["entity"]}')
        assignments.append({'asset_type': 'table', 'table': e['table'], 'column': '', 'term': ent_term})
    # attributes: a migrated (foreign key) attribute takes the term of the attribute it came from
    attrs_of = {}
    for o in objs.values():
        if o.cls == C_ATTRIBUTE:
            attrs_of.setdefault(o.owner, []).append(o)
    for e in m['entities']:
        cats = areas_of.get(e['id']) or [root]
        for a in attrs_of.get(e['id'], []):
            origin, seen = a, set()
            while P_PARENT_ATTRIBUTE in origin.props and origin.props[P_PARENT_ATTRIBUTE] in objs and origin.id not in seen:
                seen.add(origin.id)
                origin = objs[origin.props[P_PARENT_ATTRIBUTE]]
            if origin.id not in term_of_attr:
                owner = entities.get(origin.owner)
                ocats = (areas_of.get(origin.owner) or [root]) if owner else cats
                term_of_attr[origin.id] = add_term(term_name(model.attr_name(origin)), model.attr_definition(origin), ocats,
                                                   f'attribute {owner["entity"] if owner else e["entity"]}.{model.attr_name(origin)}')
            term_of_attr[a.id] = term_of_attr[origin.id]
            ev = evidence.setdefault(term_of_attr[a.id], {'domains': set(), 'entities': set()})
            dom = objs.get(a.props.get(P_PARENT_DOMAIN))
            if dom is not None and not str(dom.props.get(P_NAME, '')).startswith('<') \
                    and dom.props.get(P_NAME) not in ('String', 'Number', 'Datetime', 'Blob'):
                ev['domains'].add(dom.props[P_NAME])
            ev['entities'].add(e['entity'])
    done = set()
    for a_id, (table, col) in col_of.items():
        if (table, col) in done or a_id not in term_of_attr:
            continue
        done.add((table, col))
        assignments.append({'asset_type': 'column', 'table': table, 'column': col, 'term': term_of_attr[a_id]})
    col_terms = {(x['table'], x['column']): x['term'] for x in assignments if x['asset_type'] == 'column'}
    for v in m['views']:
        if not v['sources'] or not v['columns']:
            continue
        assignments.append({'asset_type': 'view', 'table': v['name'], 'column': '', 'term': ''})
        alias_table = {s['alias']: s['table'] for s in v['sources']}
        for c in v['columns']:
            hit = re.fullmatch(r'(\w+)\."([^"]+)"', c['sql'])
            term = col_terms.get((alias_table.get(hit.group(1)), hit.group(2))) if hit else None
            if term:
                assignments.append({'asset_type': 'view column', 'table': v['name'], 'column': c['column'], 'term': term})
    assignments = [x for x in assignments if x['term']]

    # valid value lists -> reference data, and the columns that use them
    reference = []
    for lst in (o for o in objs.values() if o.cls == C_VALID_VALUE_LIST):
        values = [(c.props.get(P_NAME, ''), c.props.get(P_VALID_VALUE_DESCRIPTION, ''))
                  for c in lst.children if c.cls == C_VALID_VALUE]
        if not values:
            continue
        used_by = []
        for u in objs.values():
            if u.cls == C_CONSTRAINT_USAGE and u.props.get(P_USAGE_TARGET) == lst.id and u.owner in col_of:
                used_by.append(col_of[u.owner])
        reference.append({'name': lst.props.get(P_NAME, f'valid_values_{lst.id}'), 'values': values,
                          'columns': list(dict.fromkeys(used_by))})

    # user-defined properties set on entities and attributes (e.g. Data Steward)
    udp = {o.id: re.sub(r'^\w+\.(Logical|Physical)\.', '', o.props.get(P_NAME, '')).strip()
           for o in objs.values() if o.cls == C_UDP}
    date_udps = {o.id for o in objs.values() if o.cls == C_UDP and o.props.get(P_UDP_TYPE) == UDP_TYPE_DATE}

    def udp_value(pid, val):
        if pid in date_udps and str(val).isdigit():
            # stored as a day count from 1899-12-30, like an Excel/OLE date
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))).isoformat()
        return val
    props = []
    for o in objs.values():
        for pid, val in o.props.items():
            if pid in udp and val not in (None, ''):
                if o.cls == C_ENTITY and o.id in entities:
                    props.append({'table': entities[o.id]['table'], 'column': '', 'property': udp[pid],
                                  'value': udp_value(pid, val)})
                elif o.cls == C_ATTRIBUTE and o.id in col_of:
                    props.append({'table': col_of[o.id][0], 'column': col_of[o.id][1], 'property': udp[pid],
                                  'value': udp_value(pid, val)})

    suggestions = suggest_classifications(terms, evidence, assignments)
    return {'schema': schema, 'categories': categories, 'terms': list(terms.values()),
            'assignments': assignments, 'reference_data': reference, 'custom_properties': props, 'notes': notes,
            'classification_suggestions': suggestions}


SENSITIVE, PII, PERSONAL = 'Sensitive Personal Information', 'Personally Identifiable Information', 'Personal Information'
CLASS_RULES = [
    (SENSITIVE, r'soc(ial)? ?sec|\bssn\b|social security|tax ?id|passport|driver.?s? licen|credit card|card (number|num|no)\b|'
                r'\bcvv\b|\bbank\b|account (number|num|no)\b|\biban\b|routing|salary|\bwage|pay ?rate|compensation|'
                r'medical|diagnos|health|religio|ethnic|biometric'),
    (PII, r'first name|last name|surname|full name|given name|e-?mail|phone|mobile|\bfax\b|address|birth|\bdob\b'),
    (PERSONAL, r'\bcity\b|\bstate\b|\bzip\b|postal|post ?code|country|gender|\bsex\b|\bage\b|hire date|marital|nationality|'
               r'(customer|employee|person|patient|member|client|user|staff) (number|num|no|id)\b'),
]
ORGANISATION = r'\b(store|shop|company|vendor|supplier|branch|office|warehouse|organi[sz]ation|publisher)\b'
PUBLIC_FIGURE = r'\b(star|director|author|actor|artist|composer)\b'


def suggest_classifications(terms, evidence, assignments):
    """Keyword-based classification suggestions for attribute terms. A starting point to review, not a decision."""
    cols = {}
    for x in assignments:
        if x['column']:
            cols.setdefault(x['term'], []).append(f"{x['table']}.{x['column']}")
    out = []
    for t in terms.values():
        if t['name'] not in evidence:
            continue  # entity terms describe tables, not personal data values
        domains = sorted(evidence[t['name']]['domains'])
        name = t['name'].lower()
        text = ' '.join([name] + [d.lower() for d in domains])
        hit = next(((c, rx) for c, rx in CLASS_RULES if re.search(rx, text)), None)
        if not hit:
            continue
        cls, conf = hit[0], 'high'  # the name itself matches; lowered below for caveats
        how = 'name' if re.search(hit[1], name) else 'erwin domain'
        reason = f'{how} matches {cls.lower()} patterns'
        if re.search(ORGANISATION, name):
            cls, conf, reason = '', 'review', 'looks like data about an organisation, not a person'
        elif re.search(PUBLIC_FIGURE, name):
            cls, conf, reason = '', 'review', 'looks like a public credit (e.g. actor, director), not personal data'
        elif re.search(r'\b(type|status|code|flag)$', name):
            conf, reason = 'review', reason + '; but the name suggests a code or category rather than the value itself'
        elif how == 'erwin domain':
            conf = 'medium'
        out.append({'name': t['name'], 'classification': cls, 'confidence': conf, 'domains': ', '.join(domains),
                    'reason': reason, 'definition': t['definition'], 'columns': '; '.join(cols.get(t['name'], []))})
    return out


def write_glossary(g, outdir):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'glossary.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(GLOSSARY_COLUMNS)
        for c in g['categories']:
            w.writerow([c['name'], 'category', c['path'], c['description'], '', ''])
        for t in g['terms']:
            # the watsonx.data intelligence import takes one path per Secondary Categories cell
            # (a comma-separated list is read as a single, non-existent path), so extra ones go to their own file
            w.writerow([t['name'], 'glossary_term', t['categories'][0], t['definition'], t['abbreviation'],
                        t['categories'][1] if len(t['categories']) > 1 else ''])
    extra = [(t['name'], c) for t in g['terms'] for c in t['categories'][2:]]
    if extra:
        with open(os.path.join(outdir, 'extra_secondary_categories.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['term', 'secondary_category'])
            w.writerows(extra)
    with open(os.path.join(outdir, 'term_assignments.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['schema', 'table', 'column', 'asset_type', 'term'])
        for a in g['assignments']:
            w.writerow([g['schema'], a['table'], a['column'], a['asset_type'], a['term']])
    if g['reference_data']:
        refdir = os.path.join(outdir, 'reference_data')
        os.makedirs(refdir, exist_ok=True)
        with open(os.path.join(outdir, 'reference_data_usage.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['reference_data_file', 'schema', 'table', 'column'])
            for r in g['reference_data']:
                fname = f'{snake(r["name"])}.csv'
                with open(os.path.join(refdir, fname), 'w', newline='') as rf:
                    rw = csv.writer(rf)
                    rw.writerow(['code', 'value'])
                    rw.writerows(r['values'])
                for table, col in r['columns'] or [('', '')]:
                    w.writerow([f'reference_data/{fname}', g['schema'], table, col])
    with open(os.path.join(outdir, 'classification_suggestions.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Name', 'Classifications', 'confidence', 'erwin_domains', 'reason', 'definition', 'columns'])
        for x in g['classification_suggestions']:
            w.writerow([x['name'], x['classification'], x['confidence'], x['domains'], x['reason'], x['definition'], x['columns']])
    if g['custom_properties']:
        with open(os.path.join(outdir, 'custom_properties.csv'), 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['schema', 'table', 'column', 'property', 'value'])
            for p in g['custom_properties']:
                w.writerow([g['schema'], p['table'], p['column'], p['property'], p['value']])


def write_metadata(m, outdir):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'model.json'), 'w') as f:
        json.dump(m, f, indent=2)
    with open(os.path.join(outdir, 'tables.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['model', 'entity', 'table', 'column_count', 'definition'])
        for e in m['entities']:
            w.writerow([m['model'], e['entity'], e['table'], len(e['columns']), e['definition']])
    with open(os.path.join(outdir, 'columns.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['entity', 'table', 'attribute', 'column', 'datatype', 'postgres_type',
                    'primary_key', 'foreign_key', 'definition'])
        for e in m['entities']:
            for c in e['columns']:
                w.writerow([e['entity'], e['table'], c['attribute'], c['column'], c['datatype'], c['pg_type'],
                            c['primary_key'], c['foreign_key'], c['definition']])
    with open(os.path.join(outdir, 'relationships.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name', 'parent_table', 'child_table', 'child_columns', 'parent_columns',
                    'phrase', 'inverse_phrase', 'definition'])
        for r in m['relationships']:
            w.writerow([r['name'], r['parent'], r['child'], ';'.join(r['child_columns']),
                        ';'.join(r['parent_columns']), r['phrase'], r['inverse_phrase'], r['definition']])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('model', help='path to the .erwin file')
    ap.add_argument('--schema', help='Postgres schema to create (default: model file name in snake_case)')
    ap.add_argument('--out', help='SQL file to write (default: <schema>.<dialect>.sql next to the model)')
    ap.add_argument('--dialect', choices=['postgres', 'sqlserver'], default='postgres',
                    help='SQL to write: PostgreSQL (default) or SQL Server T-SQL')
    ap.add_argument('--metadata', metavar='DIR', help='also write model.json and tables/columns/relationships CSVs here')
    ap.add_argument('--glossary', metavar='DIR',
                    help='also write governance files for a catalog: glossary.csv (business terms + categories), '
                         'term_assignments.csv, reference data and custom properties')
    ap.add_argument('--category', help='root glossary category for the terms (default: model file name)')
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.model))[0]
    schema = args.schema or snake(base)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.model)), f'{schema}.{args.dialect}.sql')
    model = Model(args.model)
    m = model.build()
    if not m['entities']:
        sys.exit(f'No entities found in {args.model}. This reader handles erwin Data Modeler r9 and later '
                 f'binary .erwin files; save older or XML models from erwin in that format first.')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'w') as f:
        f.write(ddl(m, schema, args.dialect))
    if args.metadata:
        write_metadata(m, args.metadata)
    if args.glossary:
        g = build_glossary(model, m, schema, args.category or base)
        write_glossary(g, args.glossary)

    fks = sum(1 for line in ddl(m, schema, args.dialect).splitlines() if 'FOREIGN KEY' in line)
    made = [v for v in m['views'] if v['sources'] and v['columns']]
    ncols = sum(len(e['columns']) for e in m['entities'])
    print(f'{m["model"]}: {len(m["entities"])} tables ({ncols} columns), {fks} foreign keys, '
          f'{len(made)} of {len(m["views"])} views -> {out} (schema "{schema}")')
    for v in m['views']:
        for n in v['notes']:
            print(f'  view {v["name"]}: {n}')
    if args.glossary:
        print(f'  glossary: {len(g["categories"])} categories, {len(g["terms"])} terms, '
              f'{len(g["assignments"])} term assignments, {len(g["reference_data"])} reference data sets, '
              f'{len(g["custom_properties"])} custom property values -> {args.glossary}')
        extra = sum(max(0, len(t['categories']) - 2) for t in g['terms'])
        if extra:
            print(f'  glossary: {extra} additional secondary categories (beyond one per term) are in '
                  f'extra_secondary_categories.csv; add them after the import')
        for n in g['notes']:
            print(f'  glossary: {n}')
    if model.failed:
        print(f'  note: {model.failed} records in the file could not be decoded and were skipped')


if __name__ == '__main__':
    main()
