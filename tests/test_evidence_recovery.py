"""Regression of the Runtime ledger -> current facts -> evaluation -> Recovery."""
import copy
import json
import threading
import unittest
from uuid import uuid4

from control_center.evaluator import Evaluator
from control_center.config import normalize_agent
from control_center.execution_graph import ExecutionGraph
from control_center.final_state import build_final_state, evidence_fingerprint, final_verification_facts
from control_center.planner import Planner, PLAN_SCHEMA_VERSION
from control_center.recovery import RecoveryController
from control_center.task_spec import deterministic_task_spec
from control_center.worker import run_task
from tests.test_control_runtime import answer
from tests import test_recovery as recovery_fixtures
from tests.test_verification_bindings import EXECUTION, INVALID, REQUEST, proposal
from tests import test_worker_assignments as assignment_fixtures


SOURCE = ('try:\n    c = float(input("Celsius: "))\n'
          '    print(f"{c * 9 / 5 + 32:.2f}")\n'
          'except ValueError:\n    print("Invalid input")\n')
FILE_CRITERION = 'temperature_converter.py exists.'
CONTENT_CRITERION = ('The temperature_converter.py source contains Celsius-to-Fahrenheit conversion, '
                     'console input, two-decimal result formatting, and invalid-input handling.')
ZERO_CRITERION = 'The program prints 32.00 for input 0.'
HUNDRED_CRITERION = 'The program prints 212.00 for input 100.'
INVALID_OUTPUT_CRITERION = 'The program reports invalid input clearly without a traceback.'
GLOBAL_FILE_CRITERION = 'temperature_converter.py contains the requested Celsius converter implementation.'
GLOBAL_RESULTS_CRITERION = ('For inputs 0 and 100, the program displays the Fahrenheit conversion '
                            'with two decimals.')
GLOBAL_INVALID_CRITERION = 'Invalid input is handled clearly without a traceback.'


def case_event(case, **extra):
    return {"event_type": "verification.case_completed", "event_id": "step-" + case["id"],
            "case_id": case["id"], "input": case["input"],
            "supports_acceptance_criterion_ids": case["supports_acceptance_criterion_ids"],
            "status": "passed", "success": True, "exit_code": 0,
            "stdout": "actual output", "stderr": "", "program_started": True,
            "environment_available": True, **extra}


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = Planner(lambda *_: proposal()).create_plan_for_spec(deterministic_task_spec(REQUEST))
        self.qa = self.plan['tasks'][1]
        self.local = [row for row in self.plan['criterion_links']['local'] if row['task_id'] == self.qa['id']]
        self.planned = {**self.qa, 'acceptance_criteria': [
            {'id': row['id'], 'criterion': row['criterion']} for row in self.local]}

    def runtime(self, cases=None, **changes):
        runtime = {'id': 'qa-runtime', 'source_task_id': self.qa['id'], 'status': 'Success',
                   'events': [case_event(case) for case in (cases if cases is not None else self.qa['verification_cases'])]}
        runtime.update(changes)
        return runtime

    def evaluate(self, runtime, planned=None):
        planned = planned or self.planned
        runtime['final_state'] = build_final_state(planned, [runtime], None)
        def semantic(_prompt, context):
            facts = {item['criterion']: item['evidence'] for item in context['semantic_criteria']}
            return {'criteria': [{'criterion': item, 'status': 'satisfied', 'reason': 'Semantic fixture review.',
                                 'evidence': [fact['evidence_id'] for fact in facts.get(item, [])],
                                 'confidence': 1.0} for item in context['planned_task']['success_criteria']]}
        return Evaluator(semantic).evaluate(planned_task=planned, runtime_task=runtime, execution_node={})

    def test_ledger_only_cases_are_first_class_facts_without_worker_final_output(self):
        outcome = self.evaluate(self.runtime())
        facts = outcome['context_snapshot']['final_state']['verification_facts']
        self.assertEqual(len(facts), 3)
        self.assertEqual(outcome['status'], 'accepted')
        self.assertEqual(outcome['missing_evidence'], [])
        self.assertEqual(outcome['metrics']['criteria_deterministic'], 1)
        self.assertTrue(all(fact['supports_acceptance_criterion_ids'] for fact in facts))
        self.assertTrue(any(event['event_type'] == 'evaluator.criterion_evidence_decided'
                            and event.get('verification_case_id') for event in outcome['events']))

    def test_step_and_case_events_enrich_one_action_not_three_proofs(self):
        runtime = self.runtime(self.qa['verification_cases'][:1])
        case = self.qa['verification_cases'][0]
        runtime['events'].insert(0, {'event_type': 'step.started', 'step_id': 'step-' + case['id'],
            'tool': 'run_command', 'arguments': {'argv': ['python', 'temperature_converter.py']}})
        runtime['events'].insert(1, {'event_type': 'step.finished', 'step_id': 'step-' + case['id'],
            'tool': 'run_command', 'status': 'Success', 'output': 'actual output'})
        # Production's final step event comes after case_completed and carries
        # an argv object in input. It must not replace the case stdin string.
        runtime['events'].append({'event_type': 'step.finished', 'step_id': 'step-' + case['id'],
            'tool': 'run_command', 'status': 'Success', 'input': {'argv': ['python', 'temperature_converter.py']}})
        runtime['result'] = {'actions': [{'tool': 'run_command', 'event_id': 'step-' + case['id'],
            'arguments': {'argv': ['python', 'temperature_converter.py']}, 'success': True, 'exit_code': 0}]}
        facts = final_verification_facts([runtime])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['case_id'], case['id'])
        self.assertEqual(facts[0]['command'], ['python', 'temperature_converter.py'])
        self.assertEqual(facts[0]['stderr'], '')
        self.assertEqual(facts[0]['input'], case['input'])

    def test_missing_case_is_a_hard_gate_even_for_an_optimistic_semantic_model(self):
        outcome = self.evaluate(self.runtime(self.qa['verification_cases'][:2]))
        self.assertEqual(outcome['status'], 'blocked')
        self.assertEqual(outcome['metrics']['model_calls'], 0)
        self.assertEqual(outcome['missing_verification_cases'], [{'task_id': self.qa['id'], 'case_id': 'case_invalid'}])
        self.assertTrue(all('case_invalid' in item for item in outcome['missing_evidence']))

    def test_existing_unbound_case_is_internal_error_not_gather_evidence(self):
        runtime = self.runtime()
        runtime['events'][2]['supports_acceptance_criterion_ids'] = ['lc-unknown']
        outcome = self.evaluate(runtime)
        self.assertEqual(outcome['reason'], 'evidence_binding_error')
        self.assertEqual(outcome['status'], 'error')
        self.assertEqual(outcome['missing_evidence'], [])
        self.assertEqual(outcome['recommended_action'], 'reject')

    def test_wrong_input_cannot_prove_the_declared_case(self):
        runtime = self.runtime()
        runtime['events'][2]['input'] = 'unexpected\n'
        outcome = self.evaluate(runtime)
        self.assertEqual(outcome['status'], 'blocked')
        self.assertEqual(outcome['missing_verification_cases'][0]['case_id'], 'case_invalid')

    def test_contradictory_status_is_not_overridden_by_zero_exit(self):
        runtime = self.runtime()
        runtime['events'][2].update(success=False, status='failed')
        outcome = self.evaluate(runtime)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertTrue(any(item['state'] == 'contradictory' for item in outcome['evidence_states']))

    def test_streams_missing_are_insufficient_not_missing_case(self):
        runtime = self.runtime()
        runtime['events'][2].pop('stdout')
        outcome = self.evaluate(runtime)
        self.assertEqual(outcome['status'], 'blocked')
        self.assertEqual(outcome['missing_verification_cases'], [])
        self.assertTrue(any(item['state'] == 'insufficient' for item in outcome['evidence_states']))

    def test_global_criterion_reuses_same_fact_ids_through_explicit_local_links(self):
        planned = copy.deepcopy(self.planned)
        planned['criterion_links'] = copy.deepcopy(self.plan['criterion_links'])
        global_row = {'id': 'gc-execution', 'criterion': EXECUTION}
        planned['criterion_links']['global'] = [global_row]
        local = next(row for row in planned['criterion_links']['local'] if row['criterion'] == EXECUTION)
        local['supports_global_criteria'] = ['gc-execution']
        planned['acceptance_criteria'].append(global_row)
        outcome = self.evaluate(self.runtime(), planned)
        groups = {row['criterion_id']: row['evidence'] for row in outcome['context_snapshot']['evidence_by_criterion']}
        self.assertEqual({ref['id'] for ref in groups[local['id']]}, {ref['id'] for ref in groups['gc-execution']})
        self.assertEqual(len(outcome['context_snapshot']['evidence_catalog']), 3)
        self.assertEqual(outcome['status'], 'accepted')

    def test_fingerprint_ignores_delivery_ids_but_detects_material_output_change(self):
        first = build_final_state(self.planned, [self.runtime()], None)
        second_runtime = self.runtime(id='other-runtime')
        for event in second_runtime['events']:
            event['event_id'] += '-replay'
        second = build_final_state(self.planned, [second_runtime], None)
        self.assertEqual(evidence_fingerprint(first), evidence_fingerprint(second))
        second['verification_facts'][2]['stdout'] = 'different output'
        self.assertNotEqual(evidence_fingerprint(first), evidence_fingerprint(second))

    def test_a_later_workspace_mutation_invalidates_reused_execution_facts(self):
        mutation = {'id': 'implementation-retry', 'result': {'actions': [
            {'tool': 'edit_file', 'success': True, 'changed': True, 'arguments': {'path': 'dependency.py'}}]}}
        facts = final_verification_facts([self.runtime(), mutation])
        self.assertEqual(facts, [])
        mutation['result']['actions'][0]['changed'] = False
        self.assertEqual(len(final_verification_facts([self.runtime(), mutation])), 3)

    def test_recovery_does_not_gather_unchanged_evidence_or_binding_errors(self):
        for error in ({'reason': 'evidence_binding_error'}, {'evidence_fingerprint': 'same'}):
            verdict = {'status': 'blocked', 'recommended_action': 'gather_evidence',
                       'missing_evidence': ['case_invalid'], **error}
            outcome = RecoveryController(offline=True).decide(planned_task=self.qa,
                execution_node={'attempt': 1}, evaluation=verdict,
                history=[{'action': 'gather_evidence', 'snapshot': {'evidence_before': 'same'}}],
                available_agents=[], plan=self.plan, limits={})
            self.assertEqual(outcome['action'], 'fail')
            self.assertIn('evidence_binding_error', outcome['reason'])


class RecoveryIdempotenceTests(unittest.TestCase):
    setUp = recovery_fixtures.RecoveryPersistenceTests.setUp
    tearDown = recovery_fixtures.RecoveryPersistenceTests.tearDown

    def pending_fixture(self):
        agent = self.store.create_agent(normalize_agent({'name': 'Recovery', 'role': 'Engineer'}))
        plan = assignment_fixtures.compile_plan([assignment_fixtures.semantic_task(
            'create', 'Create app.py', 'create_file', 'app.py', criteria=['app.py exists.'])],
            global_criteria=['app.py exists.'])
        tid = plan['tasks'][0]['id']
        run = self.store.create_orchestration('Create app.py')
        self.store.transition_orchestration(run['id'], 'Queued', 'Planning')
        self.store.save_orchestration_plan(run['id'], plan, PLAN_SCHEMA_VERSION)
        graph = ExecutionGraph(plan)
        self.store.initialize_execution_graph(run['id'], graph.serialize())
        self.store.transition_orchestration(run['id'], 'Planned', 'Running')
        selection = {'task_id': tid, 'status': 'selected', 'selected_agent_id': agent['id'],
                     'score': 1, 'classification': 'eligible', 'approval_required': False,
                     'selector_version': 1, 'reasons': [], 'warnings': [], 'candidates': [], 'attempt': 1}
        sid = self.store.save_agent_selection(run['id'], selection)
        task = self.store.create_task(agent['id'], 'Create app.py', str(self.path.parent / 'workspace'))
        did = self.store.add_delegation(run['id'], agent['id'], 'Create app.py', task['id'])
        graph.mark_selected(tid, agent['id'], sid)
        graph.mark_running(tid, task['id'], did)
        self.store.save_execution_graph(run['id'], graph.serialize())
        self.store.record_execution_attempt(run['id'], tid, selected_agent_id=agent['id'],
            selection_id=sid, runtime_task_id=task['id'], delegation_id=did, attempt=1, prompt='Create app.py')
        self.store.update_task(task['id'], status='Success', result='technical completion')
        graph.apply_runtime_status(tid, 'Success', result='technical completion')
        self.store.save_execution_graph(run['id'], graph.serialize())
        worker_id = plan['worker_assignments'][0]['worker_id']
        self.store.set_worker_status(run['id'], worker_id, 'evaluating', agent_id=agent['id'])
        committed = self.store.commit_worker_evaluation('evaluation-1', run['id'], worker_id, [tid], tid,
            failed_task_id=tid, runtime_task_id=task['id'],
            agent_id=agent['id'], attempt=1, evaluator_version=1,
            evaluation=recovery_fixtures.evaluation(), metrics={}, snapshot={}, context_truncated=False, deterministic=True)
        self.assertIsNotNone(committed)
        return run, agent, plan

    def commit(self, run, action='retry_same_agent'):
        tid = self.store.get_execution_graph(run['id'])['nodes'][0]['plan_task_id']
        return self.store.commit_recovery_action(str(uuid4()), run['id'], tid, source_attempt=1,
            source_evaluation_id='evaluation-1', decision=recovery_fixtures.recovery_decision(action, task_id=tid),
            recovery_version=5)

    def test_equivalent_decisions_reuse_the_action_with_one_durable_row(self):
        run, _, _ = self.pending_fixture()
        first = self.commit(run)
        second = self.commit(run)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.list_recoveries(run['id'])), 1)

    def test_conflicting_same_attempt_decision_is_explicit_not_sqlite_unique(self):
        run, _, _ = self.pending_fixture()
        self.commit(run)
        with self.assertRaisesRegex(ValueError, 'recovery_source_conflict'):
            self.commit(run, 'fail')
        self.assertEqual(len(self.store.list_recoveries(run['id'])), 1)

    def test_concurrent_equivalent_decisions_serialize_to_one_action(self):
        run, _, _ = self.pending_fixture()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def commit():
            try:
                barrier.wait(timeout=10)
                results.append(self.commit(run))
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=commit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(len(self.store.list_recoveries(run['id'])), 1)


class EvidencePipelineTests(unittest.TestCase):
    setUp = assignment_fixtures.WorkerAssignmentRuntimeTests.setUp
    start_run = assignment_fixtures.WorkerAssignmentRuntimeTests.start_run

    def execute(self, *, omit_case=False, replay_completed=False):
        value = proposal()
        value['tasks'][0]['success_criteria'] = [FILE_CRITERION, CONTENT_CRITERION]
        value['tasks'][1]['success_criteria'] = [ZERO_CRITERION, HUNDRED_CRITERION,
                                                 INVALID_OUTPUT_CRITERION]
        for case, criterion in zip(value['tasks'][1]['verification_cases'],
                                   [ZERO_CRITERION, HUNDRED_CRITERION, INVALID_OUTPUT_CRITERION]):
            case['supports_criteria'] = [criterion]
        value['success_criteria'] = [GLOBAL_FILE_CRITERION, GLOBAL_RESULTS_CRITERION,
                                     GLOBAL_INVALID_CRITERION]
        plan = Planner(lambda *_: value).create_plan_for_spec(deterministic_task_spec(REQUEST))
        local_ids = {row['criterion']: row['id'] for row in plan['criterion_links']['local']}
        global_ids = {row['criterion']: row['id'] for row in plan['criterion_links']['global']}
        owner = self

        class RealRuntime(assignment_fixtures.ControlledRuntime):
            def __init__(self, store):
                super().__init__(store)
                self.inputs = []
                self.outcomes = []

            def finish_active(self, seconds=None, status='Success'):
                for task in self.store.list_tasks(limit=10000):
                    if task['status'] != 'Running':
                        continue
                    implementation = task['config']['provenance']['plan_task_id'] == plan['tasks'][0]['id']
                    if implementation:
                        replies = [answer(calls=[('write_file', {'path': 'temperature_converter.py', 'content': SOURCE})]),
                                   answer('Created')]
                    else:
                        if omit_case and not self.inputs:
                            task = copy.deepcopy(task)
                            task['config']['verification_cases'] = task['config']['verification_cases'][:2]
                        elif replay_completed:
                            task = copy.deepcopy(task)
                            task['config']['verification_cases'] = plan['tasks'][1]['verification_cases'][:2]
                        self.inputs.extend(case['input'] for case in task['config']['verification_cases'])
                        replies = [answer(calls=[('run_command', {'argv': ['python', 'temperature_converter.py']})])]
                    replies = iter(replies)
                    def publish(message):
                        if message.get('kind') == 'event':
                            owner.store.append_event(task['id'], message['event'])
                    result = run_task(task, self.workspace.parent, publish, lambda: None,
                                      transport=lambda *args, **kwargs: next(replies),
                                      approval_handler=lambda request: 'approved_once')
                    owner.assertEqual(result['status'], 'Success', result.get('error'))
                    self.outcomes.append(result)
                    # Deliberately remove action evidence: the ledger alone must
                    # suffice, independently of worker final_output serialization.
                    result = copy.deepcopy(result)
                    if not implementation:
                        result['result'] = {'summary': 'Technical execution complete.'}
                        result['verification'] = {'requested': False, 'evidence': []}
                    owner.store.update_task(task['id'], status='Success', result=result['result'],
                                            verification=result['verification'])

        reviewed_contexts = []
        def review(_prompt, context):
            reviewed_contexts.append(copy.deepcopy(context))
            return {'criteria': [{'criterion': row['criterion'], 'status': 'satisfied',
                'reason': 'Current file content and associated verification output satisfy the criterion.',
                'evidence': [fact['evidence_id'] for fact in row['evidence']], 'confidence': 1.0}
                for row in context['semantic_criteria']]}

        evaluator = Evaluator(review)
        runtime = RealRuntime(self.store)
        oid, orchestrator = self.start_run(plan, runtime, runtime.finish_active, evaluator)
        orchestrator._run_graph(oid, self.store.get_orchestration(oid), orchestrator.clock() + 60,
                                operational_prompt=REQUEST)
        owner.semantic_contexts = reviewed_contexts
        return oid, runtime

    def test_converter_three_real_cases_accepted_no_recovery_and_orchestration_success(self):
        oid, runtime = self.execute()
        plan = self.store.get_orchestration(oid)['plan']
        local_ids = {row['criterion']: row['id'] for row in plan['criterion_links']['local']}
        global_ids = {row['criterion']: row['id'] for row in plan['criterion_links']['global']}
        self.assertEqual(local_ids[FILE_CRITERION], 'lc-1')
        self.assertEqual(local_ids[CONTENT_CRITERION], 'lc-2')
        self.assertEqual(local_ids[ZERO_CRITERION], 'lc-3')
        self.assertEqual(local_ids[HUNDRED_CRITERION], 'lc-4')
        self.assertEqual(local_ids[INVALID_OUTPUT_CRITERION], 'lc-5')
        self.assertEqual(self.store.get_orchestration(oid)['status'], 'Success',
                         (self.store.get_orchestration(oid).get('error'), self.store.list_evaluations(oid)))
        self.assertEqual(self.store.list_recoveries(oid), [])
        self.assertEqual(runtime.inputs, ['0\n', '100\n', 'invalid\n'])
        self.assertEqual((runtime.workspace / 'temperature_converter.py').read_text(), SOURCE)
        evaluation = self.store.list_evaluations(oid)[0]
        self.assertEqual(evaluation['status'], 'accepted')
        self.assertEqual({item['status'] for item in evaluation['criteria']}, {'satisfied'})
        self.assertEqual(evaluation['metrics']['criteria_deterministic'] +
                         evaluation['metrics']['criteria_semantic'], 8)
        self.assertEqual(evaluation['missing_evidence'], [])
        context = self.semantic_contexts[0]
        self.assertNotIn('final_state', context)
        self.assertEqual(set(context['planned_task']['success_criteria']), {
            CONTENT_CRITERION, ZERO_CRITERION, HUNDRED_CRITERION, INVALID_OUTPUT_CRITERION,
            GLOBAL_FILE_CRITERION, GLOBAL_RESULTS_CRITERION, GLOBAL_INVALID_CRITERION,
        })
        rows = {criterion_id: row for row in context['semantic_criteria']
                for criterion_id in row['criterion_ids']}
        expected_cases = [
            (local_ids[ZERO_CRITERION], {'case_0'}),
            (local_ids[HUNDRED_CRITERION], {'case_100'}),
            (local_ids[INVALID_OUTPUT_CRITERION], {'case_invalid'}),
            (global_ids[GLOBAL_RESULTS_CRITERION], {'case_0', 'case_100'}),
            (global_ids[GLOBAL_INVALID_CRITERION], {'case_invalid'}),
        ]
        for criterion_id, case_ids in expected_cases:
            evidence = rows[criterion_id]['evidence']
            self.assertEqual({fact.get('case_id') for fact in evidence if fact.get('case_id')}, case_ids)
            self.assertTrue(context['criterion_evidence'][criterion_id])
            for fact in (item for item in evidence if item.get('case_id')):
                self.assertEqual(fact['criterion_id'], criterion_id)
                self.assertEqual(fact['verification_case_id'], fact['case_id'])
                self.assertEqual(fact['type'], 'command_execution')
                self.assertTrue(fact['source'])
                self.assertTrue(fact['evidence_id'])
                self.assertIn('input', fact)
                self.assertIn('stdout', fact)
                self.assertIn('stderr', fact)
                self.assertIn('exit_code', fact)
                self.assertIn('status', fact)
        self.assertIn('32.00', next(fact['stdout'] for fact in rows[local_ids[ZERO_CRITERION]]['evidence']
                                     if fact.get('case_id') == 'case_0'))
        self.assertIn('212.00', next(fact['stdout'] for fact in rows[local_ids[HUNDRED_CRITERION]]['evidence']
                                      if fact.get('case_id') == 'case_100'))
        source_row = rows[local_ids[CONTENT_CRITERION]]
        source_fact = next(fact for fact in source_row['evidence']
                           if fact.get('type') == 'file_readback')
        self.assertEqual(source_fact['path'], 'temperature_converter.py')
        self.assertEqual(source_fact['content'], SOURCE)
        self.assertIn('ValueError', source_fact['content'])
        global_file_evidence = rows[global_ids[GLOBAL_FILE_CRITERION]]['evidence']
        self.assertIn(source_fact['evidence_id'], [fact['evidence_id'] for fact in global_file_evidence])
        local_case_evidence = {
            criterion: {fact['evidence_id'] for fact in rows[local_ids[criterion]]['evidence']
                        if fact.get('case_id')}
            for criterion in (ZERO_CRITERION, HUNDRED_CRITERION, INVALID_OUTPUT_CRITERION)
        }
        global_results_evidence = {fact['evidence_id'] for fact in
                                   rows[global_ids[GLOBAL_RESULTS_CRITERION]]['evidence']
                                   if fact.get('case_id')}
        self.assertEqual(global_results_evidence,
                         local_case_evidence[ZERO_CRITERION] | local_case_evidence[HUNDRED_CRITERION])
        global_invalid_evidence = {fact['evidence_id'] for fact in
                                   rows[global_ids[GLOBAL_INVALID_CRITERION]]['evidence']
                                   if fact.get('case_id')}
        self.assertEqual(global_invalid_evidence, local_case_evidence[INVALID_OUTPUT_CRITERION])
        invalid_fact = next(fact for fact in rows[local_ids[INVALID_OUTPUT_CRITERION]]['evidence']
                            if fact.get('case_id') == 'case_invalid')
        self.assertIn('Invalid input', invalid_fact['stdout'])
        criteria = {item['criterion_id']: item for item in evaluation['criteria']}
        self.assertEqual(criteria[local_ids[FILE_CRITERION]]['status'], 'satisfied')
        self.assertIn('final_file:temperature_converter.py', criteria[local_ids[CONTENT_CRITERION]]['evidence'])
        orchestration_events = self.store.get_orchestration(oid)['events']
        for criterion, case_id in ((ZERO_CRITERION, 'case_0'), (HUNDRED_CRITERION, 'case_100'),
                                   (INVALID_OUTPUT_CRITERION, 'case_invalid')):
            evidence_ids = criteria[local_ids[criterion]]['evidence']
            bound_events = [json.loads(event['payload_json']) for event in orchestration_events
                            if event.get('event_type') == 'evaluator.evidence_prepared'
                            and json.loads(event['payload_json']).get('criterion_id') == local_ids[criterion]]
            self.assertEqual(len(bound_events), 1)
            self.assertTrue(set(evidence_ids) & set(bound_events[0]['evidence_ids']))

    def test_missing_case_recovery_runs_only_that_case_and_reuses_prior_facts(self):
        oid, runtime = self.execute(omit_case=True)
        self.assertEqual(self.store.get_orchestration(oid)['status'], 'Success',
                         (self.store.get_orchestration(oid).get('error'), self.store.list_evaluations(oid)))
        self.assertEqual(runtime.inputs, ['0\n', '100\n', 'invalid\n'])
        recoveries = self.store.list_recoveries(oid)
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]['action'], 'gather_evidence')
        evaluations = self.store.list_evaluations(oid)
        self.assertEqual([item['status'] for item in evaluations], ['blocked', 'accepted'])
        self.assertEqual(evaluations[0]['missing_verification_cases'][0]['case_id'], 'case_invalid')
        self.assertNotEqual(evaluations[0]['evidence_fingerprint'], evaluations[1]['evidence_fingerprint'])

    def test_recovery_that_repeats_evidence_stops_without_a_second_gather_action(self):
        oid, runtime = self.execute(omit_case=True, replay_completed=True)
        self.assertEqual(self.store.get_orchestration(oid)['status'], 'Failed')
        self.assertEqual(runtime.inputs, ['0\n', '100\n', '0\n', '100\n'])
        self.assertEqual(len(self.store.list_recoveries(oid)), 1)
        evaluations = self.store.list_evaluations(oid)
        self.assertEqual(evaluations[0]['evidence_fingerprint'], evaluations[1]['evidence_fingerprint'])
        events = self.store.get_orchestration(oid)['events']
        self.assertTrue(any(event['event_type'] == 'recovery.evidence_no_progress' for event in events))


if __name__ == '__main__':
    unittest.main()
