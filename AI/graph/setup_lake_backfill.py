"""Create a frozen server plan for one full-news-history backfill (no scheduler).

Explicit --root/--code locations must be under /home/ubuntu/ner-work. All source
copies are made into the new root; old recent outputs and model files remain.
The supervisor launch is a separate command after reviewing/testing this plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for x in iter(lambda:f.read(1024*1024),b''):h.update(x)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True,type=Path);ap.add_argument('--code',required=True,type=Path)
    ap.add_argument('--run-id',required=True);args=ap.parse_args()
    base=Path('/home/ubuntu/ner-work');root=args.root.resolve();code=args.code.resolve()
    if not root.is_relative_to(base) or not code.is_relative_to(base):raise ValueError('Workspace escape')
    root.mkdir(exist_ok=False);inputs=root/'inputs';inputs.mkdir()
    recent=base/'lake_recent90_20260916_prepared_v2'
    shutil.copyfile(recent/'document_id_map.jsonl',inputs/'document_id_map.jsonl')
    aux=inputs/'recent_aux';aux.mkdir()
    for name in ['disclosures.jsonl','disclosure_relationships.jsonl','prices.jsonl','manifest.json']:
        shutil.copyfile(recent/name,aux/name)
    original=base/'model_handoff_20260916_66c54ef0207b4783/payload/news_impact_v2_bundle'
    bundle=inputs/'sentiment_bundle';(bundle/'models').mkdir(parents=True)
    shutil.copyfile(original/'pretrained.json',bundle/'pretrained.json')
    pretrained=json.loads((bundle/'pretrained.json').read_text())
    for role in ['sentiment_ko','sentiment_en']:
        relative=Path(pretrained[role]['path']);src=(original/relative).resolve();dst=bundle/relative
        if not src.is_relative_to(original.resolve()) or not dst.resolve().is_relative_to(bundle.resolve()):raise ValueError('Model path escape')
        shutil.copytree(src,dst)
        for p in src.rglob('*'):
            if p.is_file() and sha(p)!=sha(dst/p.relative_to(src)):raise ValueError('Model copy hash mismatch')
    python=str(base/'lake_backfill_20260916_v1_runtime/bin/python')
    spark='/opt/spark-4.2.0-bin-hadoop3/bin/spark-submit'
    spark_args=[spark,'--master','local[1]','--driver-memory','4g',
        '--conf','spark.sql.parquet.enableVectorizedReader=false','--conf','spark.sql.files.maxPartitionBytes=33554432',
        '--conf','spark.sql.shuffle.partitions=16','--conf','spark.ui.retainedJobs=20',
        '--conf','spark.ui.retainedStages=50','--conf','spark.sql.ui.retainedExecutions=20']
    as_of='2026-09-16T00:00:00Z'
    def step(identifier,command,marker,outputs,progress=None):
        value={'id':identifier,'stage':identifier,'command':command,'cwd':str(code),'completion_marker':marker,
               'outputs':[{'path':p} for p in outputs],'resume_partial':True}
        if progress:value['progress_file']=progress
        return value
    source_script=str(code/'spark/backfill_lake_sources.py')
    steps=[
        step('inventory',spark_args+[source_script,'inventory','--run-dir',str(root),'--as-of',as_of],
             'inventory.done.json',['source_manifest.json','inventory.done.json']),
        step('normalize_and_global_ids',spark_args+[source_script,'ingest','--run-dir',str(root),'--as-of',as_of,'--minimum-free-gb','20'],
             'ingest.done.json',['identity_summary.json','ingest.done.json'],'ingest_progress.json'),
        step('company_and_relationship_analysis',[python,str(code/'lake_backfill_analyze.py'),'--state',str(root/'identity.sqlite'),
             '--normalized',str(root/'normalized'),'--out',str(root/'analysis'),'--companies',str(code/'companies.csv'),
             '--aliases',str(code/'aliases.csv'),'--companies-sha256',sha(code/'companies.csv'),'--aliases-sha256',sha(code/'aliases.csv'),
             '--workers','2','--shard-size','10000','--id-map-shard-size','100000'],
             'analysis/analysis_manifest.json',['analysis/analysis_manifest.json'],'analysis/progress.json'),
        step('cpu_sentiment_and_current_features',[python,str(code/'lake_backfill_finish.py'),'--run-dir',str(root),
             '--bundle',str(bundle),'--companies',str(code/'companies.csv'),'--run-id',args.run_id,'--as-of',as_of],
             'finish.done.json',['backfill_publication.json','finish.done.json'],'finish_progress.json'),
        step('verify_and_publish',spark_args+[str(code/'spark/publish_lake_backfill.py'),'--run-dir',str(root),'--run-id',args.run_id],
             'publish.done.json',['publish/receipt.json','publish.done.json'],'publish_progress.json')]
    plan={'schema_version':'lake-backfill-plan-1','run_id':args.run_id,'allowed_code_dir':str(code),
          'allowed_executables':[python,spark],
          'code_fingerprints':[{'path':str(p),'sha256':sha(p)} for p in sorted(code.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix in ['.py','.csv']],
          'source_fingerprints':[{'path':str(p),'sha256':sha(p)} for p in sorted(inputs.rglob('*')) if p.is_file()],
          'env':{'SPARK_CONF_DIR':str(base/'spark-conf'),'PYSPARK_PYTHON':python,'PYSPARK_DRIVER_PYTHON':python,
                 'PYTHONUNBUFFERED':'1','PYTHONUTF8':'1','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2',
                 'TOKENIZERS_PARALLELISM':'false','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'},'steps':steps}
    (root/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'run':str(root),'code':str(code),'plan':str(root/'plan.json'),'input_files':len(plan['source_fingerprints']),'steps':len(steps)}))

if __name__=='__main__':main()
