#!/usr/bin/env python3
"""Run the unchanged saved CSV-only extractor with Akash's token, then compare raw rows.

Only the existing extractor makes Wrike requests. This launcher does not change
its task fetching, filters, pagination, hierarchy expansion or effort logic.
No independent reference captures or Snowflake writes are performed.
"""
import argparse
import ast
import csv
import difflib
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize

FORBIDDEN_CALLS = {'write_pandas', 'get_snowflake_connection', 'to_sql'}


def check(ok, message):
    if not ok:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(path):
    path = Path(path)
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ino


def sha(path):
    before = fingerprint(path)
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    check(fingerprint(path) == before, 'File changed while being fingerprinted: ' + Path(path).name)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
    temporary.replace(path)


def source_text(path):
    before = fingerprint(path)
    raw = Path(path).read_bytes()
    check(fingerprint(path) == before, 'Source changed during inspection: ' + Path(path).name)
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    text = raw.decode(encoding)
    try:
        tree = ast.parse(text, filename=str(path))
        compile(tree, str(path), 'exec', dont_inherit=True)  # Syntax check only.
    except SyntaxError as error:
        raise ValueError(f'Source syntax error in {Path(path).name}, line {error.lineno}; source values omitted') from None
    return text, tree, hashlib.sha256(raw).hexdigest()


def targets(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for child in node.elts for name in targets(child)]
    return []


def assignment_targets(node):
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in targets(target)]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return targets(node.target)
    return []


def extraction_region(text, tree, heading):
    starts = [node for node in tree.body if isinstance(node, ast.Assign)
              and 'API_VERSION' in assignment_targets(node)]
    boundaries = [token.start[0] for token in tokenize.generate_tokens(io.StringIO(text).readline)
                  if token.type == tokenize.COMMENT and token.start[1] == 0
                  and re.fullmatch(r'#\s*=+\s*' + re.escape(heading) + r'\s*=+\s*', token.string)]
    check(len(starts) == 1 and len(boundaries) == 1,
          f'Expected unique top-level API_VERSION and {heading} boundaries')
    start, end = starts[0].lineno, boundaries[0]
    check(end > start, 'Export heading must follow the extraction block')
    nodes = [node for node in tree.body if start <= node.lineno < end]
    check(nodes and all(node.end_lineno < end for node in nodes), 'Export boundary crosses executable source')
    code = ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False)
    return code, {'first_line': start, 'last_line': end - 1, 'statement_count': len(nodes)}


def hidden_token_check(tree, extraction_start):
    assignments = [node for node in ast.walk(tree) if 'TOKEN' in assignment_targets(node)]
    check(len(assignments) == 1 and assignments[0] in tree.body and isinstance(assignments[0], ast.Assign),
          'Expected exactly one top-level TOKEN assignment using hidden getpass input')
    node = assignments[0]
    check(len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
          and node.lineno < extraction_start,
          'TOKEN input must be the single assignment before the unchanged extraction block')
    call = node.value
    check(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
          and call.func.attr == 'strip' and not call.args and not call.keywords,
          'Expected TOKEN = getpass(...).strip(); no credentials will be supplied by this launcher')
    prompt = call.func.value
    check(isinstance(prompt, ast.Call) and len(prompt.args) <= 1 and not prompt.keywords
          and all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in prompt.args),
          'Hidden token prompt differs from the expected getpass call')
    bindings = set()
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and item.module == 'getpass' and item.level == 0:
            bindings.update(('function', alias.asname or alias.name) for alias in item.names if alias.name == 'getpass')
        elif isinstance(item, ast.Import):
            bindings.update(('module', alias.asname or alias.name) for alias in item.names if alias.name == 'getpass')
    if isinstance(prompt.func, ast.Name):
        valid = ('function', prompt.func.id) in bindings
        binding = prompt.func.id
    elif isinstance(prompt.func, ast.Attribute) and isinstance(prompt.func.value, ast.Name):
        valid = prompt.func.attr == 'getpass' and ('module', prompt.func.value.id) in bindings
        binding = prompt.func.value.id
    else:
        valid, binding = False, ''
    check(valid, 'Token prompt is not bound to the standard-library getpass function')
    rebound = [item for item in ast.walk(tree) if binding in assignment_targets(item)
               or (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == binding)]
    check(not rebound, 'getpass binding is reassigned in the extraction source')
    return node.lineno


def export_tail_check(tree, extraction_end):
    tail = [node for node in tree.body if node.lineno > extraction_end]
    parents = {}
    for statement in tail:
        for node in ast.walk(statement):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
    exports = []
    for statement in tail:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and node.id == 'df_final_complete':
                parent = parents.get(node)
                allowed = isinstance(node.ctx, ast.Load) and (
                    isinstance(parent, ast.Attribute) and parent.value is node and parent.attr in ('empty', 'to_csv')
                    or isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == 'len'
                    and parent.args == [node] and not parent.keywords)
                check(allowed, 'Export tail modifies, aliases or filters df_final_complete; no extraction started')
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'df_final_complete'):
                check(node.func.attr == 'to_csv', 'Unexpected dataframe method in export tail')
                exports.append(node)
    check(len(exports) == 1, 'Expected exactly one direct df_final_complete.to_csv export')
    export = exports[0]
    keys = [keyword.arg for keyword in export.keywords]
    check(len(export.args) == 1 and not isinstance(export.args[0], ast.Starred)
          and len(keys) == len(set(keys)) and set(keys) <= {'index', 'encoding'},
          'CSV export must preserve all columns and values; unexpected positional/keyword export options')
    index = next((keyword.value for keyword in export.keywords if keyword.arg == 'index'), None)
    check(isinstance(index, ast.Constant) and index.value is False, 'CSV export must explicitly use index=False')
    return export.lineno


def preflight(source, backup, snapshot):
    check(source != backup and source != snapshot and backup != snapshot, 'Source, backup and snapshot must be different files')
    local_text, local_tree, local_hash = source_text(source)
    backup_text, backup_tree, backup_hash = source_text(backup)
    local_code, local_bounds = extraction_region(local_text, local_tree, 'LOCAL CSV EXPORT')
    backup_code, backup_bounds = extraction_region(backup_text, backup_tree, 'SNOWFLAKE UPLOAD')
    check(local_code == backup_code, 'Extraction block differs from the saved backup. No extraction was started; review the source differences')
    token_line = hidden_token_check(local_tree, local_bounds['first_line'])
    calls = []
    for node in ast.walk(local_tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
            if name in FORBIDDEN_CALLS:
                calls.append((node.lineno, name))
    check(not calls, 'Unexpected database call remains in the local source; no extraction started')
    export_line = export_tail_check(local_tree, local_bounds['last_line'])
    def describe(node):
        value = {'statement_type': type(node).__name__, 'first_line': node.lineno, 'last_line': node.end_lineno}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            value['names'] = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value['names'] = assignment_targets(node)
        elif isinstance(node, ast.Import):
            value['names'] = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            value['module'] = node.module
            value['names'] = [alias.name for alias in node.names]
        return value
    old = [ast.dump(node, include_attributes=False) for node in backup_tree.body]
    new = [ast.dump(node, include_attributes=False) for node in local_tree.body]
    changes = []
    for operation, a, b, c, d in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if operation != 'equal':
            changes.append({'operation': operation, 'backup_statements': [describe(n) for n in backup_tree.body[a:b]],
                            'local_statements': [describe(n) for n in local_tree.body[c:d]]})
    return {'result': 'SAVED_EXTRACTION_BLOCK_MATCH', 'local_block': local_bounds,
            'backup_block': backup_bounds, 'token_prompt_line': token_line, 'csv_export_line': export_line,
            'top_level_changed_blocks': changes,
            'source_path': str(source), 'source_sha256': local_hash,
            'source_modified_utc': datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(),
            'backup_path': str(backup), 'backup_sha256': backup_hash,
            'backup_modified_utc': datetime.fromtimestamp(backup.stat().st_mtime, timezone.utc).isoformat(),
            'snapshot_path': str(snapshot), 'snapshot_sha256': sha(snapshot),
            'note': 'A match to this saved backup does not prove the current deployed RIO commit. Only the marked extraction block is compared.'}


def new_export(existing, directory):
    created = {p.resolve() for p in directory.glob('*/wrike_local_full.csv')} - existing
    check(len(created) == 1, f'Expected exactly one new completed wrike_local_full.csv; found {len(created)}')
    path = created.pop()
    digest = sha(path)
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames or []
        normalized = [name.strip().lower() for name in headers]
        check(headers and len(normalized) == len(set(normalized)) and '' not in normalized,
              'New export has blank or duplicate column headers')
        check({'key', 'effortallocation_totaleffort'} <= set(normalized), 'New export lacks expected identity/effort columns')
        count = 0
        for row in reader:
            count += 1
            check(None not in row and all(value is not None for value in row.values()), 'New CSV contains a malformed row')
    check(count > 0 and sha(path) == digest, 'New CSV is empty or changed during inspection')
    return {'path': str(path), 'sha256': digest, 'bytes': path.stat().st_size,
            'physical_rows': count, 'column_count': len(headers), 'new_file_observed': True}


LOG_PATTERNS = {
    'traceback': re.compile(r'Traceback \(most recent call last\)', re.I),
    'request_error': re.compile(r'\b(?:HTTPError|ConnectionError|TimeoutError|ReadTimeout|ConnectTimeout)\b', re.I),
    'request_failure': re.compile(r'\b(?:failed|failure|exhausted)\b', re.I),
    'http_error_code': re.compile(r'\b(?:HTTP(?: status)?|status(?: code)?)\s*[:=]?\s*[45]\d\d\b', re.I),
}


def run_logged(command, cwd, log_path):
    indicators = {name: {'count': 0, 'first_line_numbers': []} for name in LOG_PATTERNS}
    with log_path.open('x', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for number, line in enumerate(process.stdout, 1):
                print(line, end='', flush=True)
                log.write(line)
                log.flush()
                for name, pattern in LOG_PATTERNS.items():
                    if pattern.search(line):
                        item = indicators[name]
                        item['count'] += 1
                        if len(item['first_line_numbers']) < 20:
                            item['first_line_numbers'].append(number)
            return process.wait(), indicators
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            process.stdout.close()


def run(root, source, backup, snapshot, comparator, check_only=False):
    root, source, backup, snapshot, comparator = [Path(p).resolve() for p in (root, source, backup, snapshot, comparator)]
    audit = preflight(source, backup, snapshot)
    print('Saved extraction block: MATCH', flush=True)
    print(f"Local source lines {audit['local_block']['first_line']}–{audit['local_block']['last_line']}; backup lines {audit['backup_block']['first_line']}–{audit['backup_block']['last_line']}", flush=True)
    print('Token input: existing hidden getpass prompt. Database upload calls: none detected.', flush=True)
    print('Local source:', source, '| SHA256:', audit['source_sha256'], '| Modified UTC:', audit['source_modified_utc'], flush=True)
    print('Saved backup:', backup, '| SHA256:', audit['backup_sha256'], '| Modified UTC:', audit['backup_modified_utc'], flush=True)
    print('Top-level changed blocks:', len(audit['top_level_changed_blocks']), flush=True)
    def short_statements(items):
        labels = [item['statement_type'] + (' ' + '/'.join(item.get('names', [])[:6]) if item.get('names') else '') for item in items[:6]]
        if len(items) > 6:
            labels.append(f'+{len(items) - 6} more statements')
        return ', '.join(labels)
    for index, block in enumerate(audit['top_level_changed_blocks'][:10], 1):
        old = short_statements(block['backup_statements'])
        new = short_statements(block['local_statements'])
        print(f'  {index}. {block["operation"]}: backup [{old}] -> local [{new}]', flush=True)
    if len(audit['top_level_changed_blocks']) > 10:
        print(f"  +{len(audit['top_level_changed_blocks']) - 10} more changed blocks; full descriptions saved in the audit", flush=True)
    print(audit['note'], flush=True)
    if check_only:
        output = root / 'output' / 'raw_token_comparison' / ('check_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
        output.mkdir(parents=True, exist_ok=False)
        audit_path = output / 'preflight.json'
        save(audit_path, {'status': 'OFFLINE_SOURCE_CHECK_COMPLETE', 'created_utc': now(),
                         'launcher_sha256': sha(Path(__file__)), **audit})
        print('OFFLINE CHECK COMPLETE. The extractor was not run.', flush=True)
        print('Preflight audit:', audit_path, flush=True)
        return 0
    check(comparator.is_file(), 'compare_wrike_raw_rows.py must be beside this launcher before starting extraction')
    comparator_hash = sha(comparator)
    output_base = source.parent / 'output' / 'local_validation'
    existing = {p.resolve() for p in output_base.glob('*/wrike_local_full.csv')}
    output = root / 'output' / 'raw_token_comparison' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True, exist_ok=False)
    receipt_path = output / 'run_receipt.json'
    receipt = {'status': 'EXTRACTION_RUNNING', 'label': 'akash', **audit,
               'source_comparison': audit, 'launcher_sha256': sha(Path(__file__)),
               'comparator_path': str(comparator), 'comparator_sha256': comparator_hash,
               'started_utc': now(), 'validation_complete': False,
               'token_identity': 'User supplies Akash token at the extractor prompt; identity not independently verified',
               'comparison_basis': 'Every physical CSV row; no deduplication, project filtering or replacement extraction logic',
               'log_review_note': 'Exit code zero and no matching log indicators do not prove every internal API request succeeded.'}
    save(receipt_path, receipt)
    print('Starting the existing CSV-only extractor. Enter AKASH\'S token at its hidden prompt.', flush=True)
    print('No independent before/after reference capture will run.', flush=True)
    print('Run evidence:', output, flush=True)
    result = 2
    try:
        check(sha(source) == audit['source_sha256'] and sha(snapshot) == audit['snapshot_sha256']
              and sha(backup) == audit['backup_sha256'], 'Source, backup or frozen snapshot changed after preflight')
        code, indicators = run_logged([sys.executable, '-u', str(source)], source.parent, output / 'extractor.log')
        receipt.update(extractor_exit_code=code, finished_utc=now(), log_indicators=indicators,
                       extractor_log_sha256=sha(output / 'extractor.log'))
        check(code == 0, f'Extractor exited with code {code}; raw comparison was not started')
        check(sha(source) == audit['source_sha256'], 'Extraction source changed during the run')
        check(sha(snapshot) == audit['snapshot_sha256'], 'Frozen Snowflake snapshot changed during the run')
        check(sha(backup) == audit['backup_sha256'], 'Saved production backup changed during the run')
        receipt['csv'] = new_export(existing, output_base)
        receipt['status'] = 'EXTRACTION_RECORDED_NOT_COMPARED'
        save(receipt_path, receipt)
        extraction_receipt = output / 'extraction_receipt.json'
        save(extraction_receipt, receipt)
        receipt['extraction_receipt_path'] = str(extraction_receipt)
        receipt['extraction_receipt_sha256'] = sha(extraction_receipt)
        print('Fresh raw export:', receipt['csv']['path'], flush=True)
        print('Potential log failure indicators:', sum(item['count'] for item in indicators.values()), flush=True)
        print(receipt['log_review_note'], flush=True)
        check(sha(comparator) == comparator_hash, 'Offline comparison helper changed during extraction')
        compare_code, _ = run_logged([sys.executable, '-u', str(comparator), '--snapshot', str(snapshot),
            '--akash', receipt['csv']['path'], '--output', str(output / 'comparison'),
            '--run-receipt', str(extraction_receipt)], source.parent, output / 'comparison.log')
        receipt['comparison_exit_code'] = compare_code
        receipt['comparison_log_sha256'] = sha(output / 'comparison.log')
        receipt['comparison_output'] = str(output / 'comparison')
        receipt['status'] = 'RAW_COMPARISON_COMPLETE_REVIEW_REQUIRED' if compare_code == 0 else 'RAW_COMPARISON_REQUIRES_REVIEW'
        result = 0 if compare_code == 0 else 3
    except BaseException as error:
        receipt['status'] = 'RUN_INCOMPLETE'
        receipt['error_type'] = type(error).__name__
        receipt['reason'] = str(error) if type(error) is ValueError else type(error).__name__
        receipt.setdefault('finished_utc', now())
        if (output / 'extractor.log').is_file():
            receipt['extractor_log_sha256'] = sha(output / 'extractor.log')
        print('Run stopped:', receipt['reason'], flush=True)
        result = 130 if isinstance(error, KeyboardInterrupt) else 2
    finally:
        receipt['launcher_finished_utc'] = now()
        save(receipt_path, receipt)
    print('Status:', receipt['status'], flush=True)
    print('Receipt:', receipt_path, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    cwd, script_root = Path.cwd(), Path(__file__).resolve().parent
    root = cwd if (cwd / 'Wrike_Data_local_validation.py').is_file() else script_root
    source = args.source or root / 'Wrike_Data_local_validation.py'
    backup = args.backup or root / 'Wrike_Data_old_backup.py'
    snapshot = args.snapshot or root / 'snowflake_snapshot.csv'
    return run(root, source, backup, snapshot, script_root / 'compare_wrike_raw_rows.py', args.check_only)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, UnicodeError, LookupError, tokenize.TokenError) as error:
        reason = str(error) if type(error) is ValueError else (type(error).__name__ + ': ' + str(error.filename) if isinstance(error, OSError) and error.filename else type(error).__name__)
        raise SystemExit('PREFLIGHT STOPPED: ' + reason)
