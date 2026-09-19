"""No model: relation-specific endpoint identity protections."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from expand_grounded_relations_v3 import endpoint_blocked


class ExpansionIdentityTests(unittest.TestCase):
    def test_parent_review_for_other_target_event_is_not_identity_failure(self):
        pair = {'reasons':['no_grounded_target_economic_candidate'],
                'unresolved_mentions':[{'source':'body','start':0,'end':20,
                                        'reason':'other_company_between_target_and_event'}]}
        self.assertIsNone(endpoint_blocked(pair,'source',{'source_start':3,'source_end':7},{'title':'ab'}))

    def test_collision_blocks_only_the_actual_quote_span(self):
        pair = {'reasons':[], 'rejected_mentions':[{'source':'body','start':0,'end':10,
                    'reason':'explicit_name_collision_or_sports_team'}]}
        self.assertEqual(endpoint_blocked(pair,'source',{'source_start':3,'source_end':7},{'title':'ab'}),
                         'explicit_name_collision_or_sports_team')
        self.assertIsNone(endpoint_blocked(pair,'source',{'source_start':20,'source_end':23},{'title':'ab'}))

    def test_dated_identity_pending_cannot_be_promoted(self):
        pair = {'reasons':['historical_identity_time_unverified']}
        self.assertEqual(endpoint_blocked(pair,'source',{'source_start':3,'source_end':7},{'title':'ab'}),
                         'historical_issuer_identity_pending')

    def test_local_company_group_or_biography_guard_survives(self):
        for reason in ['group_or_local_issuer_attribution_unverified',
                       'historical_name_requires_dated_identity',
                       'past_employment_or_biography_requires_review']:
            pair={'reasons':[],'unresolved_mentions':[{'source':'title','start':0,'end':20,'reason':reason}]}
            self.assertEqual(endpoint_blocked(pair,'target',{'target_start':3,'target_end':7},{'title':'a'*20}),reason)


if __name__=='__main__': unittest.main()
