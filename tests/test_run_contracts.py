"""Regressions for startup, information grounding and independent QA cases."""
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from control_center.settings import Settings, DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS
from control_center.task_spec import TaskSpecAnalyst, deterministic_task_spec, _unsupported_scope_terms
from control_center.verification_cases import normalize_cases, explicit_cases, expand_case_calls
from control_center.verification_cases import group_case_tasks
from control_center.final_state import build_final_state
from control_center.orchestrator import Orchestrator
from control_center.runtime import Runtime
from control_center.storage import Store
from control_center.planner import semantic_plan_response_format
from control_center.plan_compiler import compile_semantic_plan
from control_center.planner import validate_plan, PlanValidationError
from control_center.evaluator import Evaluator
from tests.test_plan_granularity import CONVERTER_REQUEST, converter_plan
from tests.test_task_spec import _analyst_response
from tests import test_control_runtime as worker_fixtures

answer = worker_fixtures.answer


class RunConfigurationTests(unittest.TestCase):
    def test_default_is_exactly_twice_observed_default(self):
        self.assertEqual(DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS, 900 * 2)

    def test_local_configuration_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.freya-local.json').write_text(json.dumps({'FREYA_DEBUG_LLM_PROMPTS': True,
                'FREYA_ORCHESTRATION_TIMEOUT_SECONDS': 1800}))
            settings = Settings.from_sources(root, {})
            self.assertTrue(settings.debug_llm_prompts)
            self.assertIn('local_file:', settings.debug_llm_prompts_source)
            overridden = Settings.from_sources(root, {'FREYA_DEBUG_LLM_PROMPTS': 'false',
                                                     'FREYA_ORCHESTRATION_TIMEOUT_SECONDS': '75'})
            self.assertFalse(overridden.debug_llm_prompts)
            self.assertEqual(overridden.orchestration_timeout_seconds, 75)
            self.assertEqual(overridden.orchestration_timeout_source, 'environment')
            self.assertFalse(Settings.from_sources(root / 'unconfigured', {}).debug_llm_prompts)

    def test_invalid_timeouts_fail_instead_of_silently_using_default(self):
        for value in ('0', '-1', 'nan', 'inf', 'invalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_environment({'FREYA_ORCHESTRATION_TIMEOUT_SECONDS': value})

    def test_waiting_for_approval_does_not_consume_active_orchestration_budget(self):
        store = Mock()
        store.get_execution_graph.return_value = {'nodes': [{'state': 'waiting_for_approval', 'runtime_task_id': 'r'}]}
        store.get_task.return_value = {'status': 'WaitingForApproval'}
        now = [0]
        def wait(_seconds): now[0] += 30
        orchestrator = Orchestrator(store, Mock(), clock=lambda: now[0], wait=wait)
        deadline = orchestrator._wait_graph_poll('run', 10)
        self.assertEqual(deadline, 40)
        # Database reconciliation/polling while blocked is also waiting time.
        now[0] = 35
        self.assertEqual(orchestrator._wait_graph_poll('run', deadline, poll_started=30), 75)
        store.get_task.return_value = {'status': 'Running'}
        store.get_execution_graph.return_value['nodes'][0]['state'] = 'running'
        self.assertEqual(orchestrator._wait_graph_poll('run', deadline), 40)

    def test_approval_on_one_branch_does_not_pause_other_running_work(self):
        store = Mock()
        store.get_execution_graph.return_value = {'nodes': [
            {'state': 'waiting_for_approval', 'runtime_task_id': 'waiting'},
            {'state': 'running', 'runtime_task_id': 'active'}]}
        store.get_task.side_effect = lambda identifier: {'status':
            'WaitingForApproval' if identifier == 'waiting' else 'Running'}
        now = [0]
        def wait(_seconds): now[0] += .2
        orchestrator = Orchestrator(store, Mock(), clock=lambda: now[0], wait=wait)
        self.assertEqual(orchestrator._wait_graph_poll('run', 10), 10)

    def test_parent_budget_survives_thirty_seconds_wait_and_resume(self):
        runtime = Runtime(Mock(), Path.cwd(), Path.cwd())
        process = Mock()
        process.is_alive.return_value = True
        worker = {'process': process, 'queue': queue.Queue(), 'control': queue.Queue(),
                  'approval_id': 'approval', 'approval_wait_started': 5,
                  'started': 0, 'max_seconds': 10, 'agent_id': 'agent', 'workspace_key': 'work'}
        runtime.active['task'] = worker
        runtime.store.get_approval.return_value = {'task_id': 'task', 'agent_id': 'agent',
            'tool': 'write_file', 'capability': 'filesystem.create'}
        runtime.wake = Mock()
        runtime.wake.wait.side_effect = lambda _: setattr(runtime, 'closed', True)
        with patch('control_center.runtime.time.monotonic', return_value=35), \
                patch.object(runtime, '_finish') as finish, patch.object(runtime, '_refresh_agent'):
            runtime._schedule_until_error()
            finish.assert_not_called()
            runtime.resolve_approval('approval', 'approved_once')
            self.assertEqual(worker['started'], 30)
            self.assertIsNone(worker['approval_id'])
            runtime.closed = False
            runtime._schedule_until_error()
            finish.assert_not_called()


class RealEntrypointTests(unittest.TestCase):
    def test_second_entrypoint_rejects_locked_directory_without_traceback(self):
        from control_center.__main__ import InstanceLock
        with tempfile.TemporaryDirectory() as directory:
            lock = InstanceLock(Path(directory))
            try:
                result = subprocess.run([sys.executable, '-m', 'control_center',
                    '--data-dir', directory], cwd=worker_fixtures.ROOT,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn('Another server is already using this data directory.', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn('freya.runtime.configuration', result.stdout)
            finally:
                lock.close()
            # The persistent file is not a stale lock after its owner closes.
            replacement = InstanceLock(Path(directory))
            replacement.close()

    def test_normal_entrypoint_exports_debug_to_actual_spawned_worker(self):
        provider = worker_fixtures.FakeOllama([answer(json.dumps({
            'summary': 'Hola Freya', 'actions': [], 'artifacts': [],
            'verification': {}, 'limitations': []}))])
        self.addCleanup(provider.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            env = dict(os.environ, FREYA_DEBUG_LLM_PROMPTS='true')
            with (root / 'stdout.log').open('w') as stdout, (root / 'stderr.log').open('w') as stderr:
                server = subprocess.Popen([sys.executable, '-m', 'control_center', '--port', str(port),
                    '--workers', '1', '--data-dir', str(root / 'data')], cwd=worker_fixtures.ROOT,
                    env=env, stdout=stdout, stderr=stderr)
                try:
                    base = f'http://127.0.0.1:{port}'
                    def call(path, body=None):
                        request = urllib.request.Request(base + path,
                            data=json.dumps(body).encode() if body is not None else None,
                            headers={'Content-Type': 'application/json'})
                        return json.load(urllib.request.urlopen(request, timeout=5))
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            health = call('/api/health')
                            break
                        except OSError:
                            if time.monotonic() >= deadline or server.poll() is not None:
                                self.fail((root / 'stderr.log').read_text())
                            time.sleep(.1)
                    self.assertTrue(health['configuration']['debug_llm_prompts'])
                    self.assertEqual(health['configuration']['configuration_source'], 'environment')
                    agent = call('/api/agents', {'name': 'Entry point integration',
                        'instructions': 'Respond Hola Freya', 'tools': [], 'config': {
                            'endpoint': provider.url, 'model': 'fixture', 'verification': {'enabled': False}}})
                    task = call(f"/api/agents/{agent['id']}/tasks", {'prompt': 'Hola Freya'})
                    while task['status'] not in {'Success', 'Failed', 'Cancelled'}:
                        if time.monotonic() >= deadline:
                            self.fail('Spawned worker did not finish: ' + str(task))
                        time.sleep(.1)
                        task = call('/api/tasks/' + task['id'])
                    self.assertEqual(task['status'], 'Success', task.get('error'))
                    calls = [e for e in task['events'] if e['event_type'] == 'llm.call']
                    self.assertTrue(calls)
                    for event in calls:
                        self.assertTrue(event['debug_prompts_enabled'])
                        self.assertTrue(event['request_body']['messages'])
                        self.assertTrue(event['raw_response'])
                    startup = json.loads((root / 'stdout.log').read_text().splitlines()[0])
                    self.assertEqual(startup['event_type'], 'freya.runtime.configuration')
                    self.assertEqual(startup['repo_root'], str(worker_fixtures.ROOT))
                finally:
                    server.terminate()
                    server.wait(timeout=10)


class InformationGroundingTests(unittest.TestCase):
    def test_multiline_audit_prompt_survives_analysis_and_persistence(self):
        prompt = 'Create converter.py in Python.\nRead console input.\nRun with 0, 100 and abc.'
        candidate = _analyst_response(deterministic_task_spec(prompt))
        analyst = TaskSpecAnalyst(request=lambda *_a, **_k: {'message': {'content': json.dumps(candidate)}})
        spec = analyst.analyze_spec(prompt)
        self.assertEqual(spec['source_prompt'], prompt)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            run = store.create_orchestration(prompt, {})
            store.transition_orchestration(run['id'], ('Queued',), 'Analyzing')
            saved = store.save_task_spec(run['id'], spec)
            self.assertEqual(saved['status'], 'Planning')
            self.assertEqual(saved['task_spec']['source_prompt'], prompt)

    def test_failed_spec_persistence_finishes_instead_of_leaving_analyzing(self):
        prompt = 'Create converter.py in Python for console input.'
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            orchestrator = Orchestrator(store, Mock(), task_analyst=TaskSpecAnalyst(offline=True))
            run = store.create_orchestration(prompt, {})
            with patch.object(store, 'save_task_spec', side_effect=ValueError('Persistence rejected')):
                orchestrator._run(run['id'])
            saved = store.get_orchestration(run['id'])
            self.assertEqual(saved['status'], 'Failed')
            self.assertIn('Persistence rejected', saved['error'])

    def test_python_script_named_is_faithful_information(self):
        self.assertEqual(_unsupported_scope_terms('A Python script named converter.py.',
            'Creá un archivo converter.py en Python.', set()), [])

    def test_console_input_is_faithful_information(self):
        self.assertEqual(_unsupported_scope_terms('Read a temperature from console input.',
            'pedir una temperatura por consola', set()), [])

    def test_new_stack_and_artifact_are_rejected(self):
        facts = _unsupported_scope_terms('Create a React web application with a PostgreSQL database.',
            'crear conversor', set())
        self.assertIn('technology:react', facts)
        self.assertIn('technology:postgresql', facts)
        self.assertIn('artifact:other.py', _unsupported_scope_terms('Create other.py', 'Create converter.py', set()))

    def test_analyst_accepts_paraphrase_without_repair(self):
        prompt = 'Creá un archivo converter.py en Python. Pedir una temperatura por consola.'
        candidate = _analyst_response(deterministic_task_spec(prompt))
        candidate['deliverables'] = [{'description': 'A Python script named converter.py.', 'source': 'explicit'}]
        candidate['requirements'] = [{'description': 'Read a temperature from console input.', 'source': 'explicit'}]
        analyst = TaskSpecAnalyst(request=lambda *_a, **_k: {'message': {'content': json.dumps(candidate)}})
        spec = analyst.analyze_spec(prompt)
        self.assertEqual(spec['status'], 'READY_FOR_PLANNING')
        self.assertEqual(analyst.metrics['model_calls'], 1)
        self.assertFalse(analyst.metrics['fallback_used'])

    def test_unchanged_repair_logs_information_and_stops(self):
        prompt = 'Creá un conversor por consola.'
        candidate = _analyst_response(deterministic_task_spec(prompt))
        candidate['requirements'].append({'description': 'Create a React web application with PostgreSQL database.',
                                          'source': 'explicit'})
        analyst = TaskSpecAnalyst(request=lambda *_a, **_k: {'message': {'content': json.dumps(candidate)}})
        analyst.analyze_spec(prompt)
        self.assertEqual(analyst.metrics['model_calls'], 2)
        self.assertTrue(analyst.metrics['repair_made_no_meaningful_change'])
        diagnostic = analyst.metrics['initial_validation_error']
        self.assertIn('statement', diagnostic)
        self.assertIn('grounded_information', diagnostic)
        self.assertIn('technology:react', diagnostic['new_information_detected'])


class CaseContractTests(unittest.TestCase):
    def test_case_contract_cannot_be_lost_on_a_merged_implementation_task(self):
        proposal = converter_plan()
        creation = proposal['tasks'][0]
        creation['verification_cases'] = [{'id': 'creation-check', 'input': '',
                                          'supports_criteria': creation['success_criteria']}]
        with self.assertRaisesRegex(PlanValidationError, 'testing tasks only'):
            compile_semantic_plan(proposal, deterministic_task_spec(CONVERTER_REQUEST))

    def test_case_links_are_resolved_after_merge_and_cannot_invent_criteria(self):
        proposal = converter_plan()
        checks = ['The script executes successfully with temperature 0.',
                  'The script executes successfully with temperature 100.',
                  'The script handles an invalid input gracefully.']
        proposal['tasks'][2]['success_criteria'] = checks
        for case, criterion in zip(proposal['tasks'][2]['verification_cases'], checks):
            case['supports_criteria'] = [criterion]
        compiled = compile_semantic_plan(proposal, deterministic_task_spec(CONVERTER_REQUEST))
        self.assertEqual(compiled['task_count'], 2)
        rows = {row['criterion']: row['id'] for row in compiled['criterion_links']['local']}
        for case, criterion in zip(compiled['tasks'][1]['verification_cases'], checks):
            self.assertEqual(case['supports_acceptance_criterion_ids'], [rows[criterion]])
        self.assertEqual(validate_plan(compiled), compiled)
        schema = semantic_plan_response_format({})['properties']['tasks']['items']['anyOf'][1]
        fields = schema['properties']['verification_cases']['items']['properties']
        self.assertIn('supports_criteria', fields)
        self.assertNotIn('supports_acceptance_criterion_ids', fields)
        for fields in ({'supports_criteria': ['Invented criterion.']},
                       {'supports_acceptance_criterion_ids': ['LC-900']},
                       {'capabilities': ['execution.python_script']}):
            with self.subTest(fields=fields), self.assertRaises(PlanValidationError):
                invalid = converter_plan()
                invalid['tasks'][2]['verification_cases'][0].update(fields)
                compile_semantic_plan(invalid, deterministic_task_spec(CONVERTER_REQUEST))
        compiled['tasks'][1]['verification_cases'][0]['supports_acceptance_criterion_ids'] = ['LC-900']
        with self.assertRaises(PlanValidationError):
            validate_plan(compiled)

    def test_case_links_do_not_normalize_input_or_accept_invalid_metadata(self):
        case = {'id': 'session', 'input': ' 0\n\n 100\n', 'supports_criteria': ['A specific check.']}
        self.assertEqual(normalize_cases([case]), [case])
        for refs in ('not an array', [None], [''], ['x' * 1001]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                normalize_cases([{**case, 'supports_criteria': refs}])

    def test_grouped_sibling_cases_keep_their_own_criterion_links(self):
        tasks = [{'key': f'case_{index}', 'objective': 'Run converter.py', 'description': 'Run converter.py',
            'task_kind': 'testing', 'verification_mode': 'independent_cases',
            'verification_cases': [{'id': f'input-{index}', 'input': value,
                                    'supports_criteria': [f'Case {index} is correct.']}],
            'operations': ['run_python_script'], 'depends_on': ['implement'],
            'owned_paths': [], 'write_targets': [], 'success_criteria': [f'Case {index} is correct.']}
            for index, value in enumerate(['0\n', '100\n', 'abc\n'])]
        grouped, _, _ = group_case_tasks(tasks, [task['key'] for task in tasks])
        self.assertEqual([case['supports_criteria'] for case in grouped[0]['verification_cases']],
                         [[f'Case {index} is correct.'] for index in range(3)])
    def test_compiler_groups_cases_and_rewires_dependent_review(self):
        spec = deterministic_task_spec('Review existing converter.py in Python. Run with 0, 100 and abc.')
        tasks = [{'key': f'case-{index}', 'task_kind': 'testing',
                  'objective': 'Run converter.py', 'description': 'Run converter.py',
                  'depends_on': [], 'semantic_needs': ['Run Python'],
                  'operations': ['run_python_script'], 'owned_paths': [], 'write_targets': [],
                  'success_criteria': [f'The program handles input {value}.'],
                  'verification_mode': 'independent_cases',
                  'verification_cases': [{'id': f'input-{index}', 'input': value + '\n'}]}
                 for index, value in enumerate(['0', '100', 'abc'])]
        tasks.append({'key': 'review', 'task_kind': 'review', 'objective': 'Review converter.py',
                      'description': 'Inspect converter.py', 'depends_on': ['case-2'],
                      'semantic_needs': ['Read file'], 'operations': ['read_file'],
                      'owned_paths': [], 'write_targets': [],
                      'success_criteria': ['The converter.py content is reviewed.']})
        plan = compile_semantic_plan({'summary': 'Review', 'tasks': tasks,
            'success_criteria': [criterion for task in tasks for criterion in task['success_criteria']],
            'unsupported_requirements': []}, spec)
        self.assertEqual(len(plan['tasks']), 2)
        self.assertEqual([case['input'] for case in plan['tasks'][0]['verification_cases']],
                         ['0\n', '100\n', 'abc\n'])
        self.assertEqual(len(plan['tasks'][0]['success_criteria']), 3)
        self.assertEqual(plan['tasks'][1]['depends_on'], [plan['tasks'][0]['id']])

    def test_equivalent_case_tasks_group_without_changing_command_authority(self):
        tasks = [{'key': f'case_{index}', 'objective': 'Run converter.py', 'description': 'Run converter.py',
            'task_kind': 'testing', 'verification_mode': 'independent_cases',
            'verification_cases': [{'id': f'input-{index}', 'input': value}],
            'operations': ['run_python_script'], 'depends_on': ['implement'],
            'owned_paths': [], 'write_targets': [], 'success_criteria': [f'Case {index} is correct.']}
            for index, value in enumerate(['0\n', '100\n', 'abc\n'])]
        grouped, keys, audit = group_case_tasks(tasks, [t['key'] for t in tasks])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped[0]['verification_cases']), 3)
        self.assertEqual(grouped[0]['operations'], ['run_python_script'])
        self.assertEqual(len(grouped[0]['success_criteria']), 3)
        self.assertEqual(audit[0]['source_task_keys'], ['case_0', 'case_1', 'case_2'])
        tasks[1]['description'] = tasks[1]['objective'] = 'Run other.py'
        self.assertEqual(len(group_case_tasks(tasks, [t['key'] for t in tasks])[0]), 2)
        tasks[2]['depends_on'] = ['case_0']
        self.assertEqual(len(group_case_tasks(tasks, [t['key'] for t in tasks])[0]), 3)

    def test_planner_schema_and_compiler_preserve_case_identity(self):
        properties = semantic_plan_response_format({})['properties']['tasks']['items']['anyOf'][1]['properties']
        self.assertIn('verification_cases', properties)
        spec = deterministic_task_spec('Create converter.py in Python for console input. Run with 0, 100 and abc.')
        cases = normalize_cases([{'id': 'zero', 'input': '0\n'}, {'id': 'hundred', 'input': '100\n'},
                                 {'id': 'invalid', 'input': 'abc\n'}])
        planned = {'key': 'verify', 'task_kind': 'testing', 'objective': 'Verify',
            'description': 'Run independent inputs', 'depends_on': [], 'semantic_needs': ['Run Python'],
            'operations': ['run_python_script'], 'owned_paths': [], 'write_targets': [],
            'success_criteria': ['The program handles the three requested cases.'],
            'verification_mode': 'independent_cases', 'verification_cases': cases}
        plan = compile_semantic_plan({'summary': 'Verify', 'tasks': [planned],
            'success_criteria': planned['success_criteria'], 'unsupported_requirements': []}, spec)
        self.assertEqual(plan['tasks'][0]['verification_cases'], cases)
        self.assertEqual(plan['tasks'][0]['required_tools'], ['run_command'])
        # Older Planner output can omit the fields. Canonical intent supplies
        # the inputs to its sole compatible verifier without a model repair.
        planned.pop('verification_cases')
        planned.pop('verification_mode')
        planned['description'] = 'Run the requested verification.'
        plan = compile_semantic_plan({'summary': 'Verify', 'tasks': [planned],
            'success_criteria': planned['success_criteria'], 'unsupported_requirements': []}, spec)
        self.assertEqual([case['input'] for case in plan['tasks'][0]['verification_cases']],
                         ['0\n', '100\n', 'abc\n'])

    def test_independent_inputs_are_not_a_multiline_session(self):
        cases = explicit_cases('Run script with 0, 100 and abc.')
        calls, expanded = expand_case_calls([{'function': {'name': 'run_command',
            'arguments': {'argv': ['python', 'converter.py'], 'stdin': '0\n100\nabc'}}}], cases)
        self.assertTrue(expanded)
        self.assertEqual([c['function']['arguments']['stdin'] for c in calls], ['0\n', '100\n', 'abc\n'])
        self.assertEqual(explicit_cases('One session: username, password, confirmation'), [])
        self.assertEqual([c['input'] for c in explicit_cases(
            'Run script with temperatures 0, 100, and an invalid input to verify functionality.')],
            ['0\n', '100\n', 'abc\n'])
        self.assertEqual(explicit_cases('Run with 0, then display 100 rows and 2 decimals.'), [])
        self.assertEqual([c['input'] for c in explicit_cases(
            'Mostrar con 2 decimales. Ejecutar con 0, 100 y una entrada inválida para verificar.')],
            ['0\n', '100\n', 'abc\n'])
        self.assertEqual(explicit_cases('Run with 0, 100. Run with 3, 5.'), [])

    def test_duplicate_ids_and_oversized_inputs_fail_closed(self):
        for cases in ([{'id': 'a', 'input': '0'}, {'id': 'a', 'input': '1'}],
                      [{'input': 'x' * 16001}], [{'input': '\x00'}]):
            with self.assertRaises(ValueError): normalize_cases(cases)

    def test_inspection_before_case_command_is_preserved(self):
        calls, expanded = expand_case_calls([
            {'function': {'name': 'read_file', 'arguments': {'path': 'converter.py'}}},
            {'function': {'name': 'run_command', 'arguments': {'argv': ['python', 'converter.py']}}}],
            normalize_cases([{'input': '0\n'}, {'input': '100\n'}]))
        self.assertTrue(expanded)
        self.assertEqual([c['function']['name'] for c in calls], ['read_file', 'run_command', 'run_command'])


class CaseRuntimeTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    run_worker = worker_fixtures.WorkerTests.run_worker

    def test_temperature_converter_plan_worker_venv_final_state_and_evaluation(self):
        """Real Worker/venv processes with injected Planner and Evaluator replies."""
        proposal = converter_plan()
        exists = 'The file `temperature_converter.py` is created in the workspace.'
        checks = ['The script executes successfully with temperature 0.',
                  'The script executes successfully with temperature 100.',
                  'The script handles an invalid input gracefully.']
        proposal['tasks'][0]['success_criteria'] = [exists]
        proposal['success_criteria'] = [exists]
        proposal['tasks'][2]['success_criteria'] = checks
        for case, criterion in zip(proposal['tasks'][2]['verification_cases'], checks):
            case['supports_criteria'] = [criterion]
        compiled = compile_semantic_plan(proposal, deterministic_task_spec(CONVERTER_REQUEST))
        self.assertEqual((compiled['task_count'], compiled['worker_count']), (2, 1))
        implementation, qa = compiled['tasks']
        self.assertEqual(implementation['semantic_operations'], ['create_file', 'modify_file'])
        self.assertEqual(qa['depends_on'], [implementation['id']])
        source = ('try:\n    celsius = float(input("Celsius: "))\n'
                  '    print(f"{celsius * 9 / 5 + 32:.2f}")\n'
                  'except ValueError:\n    print("Invalid input")\n')
        creation = self.run_worker([answer(calls=[('write_file', {
            'path': 'temperature_converter.py', 'content': source})]), answer('Implemented')],
            config={'verification': {'enabled': False}}, prompt=implementation['description'])
        self.assertEqual(creation['status'], 'Success', creation.get('error'))
        execution = self.run_worker([answer(calls=[('run_command', {
            'argv': ['python', 'temperature_converter.py']})])],
            tools=qa['required_tools'], config={'permissions': 'execute',
            'verification_mode': qa['verification_mode'], 'verification_cases': qa['verification_cases'],
            'verification': {'enabled': False},
            'output': {'format': 'structured', 'include': ['summary', 'actions', 'artifacts',
                                                          'verification', 'limitations']}},
            prompt=qa['description'])
        self.assertEqual(execution['status'], 'Success', execution.get('error'))
        self.assertEqual(execution['model_calls'], 1)
        execution['id'] = 'runtime-qa'
        execution['source_task_id'] = qa['id']
        rows = compiled['criterion_links']['local']
        planned = {**qa, 'success_criteria': [row['criterion'] for row in rows],
                   'acceptance_criteria': [{'id': row['id'], 'criterion': row['criterion']} for row in rows],
                   'write_targets': ['temperature_converter.py']}
        state = build_final_state(planned, [execution], str(self.workspace))
        facts = state['verification_facts']
        self.assertEqual([fact['case_id'] for fact in facts], ['case_0', 'case_100', 'case_invalid'])
        self.assertEqual([fact['exit_code'] for fact in facts], [0, 0, 0])
        for fact, output in zip(facts, ['32.00', '212.00', 'Invalid input']):
            self.assertIn(output, fact['stdout'])
            self.assertTrue(fact['program_started'])
            self.assertTrue(fact['environment_available'])
            self.assertIn('supports_acceptance_criterion_ids', fact)
        reviewed = []
        def review(_prompt, context):
            reviewed.append(context)
            self.assertEqual(context['planned_task']['success_criteria'], [rows[0]['criterion'], checks[2]])
            group = next(group for group in context['semantic_criteria'] if group['criterion'] == checks[2])
            invalid = next(fact for fact in group['evidence'] if fact.get('case_id') == 'case_invalid')
            refs = [fact['evidence_id'] for fact in group['evidence']]
            self.assertIn(invalid['evidence_id'], refs)
            self.assertEqual({fact['case_id'] for fact in group['evidence'] if fact.get('case_id')},
                             {'case_invalid'})
            self.assertIn('Invalid input', invalid['stdout'])
            source = next(fact for item in context['semantic_criteria'] for fact in item['evidence']
                          if item['criterion'] == rows[0]['criterion'] and fact.get('type') == 'file_readback')
            self.assertIn('ValueError', source['content'])
            return {'criteria': [{'criterion': criterion, 'status': 'satisfied',
                    'reason': 'Current source and direct case output satisfy the criterion.',
                    'evidence': [invalid['evidence_id']], 'confidence': 1.0}
                    for criterion in context['planned_task']['success_criteria']]}
        outcome = Evaluator(review).evaluate(planned_task=planned,
                    runtime_task={**execution, 'final_state': state}, execution_node={})
        self.assertEqual(outcome['status'], 'accepted')
        details = {row['criterion']: row for row in outcome['criterion_details']}
        for criterion in [exists, *checks[:2]]:
            self.assertEqual(details[criterion]['decision_source'], 'deterministic')
        self.assertEqual(details[checks[2]]['decision_source'], 'semantic')
        self.assertEqual((outcome['metrics']['model_calls'], len(reviewed)), (1, 1))

    def test_fenced_code_without_action_gets_one_tool_correction(self):
        result = self.run_worker([answer('```python\nprint("hello")\n```'),
            answer(calls=[('write_file', {'path': 'hello.py', 'content': 'print("hello")\n'})]),
            answer(json.dumps({'summary': 'Created', 'actions': [], 'artifacts': [],
                               'verification': {}, 'limitations': []}))],
            config={'verification': {'enabled': False}},
            task_characteristics={'requires_filesystem_write': True}, prompt='Create hello.py in Python.')
        self.assertEqual(result['status'], 'Success', result.get('error'))
        self.assertEqual((self.workspace / 'hello.py').read_text(), 'print("hello")\n')
        self.assertEqual(sum(e.get('event', {}).get('event_type') == 'worker.unexecuted_code_repair'
                             for e in self.events), 1)

    def test_repeated_fenced_code_never_becomes_a_workspace_write(self):
        result = self.run_worker([answer('```python\nprint("hello")\n```')]*3,
            config={'verification': {'enabled': False}},
            task_characteristics={'requires_filesystem_write': True}, prompt='Create hello.py in Python.')
        self.assertEqual(result['status'], 'Failed')
        self.assertIn('ExpectedWorkspaceMutationNotObserved', result['error'])
        self.assertFalse((self.workspace / 'hello.py').exists())
        self.assertEqual(sum(e.get('event', {}).get('event_type') == 'worker.unexecuted_code_repair'
                             for e in self.events), 1)

    def test_worker_budget_excludes_blocked_approval_handler(self):
        clock = [0]
        def approval(_request):
            clock[0] += 30
            return 'approved_once'
        replies = iter([answer(calls=[('write_file', {'path': 'approved.txt', 'content': 'approved'})]),
            answer(json.dumps({'summary': 'Done', 'actions': [], 'artifacts': [],
                               'verification': {}, 'limitations': []}))])
        task = {'workspace': str(self.workspace), 'prompt': 'Create approved.txt', 'tools': ['write_file'],
            'config': {**worker_fixtures.DEFAULT_CONFIG, 'max_seconds': 10,
                'verification': {'enabled': False}, 'capability_policy': {'capabilities': {
                    'filesystem': {'create': {'mode': 'ask'}}, 'execution': {}, 'git': {}}}}}
        with patch('control_center.worker.time.monotonic', side_effect=lambda: clock[0]):
            result = worker_fixtures.run_task(task, self.root, self.events.append, lambda: None,
                transport=lambda *_a, **_k: next(replies), approval_handler=approval)
        self.assertEqual(result['status'], 'Success', result.get('error'))
        event = next(item['event'] for item in self.events
                     if item.get('event', {}).get('event_type') == 'task.approval_wait_completed')
        self.assertEqual(event['duration_seconds'], 30)
        self.assertEqual((self.workspace / 'approved.txt').read_text(), 'approved')

    def test_three_fresh_processes_one_model_decision_and_granular_facts(self):
        (self.workspace / 'converter.py').write_text(
            "value=input()\nif value=='100': raise ValueError('case two')\nprint(value)\n")
        cases = normalize_cases([{'id': 'zero', 'input': '0\n'}, {'id': 'hundred', 'input': '100\n'},
                                 {'id': 'invalid', 'input': 'abc\n'}])
        result = self.run_worker([answer(calls=[('run_command', {'argv': ['python', 'converter.py'],
            'stdin': '0\n100\nabc'})])], tools=['run_command'], config={
                'permissions': 'execute', 'verification_mode': 'independent_cases', 'verification_cases': cases,
                'verification': {'enabled': False}, 'output': {'format': 'structured', 'include':
                    ['summary', 'actions', 'artifacts', 'verification', 'limitations']}},
                prompt='Run the three independent cases.')
        self.assertEqual(result['status'], 'Success', result.get('error'))
        self.assertEqual(result['model_calls'], 1)
        actions = result['result']['actions']
        self.assertEqual(len(actions), 3)
        self.assertEqual([item['case_id'] for item in actions], ['zero', 'hundred', 'invalid'])
        self.assertEqual([item['exit_code'] for item in actions], [0, 1, 0])
        state = build_final_state({}, [result], str(self.workspace))
        self.assertEqual(state['files'][0]['path'], 'converter.py')
        self.assertTrue(state['files'][0]['readable'])
        facts = state['verification_facts']
        self.assertEqual([fact['status'] for fact in facts], ['passed', 'failed', 'passed'])
        self.assertEqual([fact['input'] for fact in facts], ['0\n', '100\n', 'abc\n'])
        self.assertIn('abc', facts[2]['stdout'])

    def test_explicit_interactive_session_keeps_one_multiline_process(self):
        (self.workspace / 'session.py').write_text('print(input(),input(),input())\n')
        result = self.run_worker([answer(calls=[('run_command', {'argv': ['python', 'session.py'],
            'stdin': 'user\npassword\nyes\n'})]), answer('Session completed')], tools=['run_command'],
            config={'permissions': 'execute', 'verification_mode': 'interactive_session',
                    'verification': {'enabled': False}})
        self.assertEqual(result['status'], 'Success', result.get('error'))
        self.assertEqual(result['tool_calls'], 1)
