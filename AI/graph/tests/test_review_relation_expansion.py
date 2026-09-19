"""No silently accepted or source-mismatched relation reviews."""
from pathlib import Path
import json
import sys
import shutil
import uuid
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from review_relation_expansion import merge,sha,digest,canonical


class ReviewCompletenessTests(unittest.TestCase):
    def setUp(self):
        workspace=Path(__file__).resolve().parents[1]
        self.root=workspace/('_relation_review_test_'+uuid.uuid4().hex); self.root.mkdir()
        def cleanup():
            assert self.root.resolve().is_relative_to(workspace) and self.root.name.startswith('_relation_review_test_')
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.source=self.root/'source'; self.source.mkdir()
        self.old={'edge_id':'old','source_article_sha256':'oldsource'}
        self.new={'edge_id':'new','source_article_sha256':'newsource'}
        files={}
        for key,items in [('relationship_evidence',[self.old,self.new]),('new_relationship_evidence',[self.new])]:
            path=self.source/(key+'.jsonl'); path.write_text(''.join(canonical(x)+'\n' for x in items),encoding='utf8')
            files[key]={'path':path.name,'rows':len(items),'sha256':sha(path)}
        (self.source/'manifest.json').write_text(canonical({'artifact_complete':True,'files':files}),encoding='utf8')
        (self.source/'protocol.json').write_text(canonical({'hashes':{'baseline_manifest':'baseline'}}),encoding='utf8')
        self.policy=self.root/'old.json'; self.old_decision={'evidence_id':'old','decision':'review','reason':'identity pending'}
        self.policy.write_text(canonical({'source_manifest_sha256':'baseline','decisions':[self.old_decision]}),encoding='utf8')
        self.review=self.root/'review.jsonl'; self.out=self.root/'merged.json'
        self.decision={'evidence_id':'new','decision':'keep','reason':'explicit actor and role verified',
                       'edge_sha256':digest(self.new),'source_sha256':'newsource','full_article_reviewed':True}

    def test_missing_review_never_defaults_to_keep(self):
        self.review.write_text('',encoding='utf8')
        with self.assertRaisesRegex(AssertionError,'Missing explicit'): merge(self.source,self.policy,[self.review],self.out)
        self.assertFalse(self.out.exists())

    def test_wrong_source_and_incomplete_read_are_rejected(self):
        for key,value in [('source_sha256','other'),('edge_sha256','other'),('full_article_reviewed',False)]:
            self.review.write_text(canonical({**self.decision,key:value})+'\n',encoding='utf8')
            with self.assertRaises(AssertionError): merge(self.source,self.policy,[self.review],self.out)
            self.assertFalse(self.out.exists())

    def test_complete_bound_reviews_preserve_old_defer(self):
        self.review.write_text(canonical(self.decision)+'\n',encoding='utf8')
        result=merge(self.source,self.policy,[self.review],self.out)
        self.assertEqual(next(x for x in result['decisions'] if x['evidence_id']=='old'),self.old_decision)
        self.assertEqual(result['counts'],{'review':1,'keep':1})
        self.assertFalse(result['human_gold'])


if __name__=='__main__': unittest.main()
