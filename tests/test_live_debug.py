"""Offline regression coverage for live routing, readback and debug fixtures."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pytest

from bpcagent import Config, LLM, LLMResponse, ToolCall
from bpcagent.agents.context import ContextManager, ContextPolicy
from bpcagent.tools.guideline_debug import GuidelineDebugTools
from bpcagent.tools.guideline_library import build_library
from bpcagent.tools.guidelines import GuidelineTools, GuidelineToolConfig
from bpcagent.tools.jev import JevHTTPClient, JevSettings
from bpcagent.workflow.config import WorkflowConfiguration, WorkflowSettings
from bpcagent.workflow.models import DrugCandidate, TargetCandidate


@pytest.mark.parametrize('port', [0, -1, 65536, True, '28374'])
def test_invalid_proxy_ports(port):
    with pytest.raises(ValueError):
        WorkflowSettings(proxy_port=port)


@pytest.mark.parametrize('port', [None, 28374])
def test_model_and_jev_route_without_environment_proxy(port):
    expected = f'http://127.0.0.1:{port}' if port else None
    with patch('httpx.Client') as http, patch('bpcagent.agents.models.OpenAI'):
        LLM(config=Config(model='test', api_key='fake', base_url='https://example.invalid/v1', proxy_port=port))
        assert http.call_args.kwargs['proxy'] == expected
        assert http.call_args.kwargs['trust_env'] is False
    with patch('httpx.Client') as http:
        client = JevHTTPClient(JevSettings(proxy_port=port), 'fake')
        with pytest.raises(Exception):
            client.evaluate({})
        assert http.call_args.kwargs['proxy'] == expected
        assert http.call_args.kwargs['trust_env'] is False


@pytest.mark.parametrize('page_size', [900, 2000])
def test_context_retires_old_readback_without_recursive_archives(page_size):
    manager = ContextManager(ContextPolicy(enabled=True, max_input_tokens=11000, min_compact_chars=2000))
    manager.records['original'] = json.dumps({'value': 'x' * 30000})
    messages = [{'role': 'system', 'content': 'Patient P1; cutoff 100.'}]
    for i in range(10):
        page = manager.read('original', '/value', i * page_size, page_size)
        messages += [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': str(i), 'type': 'function', 'function': {'name': 'read_context_record', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': str(i), 'content': json.dumps(
                {'tool_name': 'read_context_record', 'status': 'success', 'data': page})},
        ]
    original = deepcopy(messages)
    request = manager.prepare(messages, {}, can_read=True)
    assert messages == original
    assert set(manager.records) == {'original'}
    assert request[-1] == messages[-1]  # Newest page is still available to the model.
    retired = json.loads(request[2]['content'])
    assert retired['record_id'] == 'original'
    assert manager.read(retired['record_id'], retired['pointer'], retired['offset'], 2000)['content']
    assert manager.events[-1]['estimated_after'] <= 11000


def test_validated_submit_finishes_without_an_acknowledgement_api_call(tmp_path):
    from bpcagent.workflow.agents import TargetSelectionAgent
    llm = Mock(model='test-model')
    llm.invoke.return_value = LLMResponse(finish_reason='tool_calls', tool_calls=[
        ToolCall(id='submit', name='submit_output', arguments=json.dumps({'candidates': [], 'limitations': ['No tools']})),
        ToolCall(id='after', name='get_shared_patient_state', arguments='{}'),
    ])
    shared = SimpleNamespace(ready=True, read=lambda: {'patient_state': {'evidence_refs': []}},
                             model_view=Mock(side_effect=AssertionError('No tools after submission')))
    agent = TargetSelectionAgent(shared=shared, context={'top_m': 5}, directory=tmp_path,
                                 config=Config(model='test-model', max_tool_iterations=1), llm=llm)
    agent.read_shared = agent.read_context = True
    result = agent.run('Synthetic terminal submission test')
    assert result.finished
    assert llm.invoke.call_count == 1
    assert agent.submitted['candidates'] == []
    tools = [json.loads(m['content']) for m in result.transcript if m['role'] == 'tool']
    assert tools[0]['status'] == 'success'
    assert tools[1]['error']['code'] == 'execution_error'
    assert 'terminal submission' in tools[1]['error']['message']


def test_invalid_submit_cannot_terminate_successfully(tmp_path):
    from bpcagent.workflow.agents import TargetSelectionAgent
    llm = Mock(model='test-model')
    llm.invoke.return_value = LLMResponse(finish_reason='tool_calls', tool_calls=[
        ToolCall(id='submit', name='submit_output', arguments=json.dumps({'candidates': [], 'limitations': []})),
    ])
    agent = TargetSelectionAgent(shared=SimpleNamespace(ready=False), context={'top_m': 5}, directory=tmp_path,
                                 config=Config(model='test-model', max_tool_iterations=1), llm=llm)
    # No shared state/context read: host validation must fail, not terminate.
    result = agent.run('Synthetic invalid submission test')
    assert not result.finished
    assert agent.submitted is None


@pytest.fixture
def debug_owner(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'source.json').write_text(json.dumps([{
        'heading': 'Nonsteroidal antiandrogen therapy',
        'text': 'Synthetic fixture only: enzalutamide. Eligibility is not established.',
    }]))
    config = tmp_path / 'library.json'
    config.write_text(json.dumps({'data_root': 'data', 'sources': [{
        'id': 'nci-prostate-pdq', 'title': 'Synthetic test, not real NCI evidence',
        'version': 'test', 'publication_date': '2026-01-01', 'kind': 'test_document',
        'format': 'sections_json', 'path': 'source.json', 'source_url': 'https://example.invalid',
        'processing_allowed': True, 'external_model_allowed': True, 'permission_basis': 'Synthetic test',
    }]}))
    build_library(config)
    state = {'cancer_context': {'diagnosis': {'value': 'Prostate Cancer', 'evidence_refs': ['E-diagnosis']}}}
    tools = GuidelineTools(GuidelineToolConfig(enabled=True, library_config=str(config)), tmp_path, {})
    owner = SimpleNamespace(read_shared=True, read_context=True, context={'accepted_outputs': {}},
                            guideline_tools=tools, shared=SimpleNamespace(ready=True, read=lambda: {'patient_state': state}))
    return owner, config


def test_debug_scores_provenance_and_target_dependency(debug_owner):
    owner, _ = debug_owner
    tools = GuidelineDebugTools(owner)
    assert tools.run('drug')['candidates'] == []
    result = tools.run('target')
    assert result['clinical_validation'] == 'not_performed'
    candidate = TargetCandidate(**result['candidates'][0]).model_dump()
    tools.validate('target', [candidate])
    assert set(candidate['document_refs']) <= owner.guideline_tools.citable_refs
    altered = {**candidate, 'debug_score': 0.99}
    with pytest.raises(ValueError):
        tools.validate('target', [altered])
    owner.context['accepted_outputs']['TargetSelectionAgent'] = {'candidates': [candidate]}
    drug = DrugCandidate(**tools.run('drug')['candidates'][0]).model_dump()
    tools.validate('drug', [drug])
    assert drug['compatibility'] == 'not_assessed'
    assert drug['debug_only'] is True


def test_debug_tools_cannot_bypass_document_permissions(debug_owner):
    owner, config = debug_owner
    payload = json.loads(config.read_text())
    payload['sources'][0]['external_model_allowed'] = False
    config.write_text(json.dumps(payload))
    with pytest.raises(PermissionError):
        GuidelineDebugTools(owner).run('target')


def test_repository_proxy_reaches_all_model_settings():
    config = WorkflowConfiguration(Path(__file__).parents[1] / 'config/workflow.jsonc')
    model = config.model_configs(environ={'DEEPSEEK_API_KEY': 'fake', 'TYPESAFE_API_KEY': 'fake'})
    assert all(c.proxy_port == 28374 for c in model['agent_configs'].values())
    assert config.workflow_options({})['jev']['proxy_port'] == 28374
