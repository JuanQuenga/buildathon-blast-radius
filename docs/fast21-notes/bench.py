import sys,json,subprocess,tempfile,shutil,os
from pathlib import Path
w=Path(__file__).resolve().parent
src=w/'src';cat=w.parent/'starter/sample-kit/sample/tests.json'
sys.path[:0]=[str(src/'tools'),str(src)]
from measure_heavy import FLAGS,WRAPPER,score_lib
from replay_heavy_release import cohorts
from collections import Counter
target_ids={t['id'].rsplit('::',1)[-1]:t['number'] for t in json.loads(cat.read_text())['tests']}
catalog=json.loads(cat.read_text())['tests'];dur={t['number']:round(t['seconds']*1000) for t in catalog}
more=json.load(open(w.parents[1]/'repos/buildathon-blast-radius/eval/cases/dev/answers.json'))['commits']
labels=json.load(open(w.parent/'static15/labels.json'))
image=sys.argv[1]; name=sys.argv[2]; group=sys.argv[3]
out=w/'runs'/name/group;out.mkdir(parents=True,exist_ok=True)
flags=FLAGS+['--cpuset-cpus','4-7,12-15']
if name.startswith('trial'):flags+=['-v',f'{src}/selector:/app/selector:ro']
def batches():
 if group in ('dev','holdout','generated'):
  for k,rows in cohorts(group,Path('/tmp/br8-genpatches'),'860e0bf'):
   if k.endswith('/dev'):
    rows=[(p,set(more[p.stem]['failing']) if p.stem in more and not more[p.stem].get('basis') else fs) for p,fs in rows]
   if k.startswith('extra/'):
    rows=[(p,fs) for p,fs in rows if fs is not None]
   for i in range(0,len(rows),40):
    yield k,rows[i:i+40]
 elif group=='fixtures':
  f=w.parent/'fixture14'
  forbatch=[]
  for p in sorted((f/'patches').glob('*.patch')):
   fs={target_ids[n.rsplit('::',1)[-1]] for n in json.load(open(f/'oracle'/(p.stem+'.json')))[1]}
   forbatch.append((p,fs))
  yield 'fixtures',forbatch
 elif group in ('dual','unsettled','sample'):
  code=(w.parent/('hybrid16/dual-unsettled.py' if group=='unsettled' else 'practice-diagnostics/dual.py')).read_text()
  if group=='sample':
   r=next(rs for k,rs in cohorts('dev',Path('/tmp/br8-genpatches'),'860e0bf') if k=='matched/sample')
  else:
   ns={};os.chdir(src);exec(code[:code.index('print("two passing')],ns);r=[(p,fs) for p,_,fs in ns['batch']]
  yield group,r
 else: raise ValueError(group)
stats={}
for i,(k,rows) in enumerate(batches()):
 vals=stats.setdefault(k,Counter())
 with tempfile.TemporaryDirectory() as tmp:
  d=Path(tmp);d.chmod(0o755)
  for p,_ in rows:shutil.copyfile(p,d/p.name)
  status=(w.parents[1]/'coord/log/cpu.status.json').read_text()
  (out/f'{i}.host').write_text(status)
  r=subprocess.run(['docker','run',*flags,'-v',f'{d}:/data/commits:ro','-v',f'{cat}:/data/tests.json:ro',image,'python','-c',WRAPPER],capture_output=True,timeout=210)
 (out/f'{i}.plans').write_bytes(r.stdout);(out/f'{i}.trace').write_bytes(r.stderr);r.check_returncode()
 result=score_lib.evaluate(r.stdout,catalog,[p.stem for p,_ in rows]);assert result['ok']
 vals['cpu']+=float(r.stderr.splitlines()[-1].split()[1])
 for p,fs in rows:
  workers=result['plans'][p.stem]; tests=set().union(*workers);vals['count']+=1
  if group=='generated':
   ans=labels.get('generated-'+p.stem,[{}])[-1]
   fs={target_ids[n] for g,n in [('ds','test_execute_tpcds'),('parser','test_max_nodes'),('generator','test_generator_memory')] if ans.get(g)=='fail'}
   vals['heavy_failures']+=len(fs)
  if fs is not None:
   vals['labelled']+=1;vals['misses']+=bool(fs-tests);vals['sim']+=score_lib.commit_cost_ms(workers,dur)
   if fs-tests:print('MISSED',k,p,fs-tests,flush=True)
 print(k,i,vals,flush=True)
(out/'stats.json').write_text(json.dumps(stats))
