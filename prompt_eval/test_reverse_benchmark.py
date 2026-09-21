"""Offline tests for input isolation and literal evidence validation (no API calls)."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import benchmark_reverse_models as b

class ReverseBenchmarkTests(unittest.TestCase):
    def test_education_excludes_later_business_and_generated_answers(self):
        item=b.prepare('E0918')
        for forbidden in ('--- 模型回答 ---','9B','不超纲','ContextController','MemoryManager'):
            self.assertNotIn(forbidden,item['dialogue'])
        self.assertIn('工具调用',item['dialogue'])

    def test_tl_does_not_import_later_candidate_self_assessment(self):
        item=b.prepare('T0918')
        for forbidden in ('正规的PR合并','PR合并','我是比较好奇，为什么会一直觉得'):
            self.assertNotIn(forbidden,item['dialogue'])
        self.assertIn('很难看出',item['dialogue'])

    def test_guangzhou_has_no_later_business_leak(self):
        item=b.prepare('G0916')
        self.assertNotIn('游轮',item['dialogue'])
        self.assertNotIn('订票',item['dialogue'])
        self.assertNotIn('【采集批次 10',item['dialogue'])

    def test_diarized_input_excludes_summary(self):
        a=b.prepare('A0826')
        self.assertNotIn('本次面试围绕',a['dialogue'])
        self.assertNotIn('团队十余人',a['dialogue'])
        self.assertIn('skill',a['dialogue'].lower())

    def test_missing_interviewer_remains_unknown(self):
        item=b.prepare('P0907')
        self.assertEqual(sum(v['speaker']=='interviewer' for v in item['evidence'].values()),0)
        self.assertNotIn('会议概览',item['dialogue'])
        self.assertNotIn('侯晓辰指出',item['dialogue'])

    def test_candidate_cache_statement_not_reassigned(self):
        item=b.prepare('V0902')
        rows=[v for v in item['evidence'].values() if '缓存命中率比较差' in v['text']]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['speaker'],'candidate')
        self.assertNotIn('两百多个人',item['dialogue'])

    def test_quote_and_speaker_are_both_checked(self):
        item={'evidence':{'7':{'text':'短期记忆保留上下文','speaker':'candidate'}}}
        row={'priority':'P1','category':'实践请教','topic':'记忆','question':'怎样保留上下文？','context_reason':'信息保留方法','score':7,
             'evidence':[{'line':7,'quote':'短期记忆','speaker':'interviewer'}]}
        bad=b.inspect_answer(json.dumps([row],ensure_ascii=False),item,False)
        self.assertEqual(bad['evidence_valid'],0)
        self.assertIn('evidence_mismatch',bad['issues'])
        row['evidence'][0]['speaker']='candidate'
        good=b.inspect_answer(json.dumps([row],ensure_ascii=False),item,False)
        self.assertEqual(good['evidence_valid'],1)

    def test_truncated_and_fenced_json_are_not_silently_repaired(self):
        for value in ('[{','```json\n[]\n```','not json'):
            self.assertFalse(b.inspect_answer(value,{'evidence':{}},False)['strict_json'])
        self.assertTrue(b.inspect_answer('[]',{'evidence':{}},False)['schema_valid'])

    def test_input_snapshot_hash_is_stable(self):
        self.assertEqual(b.prepare('E0918')['input_sha256'],b.prepare('E0918')['input_sha256'])

if __name__=='__main__':
    unittest.main()
