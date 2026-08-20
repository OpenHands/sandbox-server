"""Sandbox service backed by kubernetes-sigs/agent-sandbox.

Each sandbox is a ``SandboxClaim``; the agent-sandbox controller turns that into a
``Sandbox`` and a pod running the agent server. Compared with the docker service
this gains multi-node scheduling, pause/resume that keeps a persistent volume, and
optional gVisor or Kata isolation. All of that is configured on the cluster side in
a ``SandboxTemplate`` rather than here.

Like the docker service this holds no state of its own: the cluster is the source
of truth. The per-sandbox session API key is injected as an environment variable on
the claim and read back from it, mirroring how the docker service uses container
environment variables.

Requires a ``SandboxWarmPool`` (and the ``SandboxTemplate`` it references) to exist
in the target namespace. The template must allow environment injection
(``envVarsInjectionPolicy: Allowed`` or ``Overrides``) so the session key reaches
the agent server.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator

import base62
import httpx
from fastapi import Request
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, ConfigDict, Field

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxRecord,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import (
    SESSION_API_KEY_VARIABLE,
    WEBHOOK_CALLBACK_VARIABLE,
    SandboxService,
    SandboxServiceInjector,
)
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    resolve_sandbox_spec,
)
from openhands.app_server.services.injector import InjectorState

_logger = logging.getLogger(__name__)

# agent-sandbox custom resources.
CLAIM_GROUP = 'extensions.agents.x-k8s.io'
CLAIM_VERSION = 'v1beta1'
CLAIM_PLURAL = 'sandboxclaims'
SANDBOX_GROUP = 'agents.x-k8s.io'
SANDBOX_VERSION = 'v1beta1'
SANDBOX_PLURAL = 'sandboxes'

# Labels used to find the claims this service owns. Label values allow upper case,
# so the sandbox id can be stored verbatim; resource names cannot, hence the
# separate sanitised claim name.
MANAGED_BY_LABEL = 'app.kubernetes.io/managed-by'
MANAGED_BY_VALUE = 'openhands-app-server'
SANDBOX_ID_LABEL = 'agents.openhands.dev/sandbox-id'
# The spec id is an image name, which is not a valid label value.
SPEC_ID_ANNOTATION = 'agents.openhands.dev/sandbox-spec-id'

_INVALID_NAME_CHARS = re.compile(r'[^a-z0-9-]+')


class ExposedPort(BaseModel):
    """Port within the sandbox pod that should be surfaced as a URL."""

    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


def _default_exposed_ports() -> list[ExposedPort]:
    return [
        ExposedPort(
            name=AGENT_SERVER,
            description='The port on which the agent server runs within the pod',
            container_port=8000,
        ),
        ExposedPort(
            name=VSCODE,
            description='The port on which the VSCode server runs within the pod',
            container_port=8001,
        ),
    ]


@dataclass
class KubernetesSandboxService(SandboxService):
    """Sandbox service built on kubernetes-sigs/agent-sandbox.

    The kubernetes client is synchronous, so calls are dispatched to a worker
    thread to avoid blocking the event loop.
    """

    sandbox_spec_service: SandboxSpecService
    namespace: str
    warm_pool: str | None
    claim_name_prefix: str
    sandbox_url_pattern: str
    webhook_base_url: str
    exposed_ports: list[ExposedPort]
    httpx_client: httpx.AsyncClient
    max_num_sandboxes: int
    web_url: str | None = None
    permitted_cors_origins: list[str] = field(default_factory=list)
    inject_session_key: bool = True
    shutdown_after_seconds: int | None = None
    default_sandbox_spec_id: str | None = None
    _custom_objects: Any = None

    def __post_init__(self) -> None:
        if self._custom_objects is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            self._custom_objects = k8s_client.CustomObjectsApi()

    # ── Naming ────────────────────────────────────────────────────────────────

    def _claim_name(self, sandbox_id: str) -> str:
        """Build an RFC 1123 resource name for a sandbox id."""
        name = _INVALID_NAME_CHARS.sub('-', sandbox_id.lower()).strip('-')
        return f'{self.claim_name_prefix}{name}'

    # ── Kubernetes access (sync, run in a worker thread) ───────────────────────

    def _list_claims_sync(self) -> list[dict]:
        response = self._custom_objects.list_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            label_selector=f'{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}',
        )
        return response.get('items', [])

    def _get_claim_sync(self, sandbox_id: str) -> dict | None:
        response = self._custom_objects.list_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            label_selector=(
                f'{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{SANDBOX_ID_LABEL}={sandbox_id}'
            ),
        )
        items = response.get('items', [])
        return items[0] if items else None

    def _get_sandbox_object_sync(self, sandbox_name: str) -> dict | None:
        try:
            return self._custom_objects.get_namespaced_custom_object(
                group=SANDBOX_GROUP,
                version=SANDBOX_VERSION,
                namespace=self.namespace,
                plural=SANDBOX_PLURAL,
                name=sandbox_name,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _create_claim_sync(self, body: dict) -> dict:
        return self._custom_objects.create_namespaced_custom_object(
            group=CLAIM_GROUP,
            version=CLAIM_VERSION,
            namespace=self.namespace,
            plural=CLAIM_PLURAL,
            body=body,
        )

    def _delete_claim_sync(self, name: str) -> None:
        try:
            self._custom_objects.delete_namespaced_custom_object(
                group=CLAIM_GROUP,
                version=CLAIM_VERSION,
                namespace=self.namespace,
                plural=CLAIM_PLURAL,
                name=name,
            )
        except ApiException as e:
            if e.status != 404:
                raise

    def _set_operating_mode_sync(self, sandbox_name: str, mode: str) -> None:
        self._custom_objects.patch_namespaced_custom_object(
            group=SANDBOX_GROUP,
            version=SANDBOX_VERSION,
            namespace=self.namespace,
            plural=SANDBOX_PLURAL,
            name=sandbox_name,
            body={'spec': {'operatingMode': mode}},
        )

    # ── Conversion ────────────────────────────────────────────────────────────

    def _status_from(
        self, claim: dict, sandbox: dict | None
    ) -> tuple[SandboxStatus, str | None]:
        """Derive the sandbox status and any detail from the custom resources."""
        if sandbox is None:
            # The controller has not created (or has removed) the Sandbox yet.
            return SandboxStatus.STARTING, _claim_condition_message(claim)

        if (sandbox.get('spec') or {}).get('operatingMode') == 'Suspended':
            return SandboxStatus.PAUSED, None

        conditions = (sandbox.get('status') or {}).get('conditions') or []
        for condition in conditions:
            if condition.get('type') != 'Ready':
                continue
            if condition.get('status') == 'True':
                return SandboxStatus.RUNNING, None
            reason = condition.get('reason') or ''
            message = condition.get('message')
            if reason in ('SandboxFailed', 'PodFailed', 'Failed'):
                return SandboxStatus.ERROR, message
            return SandboxStatus.STARTING, message

        return SandboxStatus.STARTING, None

    def _exposed_urls(self, sandbox_id: str, sandbox_name: str) -> list[ExposedUrl]:
        """Build the URLs for a running sandbox.

        ``sandbox_name`` is the Sandbox resource, which is what cluster DNS
        resolves; ``sandbox_id`` is the app-level id, useful for ingress routes
        that key off it.
        """
        return [
            ExposedUrl(
                name=port.name,
                url=self.sandbox_url_pattern.format(
                    sandbox_id=sandbox_id,
                    sandbox_name=sandbox_name,
                    namespace=self.namespace,
                    port=port.container_port,
                ),
                port=port.container_port,
            )
            for port in self.exposed_ports
        ]

    def _session_api_key(self, claim: dict) -> str | None:
        for env in (claim.get('spec') or {}).get('env') or []:
            if env.get('name') == SESSION_API_KEY_VARIABLE:
                return env.get('value')
        return None

    async def _claim_to_sandbox_info(self, claim: dict) -> SandboxInfo | None:
        metadata = claim.get('metadata') or {}
        labels = metadata.get('labels') or {}
        sandbox_id = labels.get(SANDBOX_ID_LABEL)
        if not sandbox_id:
            return None

        sandbox_name = _sandbox_name(claim)
        sandbox = None
        if sandbox_name:
            sandbox = await asyncio.to_thread(
                self._get_sandbox_object_sync, sandbox_name
            )

        status, status_detail = self._status_from(claim, sandbox)

        created_at = utc_now()
        creation_timestamp = metadata.get('creationTimestamp')
        if creation_timestamp:
            try:
                created_at = datetime.fromisoformat(
                    creation_timestamp.replace('Z', '+00:00')
                )
            except ValueError:
                pass

        annotations = metadata.get('annotations') or {}
        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=None,
            sandbox_spec_id=annotations.get(SPEC_ID_ANNOTATION, ''),
            status=status,
            session_api_key=(
                self._session_api_key(claim)
                if status == SandboxStatus.RUNNING
                else None
            ),
            exposed_urls=(
                self._exposed_urls(sandbox_id, sandbox_name or '')
                if status == SandboxStatus.RUNNING
                else []
            ),
            created_at=created_at,
            status_detail=status_detail,
        )

    # ── SandboxService ────────────────────────────────────────────────────────

    async def search_sandboxes(
        self,
        page_id: str | None = None,
        limit: int = 100,
    ) -> SandboxPage:
        claims = await asyncio.to_thread(self._list_claims_sync)
        items = []
        for claim in claims:
            info = await self._claim_to_sandbox_info(claim)
            if info is not None:
                items.append(info)
        items.sort(key=lambda item: item.created_at)

        offset = int(page_id) if page_id else 0
        page = items[offset : offset + limit]
        next_page_id = str(offset + limit) if len(items) > offset + limit else None
        return SandboxPage(items=page, next_page_id=next_page_id)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return None
        return await self._claim_to_sandbox_info(claim)

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        claims = await asyncio.to_thread(self._list_claims_sync)
        for claim in claims:
            if self._session_api_key(claim) == session_api_key:
                return await self._claim_to_sandbox_info(claim)
        return None

    async def get_sandbox_record_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxRecord | None:
        claims = await asyncio.to_thread(self._list_claims_sync)
        for claim in claims:
            if self._session_api_key(claim) != session_api_key:
                continue
            labels = (claim.get('metadata') or {}).get('labels') or {}
            sandbox_id = labels.get(SANDBOX_ID_LABEL)
            if sandbox_id:
                return SandboxRecord(id=sandbox_id, created_by_user_id=None)
        return None

    async def start_sandbox(
        self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None
    ) -> SandboxInfo:
        """Create a SandboxClaim for a new sandbox."""
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        sandbox_spec = await resolve_sandbox_spec(
            sandbox_spec_id,
            self.default_sandbox_spec_id,
            self.sandbox_spec_service,
            _logger,
        )

        if sandbox_id is None:
            sandbox_id = base62.encodebytes(os.urandom(16))

        env_vars = dict(sandbox_spec.initial_env)
        env_vars[WEBHOOK_CALLBACK_VARIABLE] = (
            f'{self.webhook_base_url.rstrip("/")}/api/v1/webhooks'
        )
        for port in self.exposed_ports:
            env_vars[port.name] = str(port.container_port)

        cors_origins: list[str] = []
        if self.web_url:
            cors_origins.append(self.web_url)
        cors_origins.extend(self.permitted_cors_origins)
        seen: set[str] = set()
        for origin in cors_origins:
            if origin not in seen:
                env_vars[f'OH_ALLOW_CORS_ORIGINS_{len(seen)}'] = origin
                seen.add(origin)

        session_api_key = None
        if self.inject_session_key:
            session_api_key = base62.encodebytes(os.urandom(32))
            env_vars[SESSION_API_KEY_VARIABLE] = session_api_key

        # The warm pool selects the SandboxTemplate (and therefore the image); when
        # it is not pinned in config the spec id names the pool, so the UI can offer
        # more than one runtime.
        warm_pool = self.warm_pool or sandbox_spec.id

        claim: dict[str, Any] = {
            'apiVersion': f'{CLAIM_GROUP}/{CLAIM_VERSION}',
            'kind': 'SandboxClaim',
            'metadata': {
                'name': self._claim_name(sandbox_id),
                'namespace': self.namespace,
                'labels': {
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    SANDBOX_ID_LABEL: sandbox_id,
                },
                'annotations': {SPEC_ID_ANNOTATION: sandbox_spec.id},
            },
            'spec': {
                'warmPoolRef': {'name': warm_pool},
                'env': [
                    {'name': name, 'value': value} for name, value in env_vars.items()
                ],
            },
        }
        if self.shutdown_after_seconds is not None:
            shutdown_time = utc_now() + timedelta(seconds=self.shutdown_after_seconds)
            claim['spec']['lifecycle'] = {
                'shutdownTime': shutdown_time.isoformat().replace('+00:00', 'Z'),
                'shutdownPolicy': 'Delete',
            }

        try:
            await asyncio.to_thread(self._create_claim_sync, claim)
        except ApiException as e:
            raise SandboxError(f'Could not create sandbox claim: {e.reason}') from e

        return SandboxInfo(
            id=sandbox_id,
            created_by_user_id=None,
            sandbox_spec_id=sandbox_spec.id,
            status=SandboxStatus.STARTING,
            session_api_key=None,
            exposed_urls=[],
        )

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        return await self._set_operating_mode(sandbox_id, 'Running')

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        return await self._set_operating_mode(sandbox_id, 'Suspended')

    async def _set_operating_mode(self, sandbox_id: str, mode: str) -> bool:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return False
        sandbox_name = _sandbox_name(claim)
        if not sandbox_name:
            # Still being provisioned; there is nothing to suspend or resume yet.
            return mode == 'Running'
        try:
            await asyncio.to_thread(self._set_operating_mode_sync, sandbox_name, mode)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        return True

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        claim = await asyncio.to_thread(self._get_claim_sync, sandbox_id)
        if claim is None:
            return False
        name = (claim.get('metadata') or {}).get('name')
        if not name:
            return False
        await asyncio.to_thread(self._delete_claim_sync, name)
        return True


def _sandbox_name(claim: dict) -> str | None:
    """Name of the Sandbox the controller created for this claim, if any."""
    status = claim.get('status') or {}
    return (status.get('sandbox') or {}).get('name')


def _claim_condition_message(claim: dict) -> str | None:
    """Surface the claim's own condition message while it is still provisioning."""
    for condition in (claim.get('status') or {}).get('conditions') or []:
        if condition.get('status') != 'True':
            return condition.get('message')
    return None


class KubernetesSandboxServiceInjector(SandboxServiceInjector):
    """Dependency injector for kubernetes sandbox services."""

    namespace: str = Field(
        default='default',
        description='Kubernetes namespace holding the warm pool and sandboxes.',
    )
    warm_pool: str | None = Field(
        default=None,
        description=(
            'SandboxWarmPool to claim from. When unset, the sandbox spec id is '
            'used as the pool name, which allows selecting between runtimes.'
        ),
    )
    claim_name_prefix: str = 'oh-agent-server-'
    sandbox_url_pattern: str = Field(
        default='http://{sandbox_name}.{namespace}.svc.cluster.local:{port}',
        description=(
            'URL pattern for reaching a sandbox. Placeholders: {sandbox_name} '
            '(the Sandbox resource, which cluster DNS resolves), {sandbox_id} '
            '(the app-level id), {namespace} and {port}. The default suits an app '
            'server running in the same cluster; browsers need an externally '
            'routable pattern such as https://{sandbox_name}.sandboxes.example.com.'
        ),
    )
    webhook_base_url: str = Field(
        default='http://openhands-app-server.default.svc.cluster.local:3000',
        description=(
            'Base URL of this app server as reachable from a sandbox pod, used '
            'for webhook callbacks.'
        ),
    )
    max_num_sandboxes: int = Field(
        default=10,
        description='Maximum number of sandboxes allowed to run simultaneously',
    )
    inject_session_key: bool = Field(
        default=True,
        description=(
            'Inject a unique session API key into each sandbox. This is required '
            'whenever sandboxes are reachable by more than one user. Injecting '
            'environment variables makes agent-sandbox cold start the pod rather '
            'than take a pre-warmed one, so set this to False only for '
            'single-user deployments whose SandboxTemplate already carries a key.'
        ),
    )
    shutdown_after_seconds: int | None = Field(
        default=None,
        description=(
            'Optional TTL after which the controller deletes the claim; a safety '
            'net against leaked sandboxes.'
        ),
    )
    exposed_ports: list[ExposedPort] = Field(default_factory=_default_exposed_ports)

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        # Defined inline to prevent circular lookup
        from openhands.app_server.config import (
            get_global_config,
            get_httpx_client,
            get_sandbox_spec_service,
        )

        config = get_global_config()

        async with (
            get_httpx_client(state) as httpx_client,
            get_sandbox_spec_service(state) as sandbox_spec_service,
        ):
            yield KubernetesSandboxService(
                sandbox_spec_service=sandbox_spec_service,
                namespace=self.namespace,
                warm_pool=self.warm_pool,
                claim_name_prefix=self.claim_name_prefix,
                sandbox_url_pattern=self.sandbox_url_pattern,
                webhook_base_url=self.webhook_base_url,
                exposed_ports=self.exposed_ports,
                httpx_client=httpx_client,
                max_num_sandboxes=self.max_num_sandboxes,
                web_url=config.web_url,
                permitted_cors_origins=config.permitted_cors_origins,
                inject_session_key=self.inject_session_key,
                shutdown_after_seconds=self.shutdown_after_seconds,
            )
