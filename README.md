"""Offline evidence preparation. No network, tokens, or extraction execution.

Reads the completed paired exports and comparison outputs. Writes a NEW
timestamped evidence folder. Does not alter any source or receipt.
"""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

csv.field_size_limit(100_000_000)
D = Decimal

def norm(s):
    return ''.join(c.lower() for c in s if c.isalnum())

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def one(paths, label):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError(f'{label}: expected one file, found {len(paths)}')
    return paths[0]

def write(path, rows, fields):
    with path.open('x', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            # Prevent CSV names/titles becoming spreadsheet formulas.
            safe = {k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '@', '-'))
                        and k not in ('delta_minutes', 'delta_hours', 'effort_hours') else v)
                    for k, v in r.items()}
            w.writerow(safe)

def dates(path, ids):
    found = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        lookup = {norm(c): c for c in reader.fieldnames}
        key = lookup.get('key')
        if not key:
            raise ValueError(f'No key column in {path}')
        wanted = ('createddate', 'updateddate', 'title', 'id', 'datarefresh')
        for row in reader:
            entity = row[key].strip()
            if entity not in ids:
                continue
            item = found.setdefault(entity, {k: set() for k in wanted})
            for k in wanted:
                if lookup.get(k) and row.get(lookup[k], '').strip():
                    item[k].add(row[lookup[k]].strip())
    return {key: {k: '; '.join(sorted(v)) for k, v in row.items()}
            for key, row in found.items()}

def main():
    home = Path.home()
    root = home / 'Desktop/missing-projects/paired_token_runs_20260930'
    pair = root / 'surya_vs_akash_comparison'
    access = home / 'Desktop/wrike-local-baseline/output/project_access_today/20260930T161721_018375Z/project_access_review.csv'
    evidence = {}
    for row in read(access):
        key = row['container_id']
        if key in evidence:
            raise ValueError('Duplicate access evidence: ' + key)
        evidence[key] = row
    summaries, exports, receipts = {}, {}, {}
    manifest = {'inputs': {}, 'limitations': [
        'Raw row effort retains repeated rows. Hours equal minutes / 60.',
        'Access observations are earlier evidence, not a fresh permissions check.',
        'Path association and zero residual do not establish access cause or completeness.',
        'Source identity follows user-entered token labels.',
        'Creation dates are reported as recorded; task/date ownership is not independently verified.'
    ]}
    def record(path):
        manifest['inputs'][str(path)] = sha(path)
    record(access)
    for person in ('surya', 'akash'):
        run = one((root / person / 'output/raw_token_comparison').glob('*/extraction_receipt.json'), person)
        receipt = json.loads(run.read_text())
        csv_path = one((root / person / 'output/local_validation').glob('*/wrike_local_full.csv'), person)
        if sha(csv_path) != receipt['csv']['sha256']:
            raise ValueError(person + ' export differs from saved receipt')
        sf = root / person / 'snowflake_snapshot.csv'
        if sha(sf) != receipt['snapshot_sha256']:
            raise ValueError(person + ' snapshot differs from saved receipt')
        if receipt.get('extractor_exit_code') != 0:
            raise ValueError(person + ' extractor exit code is not zero')
        summary_path = run.parent / 'comparison/summary.json'
        summary = json.loads(summary_path.read_text())
        if summary['sources']['akash']['sha256'] != sha(csv_path):
            raise ValueError(person + ' summary export hash mismatch')
        if summary['sources']['snapshot']['sha256'] != sha(sf):
            raise ValueError(person + ' summary snapshot hash mismatch')
        summaries[person] = summary
        exports[person] = csv_path
        receipts[person] = run
        for p in (run, csv_path, sf, summary_path):
            record(p)
    if summaries['surya']['sources']['snapshot']['sha256'] != summaries['akash']['sources']['snapshot']['sha256']:
        raise ValueError('Frozen snapshots differ')
    pair_summary = pair / 'summary.json'
    paired = json.loads(pair_summary.read_text())
    for side, person in (('snapshot', 'surya'), ('akash', 'akash')):
        if paired['sources'][side]['sha256'] != sha(exports[person]):
            raise ValueError('Paired comparison is not from the verified ' + person + ' export')
    record(pair_summary)
    containers = read(pair / 'container_differences.csv')
    raw = read(pair / 'raw_row_differences.csv')
    for name in ('container_differences.csv', 'raw_row_differences.csv', 'task_differences.csv'):
        record(pair / name)
    shared = {r['container_id'] for r in containers if r['classification'] == 'PRESENT_BOTH_DIFFERENT_ROWS'}
    paths = ('id', 'parent_folder_id', 'child_folder_id', 'grandchild_folder_id', 'baby_folder_id', 'grandbaby_folder_id')
    residual = []
    groups = defaultdict(lambda: D(0))
    for row in raw:
        if row['container_id'] not in shared:
            continue
        if any(evidence.get(row.get(k, ''), {}).get('akash_access', '').strip().upper() == 'NOT_FOUND' for k in paths):
            continue
        item = dict(row)
        item['delta_hours'] = str(D(row['delta_minutes']) / 60)
        item['container_name'] = evidence.get(row['container_id'], {}).get('name', '')
        residual.append(item)
        groups[(row['container_id'], item['container_name'])] += D(row['delta_minutes'])
    residual_total = sum((D(r['delta_minutes']) for r in residual), D(0))
    sf_task_path = receipts['surya'].parent / 'comparison/task_differences.csv'
    record(sf_task_path)
    sf_diff = [r for r in read(sf_task_path) if D(r['delta_minutes']) != 0]
    ids = {r['entity_key'] for r in sf_diff}
    details = {person: dates(exports[person], ids) for person in exports}
    ooo = []
    for row in sf_diff:
        for person in ('surya', 'akash'):
            item = details[person].get(row['entity_key'], {})
            ooo.append(dict(entity_id=row['entity_key'], source=person,
                present_as_key=bool(item), createdDate=item.get('createddate', ''),
                updatedDate=item.get('updateddate', ''), title=item.get('title', ''),
                container_id=item.get('id', ''), data_refresh=item.get('datarefresh', ''),
                snowflake_minus_surya_minutes=row['delta_minutes']))
    missing = []
    for row in containers:
        e = evidence.get(row['container_id'], {})
        if row['classification'] == 'SNAPSHOT_ONLY' and e.get('current_type', '').strip().lower() == 'project':
            missing.append(dict(project_id=row['container_id'], project_name=e.get('name') or row['snapshot_names'],
                                effort_hours=str(D(row['snapshot_numeric_minutes']) / 60)))
    out = root / ('closeout_evidence_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
    out.mkdir()
    write(out / 'remaining_path_rows.csv', residual,
          list(raw[0]) + ['delta_hours', 'container_name'])
    group_rows = [dict(container_id=cid, container_name=name, delta_minutes=str(v), delta_hours=str(v / 60))
                  for (cid, name), v in sorted(groups.items(), key=lambda kv: -abs(kv[1]))]
    write(out / 'remaining_path_containers.csv', group_rows,
          ['container_id', 'container_name', 'delta_minutes', 'delta_hours'])
    write(out / 'ooo_dates.csv', ooo, ['entity_id', 'source', 'present_as_key', 'title', 'createdDate', 'updatedDate',
                                     'container_id', 'data_refresh', 'snowflake_minus_surya_minutes'])
    write(out / 'missing_projects.csv', missing, ['project_id', 'project_name', 'effort_hours'])
    manifest['snapshot_recorded_times'] = summaries['surya']['sources']['snapshot']['observed_time_values']
    manifest['remaining_path_minutes'] = str(residual_total)
    manifest['missing_project_count'] = len(missing)
    manifest['missing_project_minutes'] = str(sum((D(r['effort_hours']) * 60 for r in missing), D(0)))
    # Detect modifications while preparing the evidence.
    for path, digest in manifest['inputs'].items():
        if sha(Path(path)) != digest:
            raise ValueError('Input changed during analysis: ' + path)
    (out / 'evidence_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Verified: paired exports match their saved receipts and comparison source hashes.')
    print('Missing projects:', len(missing))
    print('Remaining path net hours:', f'{residual_total / 60:,.2f}')
    print('Remaining path gross positive hours:', f'{sum((D(r["delta_minutes"]) for r in residual if D(r["delta_minutes"]) > 0), D(0)) / 60:,.2f}')
    print('Remaining path gross negative hours:', f'{sum((D(r["delta_minutes"]) for r in residual if D(r["delta_minutes"]) < 0), D(0)) / 60:,.2f}')
    for r in group_rows:
        if D(r['delta_minutes']):
            print(r['container_id'], '|', r['container_name'], '|', f'{D(r["delta_minutes"]) / 60:,.2f}', 'hours')
    print('Snapshot recorded times:', json.dumps(manifest['snapshot_recorded_times']))
    print('OOO/date evidence (raw source values):')
    for r in ooo:
        print(json.dumps(r))
    print('SAVED:', out)
    print('Evidence prepared; workbook not generated by this script.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        raise SystemExit('STOP: ' + str(exc))
