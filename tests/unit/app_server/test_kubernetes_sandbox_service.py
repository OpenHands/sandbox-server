"""Tests for KubernetesSandboxService.

Covers the mapping between agent-sandbox custom resources and SandboxInfo:
- claim creation (labels, annotations, injected environment, warm pool selection)
- status derivation from the Sandbox conditions and operating mode
- pause / resume through spec.operatingMode
- lookup by id and by session API key
- delete
No cluster is required: the CustomObjectsApi is mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from openhands.app_server.sandbox.kubernetes_sandbox_service import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SANDBOX_ID_LABEL,
    SPEC_ID_ANNOTATION,
    KubernetesSandboxService,
)
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER, SandboxStatus
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
)


@pytest.fixture
def mock_sandbox_spec_service():
    service = AsyncMock()
    spec = MagicMock()
    spec.id = 'openhands-pool'
    spec.initial_env = {'LOG_JSON': 'true'}
    spec.command = None
    spec.working_dir = '/workspace/project'
    service.get_default_sandbox_spec.return_value = spec
    service.get_sandbox_spec.return_value = spec
    return service


@pytest.fixture
def custom_objects():
    return MagicMock()


@pytest.fixture
def service(mock_sandbox_spec_service, custom_objects):
    return KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool=None,
        claim_name_prefix='oh-agent-server-',
        sandbox_url_pattern='https://{sandbox_id}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        httpx_client=httpx.AsyncClient(),
        max_num_sandboxes=5,
        _custom_objects=custom_objects,
    )


def _claim(sandbox_id='sb1', sandbox_name='sb-1', env=None):
    return {
        'metadata': {
            'name': f'oh-agent-server-{sandbox_id}',
            'labels': {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                SANDBOX_ID_LABEL: sandbox_id,
            },
            'annotations': {SPEC_ID_ANNOTATION: 'openhands-pool'},
            'creationTimestamp': '2026-08-19T10:00:00Z',
        },
        'spec': {'env': env if env is not None else []},
        'status': {'sandboxName': sandbox_name},
    }


def _sandbox(ready=True, operating_mode='Running', reason=None, message=None):
    return {
        'spec': {'operatingMode': operating_mode},
        'status': {
            'conditions': [
                {
                    'type': 'Ready',
                    'status': 'True' if ready else 'False',
                    'reason': reason,
                    'message': message,
                }
            ]
        },
    }


async def test_start_sandbox_creates_claim(service, custom_objects):
    info = await service.start_sandbox()

    custom_objects.create_namespaced_custom_object.assert_called_once()
    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']

    assert body['kind'] == 'SandboxClaim'
    assert body['metadata']['namespace'] == 'agents'
    assert body['metadata']['labels'][MANAGED_BY_LABEL] == MANAGED_BY_VALUE
    assert body['metadata']['labels'][SANDBOX_ID_LABEL] == info.id
    # The warm pool falls back to the spec id when not pinned in config.
    assert body['spec']['warmPoolRef'] == {'name': 'openhands-pool'}

    env = {item['name']: item['value'] for item in body['spec']['env']}
    assert env['LOG_JSON'] == 'true'
    assert env[WEBHOOK_CALLBACK_VARIABLE] == 'http://app-server:3000/api/v1/webhooks'
    assert env[SESSION_API_KEY_VARIABLE]

    # A brand new sandbox is not reachable yet.
    assert info.status == SandboxStatus.STARTING
    assert info.session_api_key is None
    assert info.exposed_urls == []


async def test_start_sandbox_can_skip_session_key_injection(
    mock_sandbox_spec_service, custom_objects
):
    service = KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool='pinned-pool',
        claim_name_prefix='oh-',
        sandbox_url_pattern='https://{sandbox_id}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        httpx_client=httpx.AsyncClient(),
        max_num_sandboxes=5,
        inject_session_key=False,
        _custom_objects=custom_objects,
    )

    await service.start_sandbox()

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    env = {item['name']: item['value'] for item in body['spec']['env']}
    assert SESSION_API_KEY_VARIABLE not in env
    # An explicitly configured pool wins over the spec id.
    assert body['spec']['warmPoolRef'] == {'name': 'pinned-pool'}


async def test_claim_name_is_dns_safe(service, custom_objects):
    await service.start_sandbox(sandbox_id='Mixed_Case.Id')

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert body['metadata']['name'] == 'oh-agent-server-mixed-case-id'
    # The label keeps the id verbatim so lookups stay exact.
    assert body['metadata']['labels'][SANDBOX_ID_LABEL] == 'Mixed_Case.Id'


async def test_get_sandbox_running_exposes_urls_and_key(service, custom_objects):
    service.exposed_ports = [
        type('P', (), {'name': AGENT_SERVER, 'container_port': 8000})()
    ]
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'secret-key'}])
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.RUNNING
    assert info.session_api_key == 'secret-key'
    assert info.exposed_urls[0].url == 'https://sb1.example.com:8000'


async def test_get_sandbox_missing_returns_none(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.get_sandbox('nope') is None


async def test_suspended_sandbox_is_paused(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, operating_mode='Suspended'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.PAUSED
    # A paused sandbox must not advertise a key or URLs.
    assert info.session_api_key is None
    assert info.exposed_urls == []


async def test_failed_sandbox_reports_error_with_detail(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, reason='SandboxFailed', message='ImagePullBackOff'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == SandboxStatus.ERROR
    assert info.status_detail == 'ImagePullBackOff'


async def test_pause_and_resume_patch_operating_mode(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}

    assert await service.pause_sandbox('sb1') is True
    body = custom_objects.patch_namespaced_custom_object.call_args.kwargs['body']
    assert body == {'spec': {'operatingMode': 'Suspended'}}

    assert await service.resume_sandbox('sb1') is True
    body = custom_objects.patch_namespaced_custom_object.call_args.kwargs['body']
    assert body == {'spec': {'operatingMode': 'Running'}}


async def test_pause_unknown_sandbox_returns_false(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.pause_sandbox('nope') is False


async def test_get_sandbox_by_session_api_key(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(sandbox_id='other', env=[]),
            _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'k'}]),
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox_by_session_api_key('k')
    assert info is not None and info.id == 'sb1'

    record = await service.get_sandbox_record_by_session_api_key('k')
    assert record is not None and record.id == 'sb1'

    assert await service.get_sandbox_by_session_api_key('missing') is None


async def test_delete_sandbox(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}

    assert await service.delete_sandbox('sb1') is True
    assert (
        custom_objects.delete_namespaced_custom_object.call_args.kwargs['name']
        == 'oh-agent-server-sb1'
    )

    custom_objects.list_namespaced_custom_object.return_value = {'items': []}
    assert await service.delete_sandbox('sb1') is False
