"""Case references bind to reconciled local criteria, never provisional owners."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from control_center.plan_compiler import compile_semantic_plan, _reconcile_criterion_evidence
from control_center.planner import (PlanValidationError, Planner, validate_plan,
    VerificationCriterionReferenceError, VerificationBindingInvariantError)
from control_center.task_spec import deterministic_task_spec
from control_center.runtime_resources import RuntimeResourceCatalog
from control_center.evaluator import Evaluator, normalize_final_state_evidence
from control_center.final_state import build_final_state
from control_center.plan_evidence import classify_criterion, verification_mode
from tests import test_control_runtime as worker_fixtures
from tests.test_plan_granularity import converter_plan


REQUEST = (
    'Creá un archivo temperature_converter.py que convierta grados Celsius a Fahrenheit.\n'
    'El programa debe pedir una temperatura por consola, mostrar el resultado con\n'
    '2 decimales y manejar correctamente entradas inválidas. Después ejecutalo al menos\n'
    'con 0, 100 y una entrada inválida para verificar que funciona')
EXECUTION = 'The script executes successfully with temperatures 0, 100, and an invalid input.'
INVALID = 'The script handles invalid inputs gracefully.'


def proposal():
    value = converter_plan()
    implementation = value['tasks'][1]
    implementation.update(key='implement', operations=['create_file'], depends_on=[],
                          owned_paths=['temperature_converter.py'])
    qa = value['tasks'][2]
    qa['depends_on'] = ['implement']
    qa['success_criteria'] = [EXECUTION, INVALID]
    for case in qa['verification_cases']:
        case['supports_criteria'] = [EXECUTION]
    qa['verification_cases'][2]['supports_criteria'].append(INVALID)
    value['tasks'] = [implementation, qa]
    return value


class VerificationBindingTests(unittest.TestCase):
    def compile(self, value):
        return compile_semantic_plan(value, deterministic_task_spec(REQUEST))

    def test_reference_is_resolved_after_criterion_moves_to_testing(self):
        value = proposal()
        value['tasks'][0]['success_criteria'].append(EXECUTION)
        value['tasks'][1]['success_criteria'] = [INVALID]
        original = copy.deepcopy(value)
        compiled = self.compile(value)
        qa = compiled['tasks'][1]
        self.assertEqual(value, original)
        self.assertNotIn(EXECUTION, compiled['tasks'][0]['success_criteria'])
        self.assertIn(EXECUTION, qa['success_criteria'])
        row = next(row for row in compiled['criterion_links']['local'] if row['criterion'] == EXECUTION)
        self.assertEqual(row['task_id'], qa['id'])
        self.assertTrue(all(row['id'] in case['supports_acceptance_criterion_ids']
                            for case in qa['verification_cases']))

    def test_safe_surface_variants_resolve_to_final_text(self):
        value = proposal()
        criterion = 'The script `temperature_converter.py` executes successfully.'
        value['tasks'][1]['success_criteria'] = [criterion]
        for case in value['tasks'][1]['verification_cases']:
            case['supports_criteria'] = ['  The  script temperature_converter.py\nexecutes successfully.  ']
        compiled = self.compile(value)
        self.assertTrue(all(case['supports_criteria'] == [criterion]
                            for case in compiled['tasks'][1]['verification_cases']))

    def test_many_to_many_bindings_preserve_three_cases_and_two_tasks(self):
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(proposal(), deterministic_task_spec(REQUEST), resource_catalog=catalog)
        self.assertEqual((compiled['task_count'], compiled['worker_count']), (2, 1))
        qa = compiled['tasks'][1]
        rows = {row['criterion']: row['id'] for row in compiled['criterion_links']['local']}
        self.assertEqual([case['id'] for case in qa['verification_cases']], ['case_0', 'case_100', 'case_invalid'])
        self.assertEqual([case['supports_acceptance_criterion_ids'] for case in qa['verification_cases']],
                         [[rows[EXECUTION]], [rows[EXECUTION]], [rows[EXECUTION], rows[INVALID]]])
        self.assertEqual(validate_plan(compiled), compiled)
        started = [event for event in catalog.compiler_events
                   if event['event_type'] == 'plan_compiler.verification_case_binding_started']
        bound = [event for event in catalog.compiler_events
                 if event['event_type'] == 'plan_compiler.verification_case_bound']
        self.assertEqual((len(started), len(bound)), (3, 4))
        for event in bound:
            self.assertEqual(event['task_key'], 'verify')
            self.assertEqual(event['resolved_criterion_id'], event['criterion_id'])
        self.assertTrue(all('input' not in event for event in started + bound))

    def test_aggregate_requires_complete_declared_runtime_cases(self):
        self.assertEqual(classify_criterion(EXECUTION), 'runtime_behavior')
        self.assertEqual(verification_mode(EXECUTION), 'case_set_success')

    def test_compiler_links_semantically_supported_local_criteria_to_global_obligations(self):
        value = proposal()
        value['success_criteria'] = [
            'temperature_converter.py contains Celsius conversion, console input, two-decimal results, and ValueError handling.',
            'The converter displays the expected Fahrenheit results for inputs 0 and 100.',
            'Invalid input is handled gracefully.',
        ]
        compiled = self.compile(value)
        links = {row['criterion']: set(row['supports_global_criteria'])
                 for row in compiled['criterion_links']['local']}
        global_ids = {row['criterion']: row['id'] for row in compiled['criterion_links']['global']}
        self.assertIn(global_ids[value['success_criteria'][0]], links[
            'The source defines Celsius conversion, console input, two-decimal formatting and ValueError handling.'])
        self.assertIn(global_ids[value['success_criteria'][1]], links[EXECUTION])
        self.assertIn(global_ids[value['success_criteria'][2]], links[INVALID])
        self.assertTrue(all(row['supports_global_criteria'] for row in compiled['criterion_links']['local']))

    def test_unknown_reference_has_bounded_contextual_diagnostic(self):
        value = proposal()
        value['tasks'][1]['verification_cases'][0]['supports_criteria'] = ['This criterion does not exist.']
        catalog = RuntimeResourceCatalog.build()
        with self.assertRaises(VerificationCriterionReferenceError) as caught:
            compile_semantic_plan(value, deterministic_task_spec(REQUEST), resource_catalog=catalog)
        self.assertIsInstance(caught.exception, PlanValidationError)
        diagnostic = caught.exception.diagnostics
        self.assertEqual((diagnostic['task_key'], diagnostic['task_id'], diagnostic['case_id']),
                         ('verify', 'task-2', 'case_0'))
        self.assertEqual(diagnostic['semantic_criterion_reference'], 'This criterion does not exist.')
        self.assertEqual(diagnostic['available_local_criteria'], [EXECUTION, INVALID])
        failed = next(event for event in catalog.compiler_events
                      if event['event_type'] == 'plan_compiler.verification_case_binding_failed')
        self.assertEqual(failed['reason'], 'unknown')
        self.assertNotIn('input', failed)

    def test_multiple_surface_matches_are_rejected_as_ambiguous(self):
        value = proposal()
        first = 'The script `temperature_converter.py` executes successfully.'
        second = 'The script temperature_converter.py executes successfully.'
        value['tasks'][1]['success_criteria'] = [first, second]
        for case in value['tasks'][1]['verification_cases']:
            case['supports_criteria'] = [second]
        with self.assertRaises(VerificationCriterionReferenceError) as caught:
            self.compile(value)
        self.assertEqual(caught.exception.diagnostics['reason'], 'ambiguous')
        self.assertIn('ambiguous local criterion reference', str(caught.exception))

    def test_substrings_and_semantic_predicates_are_not_reference_equivalence(self):
        for reference in ('executes successfully', EXECUTION + ' and always computes correct results.'):
            with self.subTest(reference=reference):
                value = proposal()
                value['tasks'][1]['verification_cases'][0]['supports_criteria'] = [reference]
                with self.assertRaises(VerificationCriterionReferenceError):
                    self.compile(value)

    def test_reference_identity_does_not_discard_semantic_diacritics(self):
        value = proposal()
        value['tasks'][1]['success_criteria'] = ['The script records año as requested.']
        for case in value['tasks'][1]['verification_cases']:
            case['supports_criteria'] = ['The script records ano as requested.']
        with self.assertRaises(VerificationCriterionReferenceError):
            self.compile(value)

    def test_unbound_cases_compile_and_emit_explicit_unbound_events(self):
        value = proposal()
        for case in value['tasks'][1]['verification_cases']:
            case.pop('supports_criteria')
        catalog = RuntimeResourceCatalog.build()
        compiled = compile_semantic_plan(value, deterministic_task_spec(REQUEST), resource_catalog=catalog)
        self.assertTrue(all('supports_acceptance_criterion_ids' not in case
                            for case in compiled['tasks'][1]['verification_cases']))
        self.assertEqual(len([event for event in catalog.compiler_events
                              if event['event_type'] == 'plan_compiler.verification_case_unbound']), 3)

    def test_planner_repair_targets_original_task_after_normalization(self):
        original = converter_plan()  # 3 semantic Tasks become 2 compiled Tasks.
        original['tasks'][2]['success_criteria'] = [EXECUTION, INVALID]
        original['tasks'][2]['verification_cases'][0]['supports_criteria'] = ['Invented criterion.']
        repaired = copy.deepcopy(original)
        repaired['tasks'][2]['verification_cases'][0]['supports_criteria'] = [EXECUTION]
        calls = []
        def model(prompt, context):
            calls.append(prompt)
            return original if len(calls) == 1 else repaired
        planner = Planner(model)
        compiled = planner.create_plan_for_spec(deterministic_task_spec(REQUEST))
        self.assertEqual((len(calls), compiled['task_count']), (2, 2))
        diagnostic = json.loads(calls[1])['compiler_error']
        self.assertEqual(diagnostic['type'], 'VerificationCriterionReferenceError')
        self.assertEqual(diagnostic['affected_tasks'], ['task-3'])
        self.assertEqual(diagnostic['affected_paths'], [])
        self.assertEqual(diagnostic['verification_criterion_reference']['task_id'], 'task-2')
        self.assertEqual(diagnostic['verification_criterion_reference']['case_id'], 'case_0')

    def test_grouped_case_error_targets_its_semantic_source_not_the_anchor(self):
        value = proposal()
        qa = value['tasks'][1]
        siblings = []
        for index, case in enumerate(qa['verification_cases']):
            sibling = copy.deepcopy(qa)
            sibling['key'] = f'verify_{index}'
            sibling['verification_cases'] = [case]
            siblings.append(sibling)
        value['tasks'] = [value['tasks'][0], *siblings]
        siblings[1]['verification_cases'][0]['supports_criteria'] = ['Invented criterion.']
        with self.assertRaises(VerificationCriterionReferenceError) as caught:
            self.compile(value)
        self.assertEqual(caught.exception.diagnostics['semantic_task_keys'], ['verify_1'])

    def test_compiler_invariant_failure_does_not_request_planner_repair(self):
        def corrupt(*args, **kwargs):
            reconciled = _reconcile_criterion_evidence(*args, **kwargs)
            args[0][1]['success_criteria'].remove(EXECUTION)
            return reconciled
        calls = []
        planner = Planner(lambda *_: (calls.append(True), proposal())[1])
        with patch('control_center.plan_compiler._reconcile_criterion_evidence', side_effect=corrupt):
            with self.assertRaises(VerificationBindingInvariantError):
                planner.create_plan_for_spec(deterministic_task_spec(REQUEST))
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(event['event_type'] == 'planner.repair_requested'
                             for event in planner.metrics.get('planner_events', [])))
        self.assertEqual(planner.metrics['semantic_compiler']['status'], 'Failed')

    def test_reference_to_ancestor_static_criterion_is_a_reference_error_not_an_invariant(self):
        value = proposal()
        reference = value['tasks'][0]['success_criteria'][0]
        value['tasks'][1]['verification_cases'][0]['supports_criteria'] = [reference]
        with self.assertRaises(VerificationCriterionReferenceError):
            self.compile(value)

    def test_resolved_ids_override_conflicting_text_and_recovered_metadata(self):
        compiled = self.compile(proposal())
        qa = compiled['tasks'][1]
        criteria = [{'id': row['id'], 'criterion': row['criterion']}
                    for row in compiled['criterion_links']['local'] if row['task_id'] == qa['id']]
        execution_id = next(row['id'] for row in criteria if row['criterion'] == EXECUTION)
        invalid_id = next(row['id'] for row in criteria if row['criterion'] == INVALID)
        fact = {'id': 'F-case_0', 'case_id': 'case_0', 'type': 'command_execution',
                'supports_acceptance_criterion_ids': [execution_id],
                'supports_acceptance_criteria': [INVALID], 'criterion_id': invalid_id,
                'path': 'temperature_converter.py', 'stdout': 'Invalid input', 'exit_code': 0}
        normalized = normalize_final_state_evidence({'verification_facts': [fact]}, {'acceptance_criteria': criteria})
        groups = {group['criterion_id']: group['evidence'] for group in normalized['by_criterion']}
        self.assertEqual([ref['id'] for ref in groups[execution_id]], ['F-case_0'])
        self.assertEqual(groups[invalid_id], [])
        self.assertEqual(groups[execution_id][0]['association'], 'declared_verification_case')


class VerificationBindingRuntimeTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    run_worker = worker_fixtures.WorkerTests.run_worker

    def execute_cases(self, *, bound):
        value = proposal()
        # Exercise the exact lifecycle bug in a real Worker/venv regression.
        value['tasks'][0]['success_criteria'].append(EXECUTION)
        value['tasks'][1]['success_criteria'] = [INVALID]
        if not bound:
            for case in value['tasks'][1]['verification_cases']:
                case.pop('supports_criteria')
        spec = deterministic_task_spec(REQUEST)
        self.assertEqual(spec['source_prompt'], REQUEST)
        compiled = Planner(lambda *_: value).create_plan_for_spec(spec)
        self.assertEqual((compiled['task_count'], compiled['worker_count']), (2, 1))
        implementation, qa = compiled['tasks']
        self.assertEqual((implementation['task_kind'], qa['task_kind']), ('program_creation', 'testing'))
        source = ('try:\n    celsius = float(input("Celsius: "))\n'
                  '    print(f"{celsius * 9 / 5 + 32:.2f}")\n'
                  'except ValueError:\n    print("Invalid input")\n')
        created = self.run_worker([worker_fixtures.answer(calls=[('write_file', {
            'path': 'temperature_converter.py', 'content': source})]), worker_fixtures.answer('Implemented')],
            config={'verification': {'enabled': False}}, prompt=implementation['description'])
        self.assertEqual(created['status'], 'Success', created.get('error'))
        executed = self.run_worker([worker_fixtures.answer(calls=[('run_command', {
            'argv': ['python', 'temperature_converter.py']})])], tools=qa['required_tools'],
            config={'permissions': 'execute', 'verification': {'enabled': False},
                    'verification_mode': qa['verification_mode'], 'verification_cases': qa['verification_cases'],
                    'output': {'format': 'structured', 'include': ['summary', 'actions', 'artifacts',
                                                                  'verification', 'limitations']}},
            prompt=qa['description'])
        self.assertEqual(executed['status'], 'Success', executed.get('error'))
        self.assertEqual(executed['model_calls'], 1)
        executed.update(id='runtime-qa', source_task_id=qa['id'],
                        config={'worker_assignment': {'worker_id': 'worker-1'}})
        local = [row for row in compiled['criterion_links']['local'] if row['task_id'] == qa['id']]
        planned = {**qa, 'acceptance_criteria': [{'id': row['id'], 'criterion': row['criterion']} for row in local],
                   'read_targets': ['temperature_converter.py']}
        state = build_final_state(planned, [executed], str(self.workspace))
        facts = state['verification_facts']
        self.assertEqual([fact['case_id'] for fact in facts], ['case_0', 'case_100', 'case_invalid'])
        for fact, case, output in zip(facts, qa['verification_cases'], ['32.00', '212.00', 'Invalid input']):
            self.assertEqual((fact['program_started'], fact['environment_available'], fact['exit_code'], fact['status']),
                             (True, True, 0, 'passed'))
            self.assertEqual(fact['input'], case['input'])
            self.assertEqual(fact['stdin_sha256'], hashlib.sha256(case['input'].encode()).hexdigest())
            self.assertIn(output, fact['stdout'])
            self.assertEqual(fact['stderr'], '')
            self.assertEqual((fact['source_task_id'], fact['source_runtime_task_id'], fact['worker_id']),
                             (qa['id'], 'runtime-qa', 'worker-1'))
        reviewed = []
        def review(_prompt, context):
            reviewed.append(context)
            self.assertEqual(context['planned_task']['success_criteria'], [INVALID])
            evidence = context['semantic_criteria'][0]['evidence']
            case_facts = [fact for fact in evidence if fact.get('case_id')]
            self.assertEqual({fact['case_id'] for fact in case_facts}, {'case_invalid'})
            case_map = context['criterion_evidence']
            self.assertTrue(case_map[context['semantic_criteria'][0]['criterion_id']])
            fact = case_facts[0]
            for field in ('evidence_id', 'criterion_id', 'type', 'source', 'verification_case_id',
                          'status', 'input', 'stdout', 'stderr', 'exit_code'):
                self.assertIn(field, fact)
            return {'criteria': [{'criterion': criterion, 'status': 'satisfied' if bound else 'unknown',
                    'reason': 'Review of the actual case output.', 'evidence': [fact['evidence_id'] for fact in evidence],
                    'confidence': 1.0} for criterion in context['planned_task']['success_criteria']]}
        outcome = Evaluator(review).evaluate(planned_task=planned,
                    runtime_task={**executed, 'final_state': state}, execution_node={})
        self.assertEqual(outcome['metrics']['criteria_deterministic'], 1)
        self.assertEqual(len(reviewed), 1 if bound else 0)
        return compiled, facts, outcome, outcome['context_snapshot']

    def test_exact_temperature_converter_aggregate_reaches_worker_and_evaluator(self):
        compiled, facts, outcome, context = self.execute_cases(bound=True)
        self.assertEqual(outcome['status'], 'accepted')
        groups = {group['criterion']: group['evidence'] for group in context['evidence_by_criterion']}
        command_ids = {fact['id'] for fact in facts}
        self.assertEqual({ref['id'] for ref in groups[EXECUTION]} & command_ids, command_ids)
        self.assertEqual({ref['id'] for ref in groups[INVALID]} & command_ids, {facts[2]['id']})
        for record in context['evidence_catalog']:
            if record.get('case_id'):
                raw = next(fact for fact in facts if fact['case_id'] == record['case_id'])
                for field in ('case_id', 'input', 'stdin_sha256', 'supports_acceptance_criterion_ids',
                              'source_task_id', 'source_runtime_task_id', 'worker_id', 'program_started',
                              'environment_available', 'exit_code', 'stdout', 'stderr', 'status'):
                    self.assertEqual(record[field], raw[field])

    def test_unbound_cases_execute_and_remain_global_without_invented_association(self):
        _compiled, facts, outcome, context = self.execute_cases(bound=False)
        self.assertEqual(outcome['reason'], 'evidence_binding_error')
        self.assertEqual(outcome['missing_evidence'], [])
        fact_ids = {fact['id'] for fact in facts}
        self.assertTrue(fact_ids <= set(context['global_evidence_ids']))
        self.assertFalse(any(ref['id'] in fact_ids
                             for group in context['evidence_by_criterion'] for ref in group['evidence']))


if __name__ == '__main__':
    unittest.main()
