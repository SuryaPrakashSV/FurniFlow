#!/usr/bin/env python3
"""Compare saved Snowflake and Akash CSV effort rows, preserving multiplicity.

No API calls, token access, extraction imports, or source changes. This is an
effort-row comparison, not an equality test of every source metadata column.
"""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path

getcontext().prec = 160
csv.field_size_limit(16 * 1024 * 1024)
METRIC = 'effortallocation_totaleffort'
TASKS = ('parent_task_id', 'child_task_id', 'grandchild_task_id', 'baby_task_id',
         'grandbaby_task_id', 'great_grandbaby_task_id')
FOLDERS = ('parent_folder_id', 'child_folder_id', 'grandchild_folder_id',
           'baby_folder_id', 'grandbaby_folder_id')
OPTIONAL_CONTEXT = ('owner_userid', 'responsible_userid', 'owner_dailyallocation_date')
NULLS = {'', 'null', 'none', 'nan', 'nat', '<na>', '\\n'}
NUMERIC = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')
ZERO = Decimal(0)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    before = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            'File changed while hashing: ' + str(path))
    return h.hexdigest()


def text(value):
    return '' if value is None else str(value)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def numeric(raw):
    value = raw.strip()
    if value.lower() in NULLS:
        return 'null', None, 'null'
    if len(value) > 100 or not NUMERIC.fullmatch(value):
        return 'invalid', None, 'invalid:' + raw
    try:
        result = Decimal(value)
        if not result.is_finite() or (result and abs(result.adjusted()) > 30):
            return 'invalid', None, 'invalid:' + raw
        result = ZERO if not result else result.normalize()
        return 'number', result, 'number:' + str(result)
    except InvalidOperation:
        return 'invalid', None, 'invalid:' + raw


def identifier(raw):
    value = raw.strip()
    return '' if value.lower() in NULLS or value.upper() == 'PLACEHOLDER' else value


def headers(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.reader(f, strict=True)
        original = next(r, [])
    normalized = [h.strip().lower() for h in original]
    require(normalized and '' not in normalized and len(set(normalized)) == len(normalized),
            'Blank or duplicate normalized CSV headers: ' + str(path))
    missing = sorted({'id', 'key', METRIC, *TASKS, *FOLDERS} - set(normalized))
    require(not missing, 'Missing CSV columns in {}: {}'.format(path, ', '.join(missing)))
    return normalized, original


def identity_kind(row):
    key, cid = identifier(row['key']), identifier(row['id'])
    task_ids = [identifier(row[t]) for t in TASKS]
    folder_ids = {identifier(row[f]) for f in FOLDERS} | {cid}
    deepest = next((v for v in reversed(task_ids) if v), '')
    if key and key in folder_ids and key not in task_ids:
        return 'CONTAINER_OWN', ''
    if key and key == deepest and key not in folder_ids:
        return 'TASK_OWN', key
    return 'OTHER_OR_AMBIGUOUS', ''


def load_csv(db, path, side, cols, context_cols):
    initial = sha(path)
    before = path.stat()
    counts = Counter()
    refresh = {}
    total = ZERO
    referenced = set()
    batch = []
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, strict=True)
        next(reader)
        for n, cells in enumerate(reader, 1):
            require(len(cells) == len(cols), '{}: wrong column count at data row {}, CSV line {}'.format(path, n, reader.line_num))
            row = dict(zip(cols, cells))
            raw = row[METRIC]
            kind, minutes, sig = numeric(raw)
            counts['physical_rows'] += 1
            counts[{'number':'numeric_rows', 'null':'null_rows', 'invalid':'invalid_rows'}[kind]] += 1
            if minutes is not None:
                total += minutes
                counts['negative_numeric_rows'] += minutes < 0
            for name in ('data refresh', 'snapshot_exported_at', 'evidence_exported_at'):
                if name in row:
                    refresh.setdefault(name, Counter())[row[name]] += 1
            context = [identifier(row[c]) for c in context_cols]
            row_type, own_task = identity_kind(row)
            counts[row_type.lower() + '_rows'] += 1
            referenced.update(identifier(row[c]) for c in TASKS if identifier(row[c]))
            batch.append((side, n, reader.line_num, compact(context), sig, kind,
                          text(minutes), raw, identifier(row['key']), identifier(row['id']),
                          own_task, row_type, row.get('title', ''), row.get('is_project', '')))
            if len(batch) >= 2000:
                db.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
                batch.clear()
    if batch:
        db.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
    require(counts['physical_rows'] > 0, 'Empty CSV: ' + str(path))
    require(sha(path) == initial and path.stat().st_mtime_ns == before.st_mtime_ns,
            'CSV changed during comparison: ' + str(path))
    db.executemany('INSERT INTO hierarchy_refs VALUES (?,?)', ((side, v) for v in referenced))
    db.commit()
    return dict(path=str(path), sha256=initial, bytes=before.st_size,
                **{k:counts[k] for k in ('physical_rows', 'numeric_rows', 'null_rows', 'invalid_rows',
                  'negative_numeric_rows', 'task_own_rows', 'container_own_rows', 'other_or_ambiguous_rows')},
                raw_numeric_minutes=str(total), raw_numeric_hours=str(total / 60),
                raw_sum_has_no_invalid_cells=not counts['invalid_rows'],
                observed_time_values={k:dict(v) for k,v in refresh.items()},
                headers=cols)


def check_receipt(path, snapshot, akash):
    if path is None:
        return {'provided':False, 'limitation':'No run receipt supplied; source identity and capture success not established.'}
    path = Path(path).expanduser().resolve()
    receipt = json.loads(path.read_text(encoding='utf-8'))
    require(receipt.get('snapshot_sha256') == snapshot['sha256'], 'Run receipt snapshot hash mismatch')
    exported = receipt.get('csv', {})
    require(exported.get('sha256') == akash['sha256'], 'Run receipt Akash CSV hash mismatch')
    require(exported.get('physical_rows') == akash['physical_rows'], 'Run receipt CSV row count mismatch')
    if exported.get('bytes') is not None:
        require(exported['bytes'] == akash['bytes'], 'Run receipt CSV size mismatch')
    checked = {}
    for label in ('source', 'backup'):
        if receipt.get(label + '_sha256'):
            source = Path(receipt[label + '_path']).expanduser().resolve()
            require(sha(source) == receipt[label + '_sha256'], 'Run receipt ' + label + ' source hash mismatch')
            checked[label + '_sha256'] = receipt[label + '_sha256']
    if receipt.get('extractor_log_sha256'):
        log = Path(receipt.get('extractor_log_path') or receipt.get('log_path') or path.parent / 'extractor.log').expanduser().resolve()
        require(sha(log) == receipt['extractor_log_sha256'], 'Run receipt extractor log hash mismatch')
        checked['extractor_log_sha256'] = receipt['extractor_log_sha256']
    return {'provided':True, 'path':str(path), 'sha256':sha(path), 'verified_files':checked,
            **{k:receipt.get(k) for k in ('status','label','started_utc','finished_utc','extractor_exit_code',
                                         'source_comparison','log_indicators','comparison_status')}}


def own_sets(db):
    return {side:{r[0] for r in db.execute('SELECT DISTINCT own_task FROM observations WHERE side=? AND own_task != ?', (side, ''))}
            for side in ('snapshot', 'akash')}


def refs(raw, skip=0, excel=False):
    values = sorted(int(v) for v in (raw or '').split(',') if v)
    return ';'.join(str(v + (1 if excel else 0)) for v in values[skip:])


def row_ledger(db, out, context_cols, own):
    fields = ['classification','review_flags','entity_key','own_task_id','container_id','row_type','title',
              'effort_kind','effort_minutes','raw_effort_values','snapshot_unmatched_rows','akash_unmatched_rows',
              'snapshot_minutes','akash_minutes','delta_minutes','snapshot_source_excel_rows','akash_source_excel_rows',
              'snapshot_source_csv_line_ends','akash_source_csv_line_ends'] + list(context_cols)
    result = dict(matched_occurrences=0, unmatched_snapshot_occurrences=0, unmatched_akash_occurrences=0,
                  matched_numeric_minutes=ZERO, unmatched_snapshot_numeric_minutes=ZERO,
                  unmatched_akash_numeric_minutes=ZERO, differing_contexts=0, difference_ledger_rows=0)
    classes = Counter()
    query = '''SELECT context,signature,kind,minutes,entity_key,container_id,own_task,row_type,MIN(title),
      SUM(side='snapshot'),SUM(side='akash'),
      GROUP_CONCAT(CASE WHEN side='snapshot' THEN rownum END),
      GROUP_CONCAT(CASE WHEN side='akash' THEN rownum END),
      GROUP_CONCAT(CASE WHEN side='snapshot' THEN lineend END),
      GROUP_CONCAT(CASE WHEN side='akash' THEN lineend END),GROUP_CONCAT(DISTINCT raw_metric)
      FROM observations GROUP BY context,signature ORDER BY context,signature'''
    with (out/'raw_row_differences.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        def emit(groups):
            if not groups:
                return
            context = groups[0][0]
            counts = [sum(g[9] for g in groups), sum(g[10] for g in groups)]
            differing = any(g[9] != g[10] for g in groups)
            result['differing_contexts'] += differing
            flags = []
            if counts[0] != counts[1]: flags.append('MULTIPLICITY_DIFFERENCE')
            values = [{g[1] for g in groups if g[9+i]} for i in (0,1)]
            if values[0] != values[1]: flags.append('VALUE_SET_DIFFERENCE')
            if differing and all(counts): flags.append('EFFORT_FREQUENCY_DIFFERENCE')
            for g in groups:
                _, sig, kind, minutes, key, cid, own_id, row_type, title, sf, ak, sfrefs, akrefs, sflines, aklines, rawvalues = g
                matched = min(sf, ak)
                left, right = sf - matched, ak - matched
                value = Decimal(minutes) if kind == 'number' else ZERO
                result['matched_occurrences'] += matched
                result['matched_numeric_minutes'] += matched * value
                for side, count in (('snapshot',left),('akash',right)):
                    result['unmatched_' + side + '_occurrences'] += count
                    result['unmatched_' + side + '_numeric_minutes'] += count * value
                if not left and not right:
                    continue
                if all(counts):
                    classification = 'CONTEXT_PRESENT_BOTH'
                elif own_id:
                    other = 'akash' if left else 'snapshot'
                    classification = ('OWN_TASK_ABSENT_FROM_' + other.upper() if own_id not in own[other]
                                      else 'OWN_TASK_PRESENT_BOTH_CONTEXT_DIFFERENCE')
                else:
                    classification = 'NON_TASK_OR_AMBIGUOUS_CONTEXT_ONLY_' + ('SNAPSHOT' if left else 'AKASH')
                classes[classification] += left + right
                result['difference_ledger_rows'] += 1
                row = dict(classification=classification,review_flags=';'.join(flags),entity_key=key,
                    own_task_id=own_id,container_id=cid,row_type=row_type,title=title,
                    effort_kind=kind,effort_minutes=minutes,raw_effort_values=rawvalues,
                    snapshot_unmatched_rows=left,akash_unmatched_rows=right,
                    snapshot_minutes=text(value*left) if kind=='number' else '',
                    akash_minutes=text(value*right) if kind=='number' else '',
                    delta_minutes=text(value*(left-right)) if kind=='number' else '',
                    snapshot_source_excel_rows=refs(sfrefs,matched,True),
                    akash_source_excel_rows=refs(akrefs,matched,True),
                    snapshot_source_csv_line_ends=refs(sflines,matched),
                    akash_source_csv_line_ends=refs(aklines,matched))
                row.update(zip(context_cols,json.loads(context)))
                writer.writerow(row)

        groups, previous = [], None
        for group in db.execute(query):
            if previous is not None and group[0] != previous:
                emit(groups); groups = []
            groups.append(group); previous = group[0]
        emit(groups)
    result['classification_occurrences'] = dict(classes)
    return result


def drilldowns(db, out, own):
    """Direct id partitions physical rows; ancestry project rollups would overlap."""
    final = {}
    for column, filename in (('container_id','container_differences.csv'),('entity_key','task_differences.csv')):
        totals = {}
        for key, side, kind, minutes, count in db.execute(
                'SELECT '+column+',side,kind,minutes,COUNT(*) FROM observations GROUP BY '+column+',side,kind,minutes'):
            item = totals.setdefault(key, {s:dict(rows=0,numeric_minutes=ZERO,null_rows=0,invalid_rows=0) for s in ('snapshot','akash')})[side]
            item['rows'] += count
            if kind == 'number': item['numeric_minutes'] += Decimal(minutes)*count
            else: item[kind+'_rows'] += count
        metadata = {}
        for key, side, title, flag, row_type, entity, container in db.execute('SELECT DISTINCT '+column+',side,title,is_project,row_type,entity_key,container_id FROM observations'):
            item = metadata.setdefault((key,side),dict(names=set(),flags=set(),row_types=set()))
            item['row_types'].add(row_type)
            # A task's title cannot be used as its enclosing container's name.
            direct_own = row_type == 'CONTAINER_OWN' and entity == container
            if column == 'entity_key' or direct_own:
                if title: item['names'].add(title)
            if column == 'container_id' and direct_own:
                v=flag.strip().lower()
                if v in ('y','yes','true','1'): item['flags'].add('PROJECT')
                elif v in ('n','no','false','0'): item['flags'].add('FOLDER_OR_NONPROJECT')
                else: item['flags'].add('UNKNOWN')
        fields = [column,'snapshot_names','akash_names','snapshot_type','akash_type','classification',
                  'snapshot_rows','akash_rows','row_count_delta','snapshot_numeric_minutes','akash_numeric_minutes','delta_minutes',
                  'snapshot_numeric_hours','akash_numeric_hours','delta_hours',
                  'snapshot_null_rows','akash_null_rows','snapshot_invalid_rows','akash_invalid_rows',
                  'snapshot_own_task','akash_own_task','snapshot_hierarchy_reference','akash_hierarchy_reference']
        reference = {s:{r[0] for r in db.execute('SELECT task_id FROM hierarchy_refs WHERE side=?',(s,))} for s in ('snapshot','akash')}
        differing_keys = {r[0] for r in db.execute('''SELECT DISTINCT '''+column+''' FROM observations WHERE context IN (
            SELECT context FROM observations GROUP BY context,signature HAVING SUM(side='snapshot') != SUM(side='akash'))''')}
        delta_total=ZERO
        written=0
        with (out/filename).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
            for key in sorted(totals):
                sf,ak=totals[key]['snapshot'],totals[key]['akash']
                delta=sf['numeric_minutes']-ak['numeric_minutes'];delta_total+=delta
                if key not in differing_keys:continue
                written+=1
                row={column:key,'row_count_delta':sf['rows']-ak['rows'],'delta_minutes':str(delta),
                     'snapshot_numeric_hours':format(sf['numeric_minutes']/60,'.6f'),
                     'akash_numeric_hours':format(ak['numeric_minutes']/60,'.6f'),'delta_hours':format(delta/60,'.6f')}
                row['classification']=('PRESENT_BOTH_DIFFERENT_ROWS' if sf['rows'] and ak['rows'] else
                                       'SNAPSHOT_ONLY' if sf['rows'] else 'AKASH_ONLY')
                for side in ('snapshot','akash'):
                    item=totals[key][side];meta=metadata.get((key,side),dict(names=set(),flags=set(),row_types=set()))
                    row[side+'_names']='; '.join(sorted(meta['names']))
                    row[side+'_type']=';'.join(sorted(meta['flags'] if column=='container_id' else meta['row_types'])) or 'UNKNOWN'
                    for k,v in item.items():row[side+'_'+k]=text(v)
                    row[side+'_own_task']=int(key in own[side]) if column=='entity_key' else ''
                    row[side+'_hierarchy_reference']=int(key in reference[side]) if column=='entity_key' else ''
                w.writerow(row)
        final[filename]=dict(differing_keys=written,delta_minutes=str(delta_total),
            scope='All physical rows grouped by '+column+'; blank IDs retained. This is not a unique-task effort total.')
    return final


def compare(snapshot, akash, out, run_receipt=None):
    snapshot,akash,out=(Path(p).expanduser().resolve() for p in (snapshot,akash,out))
    require(snapshot!=akash,'Snapshot and Akash inputs must be different files')
    scols,soriginal=headers(snapshot);acols,aoriginal=headers(akash)
    optional=[c for c in OPTIONAL_CONTEXT if c in scols or c in acols]
    require(all((c in scols)==(c in acols) for c in OPTIONAL_CONTEXT),
            'Optional owner/date context columns are not present in both files; review schema before comparing')
    context_cols=('id','key',*FOLDERS,*TASKS,*optional)
    out.mkdir(parents=True,exist_ok=False)
    write_json(out/'summary.json',{'status':'RUNNING','validation_complete':False})
    db=sqlite3.connect(str(out/'row_comparison.sqlite'))
    db.execute('PRAGMA temp_store=FILE')
    db.execute('''CREATE TABLE observations(side TEXT,rownum INTEGER,lineend INTEGER,context TEXT,
        signature TEXT,kind TEXT,minutes TEXT,raw_metric TEXT,entity_key TEXT,container_id TEXT,
        own_task TEXT,row_type TEXT,title TEXT,is_project TEXT)''')
    db.execute('CREATE TABLE hierarchy_refs(side TEXT,task_id TEXT,PRIMARY KEY(side,task_id))')
    try:
        sources={s:load_csv(db,p,s,c,context_cols) for s,p,c in
                 [('snapshot',snapshot,scols),('akash',akash,acols)]}
        receipt=check_receipt(run_receipt,sources['snapshot'],sources['akash'])
        db.execute('CREATE INDEX obs_context ON observations(context,signature)')
        db.execute('CREATE INDEX obs_key ON observations(entity_key)')
        db.execute('CREATE INDEX obs_container ON observations(container_id)')
        db.commit()
        own=own_sets(db)
        ledger=row_ledger(db,out,context_cols,own)
        rawdelta=Decimal(sources['snapshot']['raw_numeric_minutes'])-Decimal(sources['akash']['raw_numeric_minutes'])
        residual=rawdelta-ledger['unmatched_snapshot_numeric_minutes']+ledger['unmatched_akash_numeric_minutes']
        require(residual==0,'Internal error: raw minute bridge does not reconcile')
        for side in ('snapshot','akash'):
            require(ledger['matched_occurrences']+ledger['unmatched_'+side+'_occurrences']==sources[side]['physical_rows'],
                    'Internal error: physical row ledger does not reconcile')
            require(ledger['matched_numeric_minutes']+ledger['unmatched_'+side+'_numeric_minutes']==Decimal(sources[side]['raw_numeric_minutes']),
                    'Internal error: raw numeric subtotal does not reconcile')
        drill=drilldowns(db,out,own)
        require(all(Decimal(v['delta_minutes'])==rawdelta for v in drill.values()),'Internal error: drilldown bridge differs')
        refs_by_side={s:{r[0] for r in db.execute('SELECT task_id FROM hierarchy_refs WHERE side=?',(s,))} for s in ('snapshot','akash')}
        tasks={'snapshot_count':len(own['snapshot']),'akash_count':len(own['akash']),
               'snapshot_only':len(own['snapshot']-own['akash']),'akash_only':len(own['akash']-own['snapshot']),
               'snapshot_only_but_referenced_in_akash':len((own['snapshot']-own['akash']) & refs_by_side['akash']),
               'akash_only_but_referenced_in_snapshot':len((own['akash']-own['snapshot']) & refs_by_side['snapshot'])}
        reasons=[]
        if ledger['difference_ledger_rows']:reasons.append('EFFORT_ROW_MULTISET_DIFFERENCES')
        if any(v['invalid_rows'] for v in sources.values()):reasons.append('INVALID_EFFORT_CELLS_NUMERIC_SUBTOTAL_ONLY')
        if any(v['negative_numeric_rows'] for v in sources.values()):reasons.append('NEGATIVE_NUMERIC_EFFORT_INCLUDED')
        if not receipt['provided']:reasons.append('RUN_PROVENANCE_NOT_PROVIDED')
        elif receipt.get('extractor_exit_code') != 0:reasons.append('EXTRACTION_EXIT_NOT_SUCCESSFUL')
        indicators=receipt.get('log_indicators') or {}
        log_counts={k:(v.get('count',0) if isinstance(v,dict) else v) for k,v in indicators.items()} if isinstance(indicators,dict) else {}
        if any(log_counts.values()):reasons.append('EXTRACTION_LOG_INDICATORS_REQUIRE_REVIEW')
        reconciliation={k:text(v) if isinstance(v,Decimal) else v for k,v in ledger.items()}
        reconciliation.update(raw_delta_minutes=str(rawdelta),raw_delta_hours=str(rawdelta/60),residual_minutes=str(residual),
            bridge='snapshot raw numeric minutes - Akash raw numeric minutes = unmatched snapshot minutes - unmatched Akash minutes')
        summary=dict(status='RAW_ROW_COMPARISON_COMPLETE_REVIEW_REQUIRED' if reasons else 'OBSERVED_EFFORT_ROWS_MATCH',
            validation_complete=False,review_reasons=reasons,metric='effortAllocation_totalEffort in minutes on every physical CSV row',
            context_columns=list(context_cols),sources=sources,run_receipt=receipt,reconciliation=reconciliation,
            log_indicator_counts=log_counts,
            own_tasks=tasks,drilldowns=drill,source_column_differences={'snapshot_only':sorted(set(scols)-set(acols)),
            'akash_only':sorted(set(acols)-set(scols))},comparator_sha256=sha(Path(__file__)),
            finished_utc=datetime.now(timezone.utc).isoformat(),limitations=[
                'Every physical row contributes its numeric effort, including repeated tasks and non-task rows. No deduplication, hierarchy expansion or inferred zero is applied.',
                'Summary hours are rounded to two decimals and drilldown CSV hours to six decimals. All reconciliation uses exact Decimal minutes.',
                'Null effort is distinct from explicit numeric zero. Invalid cells are excluded only from the explicitly labelled numeric subtotal and require review.',
                'Matching uses the listed row-context columns and normalized effort values, not every source column. Other metadata differences are outside this comparison.',
                'IDs retain case; surrounding whitespace and recognized null/PLACEHOLDER identifiers are normalized. Equivalent numeric spellings match; recognized null spellings match.',
                'Duplicate occurrences are canceled by minimum count for the same context and effort. Unmatched rows are never paired speculatively to infer a changed task value.',
                'Source Excel row numbers include the header. Within identical groups, the earliest occurrences are canceled; that is deterministic accounting, not a historical row correspondence.',
                'Direct-container drilldown partitions rows by id. Ancestor project membership can overlap and is not used to explain the additive raw total.',
                'Own-task absence means absent as an own record in the other CSV. A hierarchy reference, an unmatched duplicate, or a changed context does not establish task inaccessibility.',
                'Export, refresh and extraction times are recorded separately. Differences can reflect timing, scope, request failures, flattening, or access; this comparison alone does not prove a cause.',
                'Matching rows and totals do not independently prove extraction completeness or historical deletion dates.'])
        for side,p in (('snapshot',snapshot),('akash',akash)):
            require(sha(p)==sources[side]['sha256'],'Input changed before comparison completed: '+str(p))
        db.close()
        # The database is an intermediate index; only portable CSV/JSON evidence is delivered.
        (out/'row_comparison.sqlite').unlink()
        write_json(out/'summary.json',summary)
        lines=['Raw effort-row comparison (all physical rows; duplicates retained)',
               'Snapshot rows: {:,}; raw numeric hours: {:,.2f}; minutes: {}'.format(sources['snapshot']['physical_rows'],Decimal(sources['snapshot']['raw_numeric_hours']),sources['snapshot']['raw_numeric_minutes']),
               'Akash rows: {:,}; raw numeric hours: {:,.2f}; minutes: {}'.format(sources['akash']['physical_rows'],Decimal(sources['akash']['raw_numeric_hours']),sources['akash']['raw_numeric_minutes']),
               'Snapshot minus Akash raw hours: {:,.2f}; minutes: {}'.format(rawdelta/60,rawdelta),
               'Displayed hours are rounded (summary: 2 decimals; drilldowns: 6). The minute bridge is exact.',
               'Unmatched snapshot / Akash occurrences: {} / {}'.format(ledger['unmatched_snapshot_occurrences'],ledger['unmatched_akash_occurrences']),
               'Unmatched snapshot / Akash numeric minutes: {} / {}'.format(ledger['unmatched_snapshot_numeric_minutes'],ledger['unmatched_akash_numeric_minutes']),
               'Exact minute bridge residual: '+str(residual),
               'Own tasks absent from other CSV (snapshot / Akash): {} / {}'.format(tasks['snapshot_only'],tasks['akash_only']),
               'Invalid effort cells (snapshot / Akash): {} / {}'.format(sources['snapshot']['invalid_rows'],sources['akash']['invalid_rows']),
               'Null effort cells (snapshot / Akash): {} / {}'.format(sources['snapshot']['null_rows'],sources['akash']['null_rows']),
               'Snapshot recorded times: '+compact(sources['snapshot']['observed_time_values'])[:500],
               'Akash recorded refresh times: '+compact(sources['akash']['observed_time_values'])[:500],
               'Extraction window: {} to {}'.format(receipt.get('started_utc','unverified'),receipt.get('finished_utc','unverified')),
               'Candidate log counts: '+compact(log_counts),
               'Review reasons: '+('; '.join(reasons) or 'none detected'),
               'No permission cause or 100% completeness is inferred. See summary.json for capture times and limits.',
               'Output: '+str(out)]
        (out/'summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        write_json(out/'output_hashes.json',{p.name:sha(p) for p in sorted(out.iterdir()) if p.is_file()})
        return summary
    except Exception as exc:
        db.close()
        write_json(out/'summary.json',{'status':'RAW_ROW_COMPARISON_BLOCKED','validation_complete':False,
                   'reason':str(exc),'partial_outputs_not_validated':True})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--akash',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='New output directory; must not already exist')
    parser.add_argument('--run-receipt',type=Path)
    args=parser.parse_args()
    compare(args.snapshot,args.akash,args.output,args.run_receipt)
    print((args.output.expanduser().resolve()/'summary.txt').read_text(encoding='utf-8'),end='')


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError,csv.Error,sqlite3.Error) as exc:
        print('Comparison stopped: '+str(exc));sys.exit(2)
