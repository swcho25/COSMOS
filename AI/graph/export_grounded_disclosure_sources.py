"""Read-only server-side stdout export of narrowly selected DART source records.

No remote file writes. The gzip stream preserves each source JSON object plus
the SHA of its original JSONL line. Completion carries compressed source SHA.
Historical selection is existing cached positive ownership reports only, not
an all-filing census. Recent selection additionally inspects explicit contract,
equity-purchase and merger report families; no relation is inferred here.
"""
import collections
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path('/home/ubuntu/ner-work')

class HashedReader(io.RawIOBase):
    def __init__(self, stream): self.stream=stream; self.sha=hashlib.sha256(); self.count=0
    def readable(self): return True
    def readinto(self,b):
        data=self.stream.read(len(b))
        if not data:return 0
        b[:len(data)]=data;self.sha.update(data);self.count+=len(data);return len(data)

def main(mode):
    count=0; source_receipts=[]
    out=gzip.GzipFile(fileobj=sys.stdout.buffer,mode='wb',mtime=0)
    def emit(obj): out.write((json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf8'))
    if mode=='recent':
        wanted={json.loads(l)['report_id'] for l in (ROOT/'lake_recent90_20260916_prepared_v2/disclosure_relationships.jsonl').open()}
        path=ROOT/'lake_recent90_20260916_inputs_v2/dart.jsonl'; sha=hashlib.sha256();size=0
        with path.open('rb') as stream:
            for line in stream:
                sha.update(line);size+=len(line);row=json.loads(line)
                family=re.search(r'단일판매.?공급계약|타법인.*주식.*증권.*취득|회사합병',row.get('report_nm',''))
                if row.get('rcept_no') not in wanted and not family: continue
                emit({'kind':'source_report','collection':'recent','container_source_path':str(path),
                      'source_line_sha256':hashlib.sha256(line).hexdigest(),'report':row});count+=1
        source_receipts.append({'path':str(path),'sha256':sha.hexdigest(),'bytes':size})
    elif mode=='historical':
        groups=collections.defaultdict(set)
        for line in (ROOT/'historical_ownership.jsonl').open():
            row=json.loads(line)
            if row['positions']:groups[row['source_path']].add(row['report_id'])
        for index,(path,wanted) in enumerate(sorted(groups.items())):
            p=subprocess.Popen(['/opt/hadoop/bin/hdfs','dfs','-cat',path],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            reader=HashedReader(p.stdout); found=set()
            with gzip.GzipFile(fileobj=io.BufferedReader(reader)) as stream:
                for line in stream:
                    row=json.loads(line)
                    if row.get('rcept_no') not in wanted:continue
                    found.add(row['rcept_no'])
                    emit({'kind':'source_report','collection':'historical','container_source_path':path,
                          'source_line_sha256':hashlib.sha256(line).hexdigest(),'report':row});count+=1
            if p.wait()!=0:raise RuntimeError('HDFS read failed: '+path)
            if found!=wanted:raise RuntimeError('Missing historical receipts: '+str(sorted(wanted-found)))
            source_receipts.append({'path':path,'sha256':reader.sha.hexdigest(),'bytes':reader.count,'selected_receipts':sorted(found)})
            print('read historical source',index+1,'/',len(groups),'selected',count,file=sys.stderr,flush=True)
    else: raise ValueError(mode)
    emit({'kind':'completion','schema':'cosmos-disclosure-source-export-1','mode':mode,'reports':count,'sources':source_receipts,
          'remote_writes':False,'all_filings_complete':False})
    out.close()

if __name__=='__main__':main(sys.argv[1])
