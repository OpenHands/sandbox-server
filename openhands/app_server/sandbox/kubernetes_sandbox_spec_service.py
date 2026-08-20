"""Sandbox specs for the kubernetes (agent-sandbox) backend.

With agent-sandbox the pod is defined cluster side by a ``SandboxTemplate``, and
that template also fixes the agent server image. A spec here therefore names the
``SandboxWarmPool`` that references the template rather than naming an image. Offering more than one spec
lets an operator expose several runtimes (for example a plain pool and a
gVisor-isolated one) for users to choose between.
"""

from typing import AsyncGenerator

from fastapi import Request
from pydantic import Field

from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfo
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
    get_agent_server_env,
)
from openhands.app_server.services.injector import InjectorState

DEFAULT_WARM_POOL = 'openhands-pool'


def get_default_sandbox_specs() -> list[SandboxSpecInfo]:
    return [
        SandboxSpecInfo(
            id=DEFAULT_WARM_POOL,
            # The template's container command applies; the claim only injects env.
            command=None,
            initial_env={
                'OPENVSCODE_SERVER_ROOT': '/openhands/.openvscode-server',
                'OH_ENABLE_VNC': '0',
                'LOG_JSON': 'true',
                'OH_CONVERSATIONS_PATH': '/workspace/conversations',
                'OH_BASH_EVENTS_DIR': '/workspace/bash_events',
                'PYTHONUNBUFFERED': '1',
                **get_agent_server_env(),
            },
            working_dir='/workspace/project',
        )
    ]


class KubernetesSandboxSpecServiceInjector(SandboxSpecServiceInjector):
    specs: list[SandboxSpecInfo] = Field(
        default_factory=get_default_sandbox_specs,
        description=(
            'Preset list of sandbox specs. Each id names a SandboxWarmPool in the '
            'configured namespace.'
        ),
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield PresetSandboxSpecService(specs=self.specs)
