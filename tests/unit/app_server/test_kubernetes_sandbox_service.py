"""Tests for KubernetesSandboxService.

Covers the mapping between agent-sandbox custom resources and SandboxInfo:
- claim creation (labels, annotations, injected environment, warm pool selection)
- status derivation from the Sandbox conditions and operating mode
- pause / resume through spec.operatingMode
- lookup by id and by session API key
- delete
No cluster is required: the CustomObjectsApi is mocked.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes.client.rest import ApiException
from pydantic import ValidationError

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.kubernetes_sandbox_service import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    SANDBOX_ID_LABEL,
    SESSION_KEY_HASH_LABEL,
    SPEC_ID_ANNOTATION,
    KubernetesSandboxService,
    KubernetesSandboxServiceInjector,
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
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
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
        # The controller reports the Sandbox it created under status.sandbox.
        'status': {'sandbox': {'name': sandbox_name}},
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
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
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
    name = body['metadata']['name']
    assert name.startswith('oh-agent-server-mixed-case-id-')
    assert re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', name), name
    # The label keeps the id verbatim so lookups stay exact.
    assert body['metadata']['labels'][SANDBOX_ID_LABEL] == 'Mixed_Case.Id'


async def test_sanitised_ids_do_not_collide(service, custom_objects):
    """Ids that sanitise to the same string must still get distinct claims."""
    await service.start_sandbox(sandbox_id='abc_def')
    first = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    await service.start_sandbox(sandbox_id='abc.def')
    second = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']

    assert first['metadata']['name'] != second['metadata']['name']


async def test_duplicate_id_is_reported_as_such(service, custom_objects):
    custom_objects.create_namespaced_custom_object.side_effect = ApiException(
        status=409, reason='Conflict'
    )

    with pytest.raises(SandboxError, match='already exists'):
        await service.start_sandbox(sandbox_id='dupe')


async def test_search_fans_out_claim_conversions(service, custom_objects):
    """Every claim is converted; the Sandbox reads happen concurrently."""
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [
            _claim(sandbox_id='sb1', sandbox_name='sb-1'),
            _claim(sandbox_id='sb2', sandbox_name='sb-2'),
        ]
    }
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    page = await service.search_sandboxes()

    assert sorted(item.id for item in page.items) == ['sb1', 'sb2']


async def test_session_key_lookup_filters_server_side(service, custom_objects):
    """The session key is looked up through a label selector, not a full scan."""
    key = 'secret-key'
    claim = _claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': key}])
    custom_objects.list_namespaced_custom_object.return_value = {'items': [claim]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    info = await service.get_sandbox_by_session_api_key(key)

    assert info is not None and info.id == 'sb1'
    selector = custom_objects.list_namespaced_custom_object.call_args.kwargs[
        'label_selector'
    ]
    assert SESSION_KEY_HASH_LABEL in selector
    assert key not in selector, 'the raw key must not be sent as a label value'


async def test_session_key_hash_collision_does_not_authenticate(
    service, custom_objects
):
    """A claim returned by the label filter still has to match the real key."""
    custom_objects.list_namespaced_custom_object.return_value = {
        'items': [_claim(env=[{'name': SESSION_API_KEY_VARIABLE, 'value': 'other'}])]
    }

    assert await service.get_sandbox_by_session_api_key('secret-key') is None


async def test_start_sandbox_labels_the_session_key_hash(service, custom_objects):
    await service.start_sandbox()

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    env = {item['name']: item['value'] for item in body['spec']['env']}
    labels = body['metadata']['labels']
    assert labels[SESSION_KEY_HASH_LABEL] == KubernetesSandboxService._session_key_hash(
        env[SESSION_API_KEY_VARIABLE]
    )
    # The label is an index, never the secret itself.
    assert labels[SESSION_KEY_HASH_LABEL] != env[SESSION_API_KEY_VARIABLE]


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
    # DNS resolves the Sandbox resource name, not the app-level id.
    assert info.exposed_urls[0].url == 'https://sb-1.example.com:8000'


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
        ready=False, reason='PodFailed', message='ImagePullBackOff'
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


@pytest.mark.parametrize(
    'reason,expected',
    [
        ('PodFailed', SandboxStatus.ERROR),
        ('PodSucceeded', SandboxStatus.ERROR),
        ('SandboxExpired', SandboxStatus.ERROR),
        ('DependenciesNotReady', SandboxStatus.STARTING),
    ],
)
async def test_terminal_ready_reasons(service, custom_objects, reason, expected):
    """The reasons the controller actually emits must not read as STARTING."""
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox(
        ready=False, reason=reason, message='detail'
    )

    info = await service.get_sandbox('sb1')

    assert info is not None
    assert info.status == expected


@pytest.mark.parametrize(
    'bad_id',
    [
        'has,comma',
        'has=equals',
        'a' * 64,
        '',
        '-leading-dash',
    ],
)
async def test_invalid_sandbox_ids_are_rejected(service, custom_objects, bad_id):
    """Ids travel as label values, so a selector-breaking id must not be accepted."""
    with pytest.raises(SandboxError, match='Invalid sandbox id'):
        await service.start_sandbox(sandbox_id=bad_id)

    custom_objects.create_namespaced_custom_object.assert_not_called()


async def test_claim_name_fits_dns_label_limit(service, custom_objects):
    await service.start_sandbox(sandbox_id='s' * 63)

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    name = body['metadata']['name']
    # On a cold start this is also the Sandbox and Service name.
    assert len(name) <= 63, name
    assert re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', name), name


async def test_malformed_page_id_starts_from_the_beginning(service, custom_objects):
    custom_objects.list_namespaced_custom_object.return_value = {'items': [_claim()]}
    custom_objects.get_namespaced_custom_object.return_value = _sandbox()

    page = await service.search_sandboxes(page_id='not-a-number')

    assert [item.id for item in page.items] == ['sb1']


@pytest.mark.parametrize('bad_id', ['has,comma', 'has=equals', 'a' * 64])
async def test_lookups_treat_unusable_ids_as_missing(service, custom_objects, bad_id):
    """An id that would corrupt the selector must 404, not reach the API server."""
    assert await service.get_sandbox(bad_id) is None
    assert await service.pause_sandbox(bad_id) is False
    assert await service.resume_sandbox(bad_id) is False
    assert await service.delete_sandbox(bad_id) is False

    custom_objects.list_namespaced_custom_object.assert_not_called()


async def test_invalid_id_does_not_pause_existing_sandboxes(service, custom_objects):
    """Validation runs before eviction, so a bad request has no side effects."""
    with pytest.raises(SandboxError, match='Invalid sandbox id'):
        await service.start_sandbox(sandbox_id='has,comma')

    custom_objects.patch_namespaced_custom_object.assert_not_called()


async def test_claim_name_fits_limit_with_a_long_prefix(
    mock_sandbox_spec_service, custom_objects
):
    service = KubernetesSandboxService(
        sandbox_spec_service=mock_sandbox_spec_service,
        namespace='agents',
        warm_pool=None,
        claim_name_prefix='p' * 57,
        sandbox_url_pattern='https://{sandbox_name}.example.com:{port}',
        webhook_base_url='http://app-server:3000',
        exposed_ports=[],
        max_num_sandboxes=5,
        _custom_objects=custom_objects,
    )

    await service.start_sandbox(sandbox_id='s' * 63)

    body = custom_objects.create_namespaced_custom_object.call_args.kwargs['body']
    assert len(body['metadata']['name']) <= 63


def test_injector_rejects_a_prefix_that_cannot_fit_the_digest():
    with pytest.raises(ValidationError):
        KubernetesSandboxServiceInjector(claim_name_prefix='p' * 58)


async def test_unknown_spec_id_does_not_pause_existing_sandboxes(
    service, mock_sandbox_spec_service, custom_objects
):
    """An unknown spec id is a hard error, so it must not evict anything first."""
    mock_sandbox_spec_service.get_sandbox_spec.return_value = None

    with pytest.raises(ValueError, match='not found'):
        await service.start_sandbox(sandbox_spec_id='no-such-spec')

    custom_objects.patch_namespaced_custom_object.assert_not_called()
    custom_objects.create_namespaced_custom_object.assert_not_called()
