import json
import tempfile
import unittest
from pathlib import Path

from pov_novel.gateway import CachedGateway, StructuredOutputError
from pov_novel.mock import MockGateway
from pov_novel.models import Claim, Memory
from pov_novel.semantic_context import compact_private_memory
from pov_novel.store import read_json


class MemoryBudgetTests(unittest.TestCase):
    def memory(self):
        return Memory(recent=[Claim(text=f'亲眼看见第{i}封信', kind='observation', evidence_ids=[f'e{i}']) for i in range(48)])

    def gateway(self, raw):
        class Oversized(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                raise StructuredOutputError('记忆摘要合计最多30项', raw_output=json.dumps(raw, ensure_ascii=False))
        return Oversized()

    def test_host_bounds_verified_oversize_selection_and_preserves_audit(self):
        memory = self.memory()
        original = memory.model_dump()
        raw = {'retained_ids': [f'memory-{i}' for i in range(27)], 'belief_summaries': [
            {'text': f'可能与第{i}封信有关', 'source_memory_ids': [f'memory-{i}'], 'confidence': 'low'} for i in range(11)]}
        with tempfile.TemporaryDirectory() as folder:
            backend = self.gateway(raw)
            gateway = CachedGateway(backend, Path(folder))
            result = compact_private_memory(memory, gateway)
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(len(result.summary), 30)
            self.assertEqual(result.summary[:27], memory.recent[:27])
            self.assertTrue(all(c.kind == 'belief' for c in result.summary[27:]))
            report = read_json(next((Path(folder)/'memory_budget_repairs').glob('*.json')))
            self.assertEqual(report['candidates'], raw)
            self.assertEqual(report['candidate_count'], 38)
            self.assertEqual(report['input_memories'], [claim.model_dump() for claim in memory.recent[:-8]])
            self.assertEqual(memory.model_dump(), original)

    def test_unknown_id_beyond_budget_is_checked_before_selection(self):
        raw = {'retained_ids': [f'memory-{i}' for i in range(30)] + ['unknown'], 'belief_summaries': []}
        gateway = self.gateway(raw)
        with self.assertRaises(ValueError):
            compact_private_memory(self.memory(), gateway)
        self.assertEqual(len(gateway.calls), 3)

    def test_evidence_overflow_beyond_budget_is_not_hidden(self):
        raw = {'retained_ids': [f'memory-{i}' for i in range(30)], 'belief_summaries': [
            {'text': '可能有关', 'source_memory_ids': [f'memory-{i}' for i in range(21)], 'confidence': 'low'}]}
        gateway = self.gateway(raw)
        with self.assertRaises(ValueError):
            compact_private_memory(self.memory(), gateway)
        self.assertEqual(len(gateway.calls), 3)

    def test_malformed_output_keeps_format_error_instead_of_claiming_fact_upgrade(self):
        class Malformed(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                raise StructuredOutputError('invalid memory JSON', raw_output='{')
        gateway = Malformed()
        with self.assertRaises(StructuredOutputError) as caught:
            compact_private_memory(self.memory(), gateway)
        self.assertEqual(len(gateway.calls), 3)
        self.assertIn('invalid memory JSON', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
