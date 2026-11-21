import ast,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
pin=root.parent/'sqlglot'
# An override would bypass the hook and invalidate a call-site receipt.
definitions=[]
for p in (pin/'sqlglot').rglob('*.py'):
 for n in ast.walk(ast.parse(p.read_bytes())):
  if getattr(n,'name',None)=='_replace_line_breaks' or (isinstance(n,ast.Attribute) and isinstance(n.ctx,ast.Store) and n.attr=='_replace_line_breaks'):
   definitions.append((p.relative_to(pin).as_posix(),n.lineno))
assert definitions==[('sqlglot/generator.py',5836)],definitions
cat=json.load(open(root.parent/'starter/sample-kit/sample/tests.json'))['tests']
out={}
for d in (root/'strings').iterdir():
 reports=list(d.glob('*.json'))
 assert reports and {p.stem for p in reports}=={p.stem for p in d.glob('*.start')}
 obs=[json.loads(p.read_text()) for p in reports]
 assert 'passed' in (d/'log').read_text()
 if d.name=='test_execute_tpcds': assert sum(v['queries'] for _,v in obs)==99
 ids=[t['id'] for t in cat if t['file'] in {'tests/test_parser.py','tests/test_executor.py'} and t['id'].endswith('::'+d.name)]
 assert len(ids)==1
 for k in {k for keys,_ in obs for k in keys}:
  if all(keys.get(k,True) for keys,_ in obs):
   out.setdefault(k,[]).append(ids[0])
(root/'src/selector/maps/string-calls.json').write_text(json.dumps({k:sorted(v) for k,v in out.items()},sort_keys=True)+'\n')
