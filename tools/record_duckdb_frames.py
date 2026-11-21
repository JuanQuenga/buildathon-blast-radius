import gzip,pickle,sys
from pathlib import Path
sys.path.insert(0,'/app/sqlglot')
from tests.test_executor import TestExecutor
test=TestExecutor('test_execute_tpcds')
test.setUpClass()
try:
    frames={i:test.cached_execute(sql,tpch=False) for i,(meta,sql,_) in enumerate(test.tpcds_sqls) if meta.get('execute')}
    assert len(frames)==57
    Path(sys.argv[1]).write_bytes(gzip.compress(pickle.dumps(frames, protocol=4),mtime=0))
finally:
    test.tearDownClass()
