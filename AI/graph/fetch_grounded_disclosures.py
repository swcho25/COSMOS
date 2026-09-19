"""Read fixed existing server disclosure files; never write remote/HDFS state.

This is an explicit local transfer receipt, not a claim of complete collection.
Only immutable named input files are copied. Remote SHA before/after streaming
and local SHA must agree; files are never overwritten on mismatch.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import datetime as dt

from transfer_kg_reselection import ssh, HOST

ROOT = '/home/ubuntu/ner-work'
FILES = {
    'recent/disclosure_relationships.jsonl': ROOT+'/lake_recent90_20260916_prepared_v2/disclosure_relationships.jsonl',
    'recent/disclosures.jsonl': ROOT+'/lake_recent90_20260916_prepared_v2/disclosures.jsonl',
    'recent/prepared_manifest.json': ROOT+'/lake_recent90_20260916_prepared_v2/manifest.json',
    'recent/input_manifest.json': ROOT+'/lake_recent90_20260916_inputs_v2/manifest.json',
    'recent/sec.jsonl': ROOT+'/lake_recent90_20260916_inputs_v2/sec.jsonl',
    'historical_ownership.jsonl': ROOT+'/historical_ownership.jsonl',
    'companies.csv': ROOT+'/lake_recent90_20260916_code_v2/companies.csv',
    'aliases.csv': ROOT+'/lake_recent90_20260916_code_v2/aliases.csv',
}

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def remote_digest(path):
    r=ssh('sha256sum -- '+shlex.quote(path),capture_output=True,timeout=120)
    if r.returncode: raise RuntimeError(r.stderr.decode('utf8','replace'))
    return r.stdout.decode('ascii').split()[0]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args(); args.out.mkdir(parents=True,exist_ok=True)
    receipt={'schema':'cosmos-disclosure-local-transfer-1','server':HOST,'read_only_remote':True,
        'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'files':[]}
    for relative,remote in FILES.items():
        expected=remote_digest(remote); target=args.out/relative; target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if digest(target)!=expected: raise ValueError('Existing input differs: '+relative)
        else:
            partial=target.with_suffix(target.suffix+'.part')
            with partial.open('wb') as f:
                r=ssh('cat -- '+shlex.quote(remote),stdout=f,stderr=-1,timeout=180)
            if r.returncode: raise RuntimeError(r.stderr.decode('utf8','replace'))
            if digest(partial)!=expected or remote_digest(remote)!=expected: raise ValueError('Transfer changed: '+relative)
            partial.rename(target)
        receipt['files'].append({'path':relative,'source_path':remote,'sha256':expected,'bytes':target.stat().st_size})
        print(relative,target.stat().st_size,flush=True)
    (args.out/'transfer_receipt.json').write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n',encoding='utf8')

if __name__=='__main__': main()
