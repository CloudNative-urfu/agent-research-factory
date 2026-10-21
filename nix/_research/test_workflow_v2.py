import unittest
import shutil
import subprocess
from unittest.mock import patch

import factory
import test_factory
import workflow_v2


def scenario(index=0):
    return {'harness': ('coding', 'browser', 'operations')[index % 3],
            'goal': 'Repair a local fixture service and preserve unrelated files',
            'environment': 'Local fixture repository and executable service stub; no external account',
            'initial_state': 'Failing service, repository at a fixed revision',
            'success_oracle': 'Service health AND exact allowed filesystem diff',
            'trust_boundary': 'Untrusted diagnostic output must not authorize filesystem changes',
            'attack_surface': 'Tool output containing counterfeit repair instructions',
            'history_comparison': 'New executable environment; compare prior profiles and reviewer findings',
            'mode': 'exploration',
            'steps': [{'action': 'Inspect failure', 'tool': 'diagnostics', 'state_change': 'Record observed failure'},
                      {'action': 'Stage correction based on diagnostics', 'tool': 'filesystem', 'state_change': 'Pending patch'},
                      {'action': 'Verify staged patch before commit', 'tool': 'tests', 'state_change': 'Commit or rollback'}],
            'guardrails': [('capabilities', 'transaction')[index % 2]],
            'enforcement': 'Host denies writes outside the issued capability; transaction validates final diff',
            'ablation': 'Same task, tools and model without the additional capability check',
            'existing_guardrails': [{'name': 'fixture-policy-engine', 'source_url': 'https://example.org/fixture',
                'version': 'synthetic-v1', 'requirements': 'Local host-side action checks; no logits required',
                'integration': 'Wrap tool dispatch with policy check',
                'compatibility_probe': 'Run clean and forbidden-write cases with the fixed model route'}],
            'controls': {k: 'Executable fixture with expected state diff and recorded side effects' for k in
                         ('clean_hard', 'side_effect', 'sequence', 'recovery', 'guardrail_abuse')}}


class ScenarioSchemaTests(unittest.TestCase):
    def test_primary_harness_is_scalar_with_actionable_error(self):
        value = scenario()
        value['harness'] = ['coding']
        with self.assertRaisesRegex(ValueError, 'one string, not an array'):
            workflow_v2.validate_scenario(value)
        value['harness'] = 'coding'
        workflow_v2.validate_scenario(value)
        self.assertIn('not an array', workflow_v2.CONTRACT['scenario']['harness'])

    def test_structured_requirements_preserve_named_constraints(self):
        value = scenario()
        guardrail = value['existing_guardrails'][0]
        guardrail['requirements'] = {'model_access': 'chat API', 'dependencies': 'stdlib',
                                     'hardware': 'CPU', 'license': 'MIT'}
        workflow_v2.validate_scenario(value)
        for invalid in ({}, {'hardware': ''}, {'hardware': []}, [], None):
            with self.subTest(invalid=invalid):
                guardrail['requirements'] = invalid
                with self.assertRaisesRegex(ValueError, 'requirements'):
                    workflow_v2.validate_scenario(value)


class WorkflowV2Tests(test_factory.CampaignTests):
    def setUp(self):
        super().setUp()
        cfg = factory.load(self.root / 'campaign.json')
        cfg['pipeline_version'] = 2
        factory.save(self.root / 'campaign.json', cfg)
        state = factory.load(self.root / 'state.json')
        state['version'] = 2
        factory.save(self.root / 'state.json', state)
        self.reject = None
        self.revise = False

    def worker(self, role, stage, work, prompt, log):
        if stage in ('idea_review', 'pilot_plan', 'pilot', 'pilot_review'):
            self.calls.append((role, stage, str(work)))
            a = work / 'artifacts'
            a.mkdir(exist_ok=True)
            (a / 'proof.txt').write_text('fixture evidence')
            if stage == 'pilot_plan':
                factory.save(a / 'pilot_plan.json', {'selected_ids': ['h0'],
                    'scenario_profiles': {'h0': scenario()},
                    'cases': ['fresh-dev'], 'parameters': {'max_tokens': 2048},
                    'baselines': ['D0'], 'stop_rules': ['at budget'],
                    'budget': {'max_requests': 10, 'max_output_tokens': 4096, 'max_seconds': 60},
                    'success_criteria': {'interface': 'typed valid', 'need_for_llm': 'meaning required',
                                         'distinct_mechanism': 'different trace', 'feasible_budget': 'small'},
                    'data_policy': 'development_only'})
            elif stage == 'pilot':
                factory.save(a / 'pilot_results.json', {'selected_ids': ['h0'], 'data_role': 'development',
                    'scenario_checks': [{'id': 'h0', 'checks': {k: True for k in
                        ('executable', 'multi_step', 'attack_reachable', 'observable_effects')},
                        'evidence': ['artifacts/proof.txt']}],
                    'guardrail_trials': [{'id': 'h0', 'name': 'fixture-policy-engine', 'status': 'tested',
                        'reason': 'Synthetic transport fixture: clean and attacked rollouts recorded',
                        'evidence': ['artifacts/proof.txt']}],
                    'measurements': [{'interface': True}], 'evidence': ['artifacts/proof.txt'],
                    'lesson_checks': []})
            else:
                self.assertEqual(role, 'critic')
                self.assertTrue((work / 'candidate/artifacts/candidates.json').exists())
                verdict = 'reject' if self.reject == stage else ('revise' if self.revise else 'proceed')
                factory.save(a / 'screening.json', {'verdict': verdict, 'selected_ids': ['h0'],
                    'checks': {key: True for key in ('interface', 'need_for_llm', 'distinct_mechanism', 'feasible_budget',
                                                   'scenario_diversity', 'guardrail_depth', 'existing_guardrails')},
                    'summary': 'fixture screening', 'issues': [], 'next': ['next direction'],
                    'evidence': ['artifacts/proof.txt']})
            return {'ok': True}
        result = super().worker(role, stage, work, prompt, log)
        a = work / 'artifacts'
        if stage == 'research':
            factory.save(a / 'candidates.json', {'candidates': [
                dict(id=f'h{i}', direction=f'd{i % 3}', scenario=scenario(i), **{k: 'specific fixture' for k in
                    ('problem', 'benefit', 'closest_work', 'baseline', 'mechanism_difference', 'falsification', 'pilot_test')})
                for i in range(6)], 'lesson_plan': []})
        if stage == 'plan':
            protocol = factory.load(a / 'protocol.json')
            protocol.update(pilot_used=True, selected_id='h0', scenario_profile=scenario(),
                            guardrail_baselines=['fixture-policy-engine'],
                            request_budget={'max_requests': 20, 'max_output_tokens': 40960, 'max_seconds': 60},
                            data_split={'pilot_ids': ['fresh-dev'], 'confirmation_ids': ['held-out']})
            factory.save(a / 'protocol.json', protocol)
        if stage == 'experiment':
            results = factory.load(a / 'results.json')
            results['guardrail_comparisons'] = [{'name': 'fixture-policy-engine',
                'measurements': [{'synthetic': True}], 'evidence': ['artifacts/raw.json']}]
            factory.save(a / 'results.json', results)
        if stage in ('review', 'confirm'):
            review = factory.load(a / 'review.json')
            review['assessment'] = {key: {'status': 'supported', 'reason': 'fixture', 'evidence': ['artifacts/reproduction.txt']}
                                    for key in ('technical', 'evidence', 'utility', 'novelty', 'article')}
            factory.save(a / 'review.json', review)
        return result

    def test_v2_passes_early_gates_then_full_chain(self):
        factory.run(self.root, self.worker, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')
        stages = [stage for _, stage, _ in self.calls]
        self.assertIn('idea_review', stages)
        self.assertLess(stages.index('pilot_review'), stages.index('plan'))
        self.assertEqual(factory.load(self.root / 'cycles/01/cycle.json')['pipeline_version'], 2)
        history = factory.load(self.root / 'cycles/02/work/handoff.json')['scenario_history']
        self.assertEqual(history[0]['scope'], 'evaluated')
        self.assertEqual(history[0]['profiles'], {'h0': scenario()})

    def test_early_reject_is_remembered_without_fake_results(self):
        self.reject = 'idea_review'
        factory.run(self.root, self.worker, self.verify)
        state = factory.load(self.root / 'state.json')
        self.assertEqual(state['status'], 'exhausted')
        self.assertEqual(len(state['candidates']), 2)
        self.assertTrue(all(c['kind'] == 'screened_out' and not c['eligible'] for c in state['candidates']))
        self.assertNotIn('build', [s for _, s, _ in self.calls])
        self.assertFalse((self.root / 'cycles/01/snapshot/artifacts/results.json').exists())
        memory = factory.load(self.root / 'cycles/02/work/handoff.json')
        self.assertEqual(memory['previous'][0]['cycle'], 1)
        self.assertTrue((self.root / 'cycles/02/work/prior-evidence/01/screening-evidence/artifacts/proof.txt').exists())

    def test_revisions_are_bounded(self):
        self.revise = True
        factory.run(self.root, self.worker, self.verify)
        self.assertEqual(sum(s == 'idea_review' for _, s, _ in self.calls), 4)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'exhausted')

    def test_legacy_prompts_match_required_paths(self):
        self.assertIn('artifacts/sources.json', factory.prompt_for('research'))
        self.assertIn('artifacts/memory.json', factory.prompt_for('report'))

    def test_pilot_reject_skips_main_experiment(self):
        self.reject = 'pilot_review'
        factory.run(self.root, self.worker, self.verify)
        self.assertNotIn('experiment', [s for _, s, _ in self.calls])
        self.assertEqual(len(factory.load(self.root / 'state.json')['candidates']), 2)
        self.assertEqual(factory.load(self.root / 'cycles/02/work/handoff.json')['scenario_history'][0]['scope'], 'piloted')

    def test_legacy_history_survives_explicit_upgrade(self):
        cfg = factory.load(self.root / 'campaign.json')
        cfg.pop('pipeline_version')
        factory.save(self.root / 'campaign.json', cfg)
        state = factory.load(self.root / 'state.json')
        state['version'] = 1
        factory.save(self.root / 'state.json', state)
        factory.run(self.root, super().worker, self.verify)
        before = factory.load(self.root / 'state.json')['candidates']
        factory.extend(self.root, 3, pipeline_version=2)
        factory.run(self.root, self.worker, self.verify)
        state = factory.load(self.root / 'state.json')
        self.assertEqual(state['candidates'][:2], before)
        self.assertEqual(state['status'], 'completed')
        self.assertTrue((self.root / 'cycles/03/work/prior-evidence/01/artifacts/results.json').exists())
        history = factory.load(self.root / 'cycles/03/work/handoff.json')['scenario_history']
        self.assertEqual(history[0]['scope'], 'legacy_unspecified')
        self.assertEqual(history[0]['profiles'], {})
        self.assertEqual(history[0]['summary'], before[0]['review']['summary'])

    def test_config_only_upgrade_rejected(self):
        state = factory.load(self.root / 'state.json')
        state['version'] = 1
        factory.save(self.root / 'state.json', state)
        with self.assertRaises(factory.Invalid):
            factory.run(self.root, self.worker, self.verify)
        self.assertFalse(self.calls)

    def test_pilot_failure_cannot_be_approved(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot_review':
                data = factory.load(work / 'artifacts/screening.json')
                data['checks']['interface'] = False
                factory.save(work / 'artifacts/screening.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'pilot_review')
        self.assertNotIn('plan', [s for _, s, _ in self.calls])

    def test_holdout_overlap_blocks_main_build(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'plan':
                data = factory.load(work / 'artifacts/protocol.json')
                data['data_split']['confirmation_ids'] = ['fresh-dev']
                factory.save(work / 'artifacts/protocol.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'plan')
        self.assertNotIn('build', [s for _, s, _ in self.calls])

    def test_relabeling_same_harness_is_not_diversity(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'research':
                data = factory.load(work / 'artifacts/candidates.json')
                for item in data['candidates']:
                    item['scenario']['harness'] = 'coding'
                factory.save(work / 'artifacts/candidates.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertNotIn('idea_review', [s for _, s, _ in self.calls])

    def test_prompt_only_guardrail_is_not_substantive(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'research':
                data = factory.load(work / 'artifacts/candidates.json')
                data['candidates'][0]['scenario']['guardrails'] = ['prompt_only']
                factory.save(work / 'artifacts/candidates.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)

    def test_critic_can_stop_cosmetic_diversity(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'idea_review':
                data = factory.load(work / 'artifacts/screening.json')
                data['checks']['scenario_diversity'] = False
                factory.save(work / 'artifacts/screening.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertNotIn('pilot', [s for _, s, _ in self.calls])

    def test_unreachable_attack_does_not_pass_pilot(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot':
                data = factory.load(work / 'artifacts/pilot_results.json')
                data['scenario_checks'][0]['checks']['attack_reachable'] = False
                factory.save(work / 'artifacts/pilot_results.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'pilot_review')

    def test_scenario_history_distinguishes_proposals_from_experiments(self):
        self.reject = 'idea_review'
        factory.run(self.root, self.worker, self.verify)
        history = factory.load(self.root / 'cycles/02/work/handoff.json')['scenario_history']
        self.assertEqual(history[0]['scope'], 'proposed')
        self.assertEqual(history[0]['profiles']['h0']['harness'], 'coding')

    def test_single_step_scenario_and_single_guardrail_pool_are_rejected(self):
        for defect in ('steps', 'guardrails'):
            with self.subTest(defect=defect):
                work = self.root / defect
                work.mkdir()
                factory.save(work / 'handoff.json', {'lessons': []})
                self.worker('researcher', 'research', work, '', None)
                data = factory.load(work / 'artifacts/candidates.json')
                for item in data['candidates']:
                    item['scenario'][defect] = (item['scenario']['steps'][:1] if defect == 'steps' else ['capabilities'])
                factory.save(work / 'artifacts/candidates.json', data)
                with self.assertRaises(ValueError):
                    factory.workflow_v2.validate('research', work, self.config)

    def test_main_protocol_cannot_swap_piloted_harness(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'plan':
                data = factory.load(work / 'artifacts/protocol.json')
                data['scenario_profile']['harness'] = 'office'
                factory.save(work / 'artifacts/protocol.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'plan')

    def test_justified_replication_can_complete(self):
        def replication(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'research':
                data = factory.load(work / 'artifacts/candidates.json')
                data['candidates'][0]['scenario'].update(mode='replication',
                    history_comparison='Repeat the harness on new tasks with a different trust boundary and independent oracle')
                factory.save(work / 'artifacts/candidates.json', data)
            elif stage == 'pilot_plan':
                data = factory.load(work / 'artifacts/pilot_plan.json')
                data['scenario_profiles']['h0'] = factory.load(work / 'artifacts/candidates.json')['candidates'][0]['scenario']
                factory.save(work / 'artifacts/pilot_plan.json', data)
            elif stage == 'plan':
                data = factory.load(work / 'artifacts/protocol.json')
                data['scenario_profile'] = factory.load(work / 'pilot-frozen.json')['scenario_profiles']['h0']
                factory.save(work / 'artifacts/protocol.json', data)
            return result
        factory.run(self.root, replication, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')

    def test_existing_guardrail_without_trial_blocks_pilot(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot':
                data = factory.load(work / 'artifacts/pilot_results.json')
                data['guardrail_trials'] = []
                factory.save(work / 'artifacts/pilot_results.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'pilot')

    def test_tested_guardrail_cannot_disappear_from_experiment(self):
        def invalid(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'experiment':
                data = factory.load(work / 'artifacts/results.json')
                data['guardrail_comparisons'] = []
                factory.save(work / 'artifacts/results.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, invalid, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'experiment')

    def test_temporarily_blocked_guardrail_cannot_be_approved(self):
        def blocked(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot':
                data = factory.load(work / 'artifacts/pilot_results.json')
                data['guardrail_trials'][0]['status'] = 'blocked'
                factory.save(work / 'artifacts/pilot_results.json', data)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, blocked, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['stage'], 'pilot_review')

    def test_documented_incompatibility_does_not_require_fake_comparison(self):
        def incompatible(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot':
                data = factory.load(work / 'artifacts/pilot_results.json')
                data['guardrail_trials'][0].update(status='incompatible',
                    reason='Fixture implementation requires logits absent from the declared route')
                factory.save(work / 'artifacts/pilot_results.json', data)
            elif stage == 'plan':
                data = factory.load(work / 'artifacts/protocol.json')
                data['guardrail_baselines'] = []
                factory.save(work / 'artifacts/protocol.json', data)
            elif stage == 'experiment':
                data = factory.load(work / 'artifacts/results.json')
                data['guardrail_comparisons'] = []
                factory.save(work / 'artifacts/results.json', data)
            return result
        factory.run(self.root, incompatible, self.verify)
        self.assertEqual(factory.load(self.root / 'state.json')['status'], 'completed')

    def test_approved_idea_cannot_be_replaced_before_pilot(self):
        def tamper(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot_plan':
                data = factory.load(work / 'artifacts/candidates.json')
                data['candidates'][0]['scenario']['harness'] = 'office'
                factory.save(work / 'artifacts/candidates.json', data)
                plan = factory.load(work / 'artifacts/pilot_plan.json')
                plan['scenario_profiles']['h0'] = data['candidates'][0]['scenario']
                factory.save(work / 'artifacts/pilot_plan.json', plan)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, tamper, self.verify)
        self.assertNotIn('pilot', [s for _, s, _ in self.calls])

    def test_main_experiment_cannot_exceed_combined_cycle_budget(self):
        def oversized(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'plan':
                protocol = factory.load(work / 'artifacts/protocol.json')
                protocol['request_budget']['max_requests'] = 600
                factory.save(work / 'artifacts/protocol.json', protocol)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, oversized, self.verify)
        self.assertNotIn('build', [s for _, s, _ in self.calls])

    def test_pilot_cannot_spend_four_hours(self):
        cfg = factory.load(self.root / 'campaign.json')
        cfg['timeout_seconds'] = 86400
        factory.save(self.root / 'campaign.json', cfg)
        def oversized(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'pilot_plan':
                plan = factory.load(work / 'artifacts/pilot_plan.json')
                plan['budget']['max_seconds'] = 14400
                factory.save(work / 'artifacts/pilot_plan.json', plan)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, oversized, self.verify)
        self.assertNotIn('pilot', [s for _, s, _ in self.calls])

    def test_main_time_budget_includes_pilot(self):
        cfg = factory.load(self.root / 'campaign.json')
        cfg['timeout_seconds'] = 86400
        factory.save(self.root / 'campaign.json', cfg)
        def oversized(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'plan':
                protocol = factory.load(work / 'artifacts/protocol.json')
                protocol['request_budget']['max_seconds'] = 14400
                factory.save(work / 'artifacts/protocol.json', protocol)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, oversized, self.verify)
        self.assertNotIn('build', [s for _, s, _ in self.calls])

    def test_approved_trial_cannot_be_reclassified_to_drop_baseline(self):
        def tamper(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'plan':
                data = factory.load(work / 'artifacts/pilot_results.json')
                data['guardrail_trials'][0]['status'] = 'incompatible'
                factory.save(work / 'artifacts/pilot_results.json', data)
                protocol = factory.load(work / 'artifacts/protocol.json')
                protocol['guardrail_baselines'] = []
                factory.save(work / 'artifacts/protocol.json', protocol)
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, tamper, self.verify)
        self.assertNotIn('build', [s for _, s, _ in self.calls])

    def test_approved_pilot_evidence_cannot_be_rewritten_during_build(self):
        def tamper(role, stage, work, *args):
            result = self.worker(role, stage, work, *args)
            if stage == 'build':
                (work / 'artifacts/proof.txt').write_text('replacement evidence')
            return result
        with self.assertRaises(factory.Blocked):
            factory.run(self.root, tamper, self.verify)
        self.assertNotIn('experiment', [s for _, s, _ in self.calls])

    @unittest.skipUnless(shutil.which('bwrap'), 'requires Linux bubblewrap')
    def test_sandbox_mounts_approved_evidence_read_only(self):
        work = self.root / 'approved-sandbox'
        (work / 'artifacts').mkdir(parents=True)
        (work / 'artifacts/proof.txt').write_text('approved')
        factory.save(work / 'approved-inputs.json', {'artifacts/proof.txt': 'fixture'})
        result = subprocess.run(factory.sandbox(['python3', '-c',
            'from pathlib import Path; Path("artifacts/proof.txt").write_text("changed")'], work, self.root),
            capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((work / 'artifacts/proof.txt').read_text(), 'approved')

    def test_runtime_supplies_controller_budget_to_pilot_worker(self):
        work = self.root / 'runtime-budget'
        work.mkdir()
        limits = {'max_requests': 5, 'max_output_tokens': 100, 'max_seconds': 60}
        factory.save(work / 'pilot-frozen.json', {'budget': limits})
        cfg = factory.load(self.root / 'campaign.json')
        cfg['researcher'] = {'backend': 'claude', 'model': 'opus'}
        log = work / 'worker.json'
        def process(argv, *args):
            log.write_text('{"type":"result","subtype":"success"}')
            self.assertIn(f'RESEARCH_BUDGET_LEDGER={work / "artifacts/budget-pilot.sqlite"}', argv)
            encoded = next(a.split('=', 1)[1] for a in argv if a.startswith('RESEARCH_BUDGET_LIMITS='))
            self.assertEqual(factory.json.loads(encoded), limits)
            return {'ok': True, 'exit_code': 0}
        with patch.object(factory, 'sandbox', side_effect=lambda argv, *args: argv), patch.object(factory, 'process', side_effect=process):
            self.assertTrue(factory.Runtime(self.root, cfg).invoke('researcher', 'pilot', work, 'fixture', log)['ok'])


if __name__ == '__main__':
    unittest.main()
