"""Tests that RUNTIME=kubernetes selects the agent-sandbox backed services."""

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reset_global_config():
    """Reset the global config before and after each test."""
    import openhands.app_server.config as config_module

    original_config = config_module._global_config
    config_module._global_config = None
    yield
    config_module._global_config = original_config


def _get_clean_env():
    env = {}
    for key in ['PATH', 'HOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'TMPDIR', 'TMP', 'TEMP']:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


@pytest.mark.parametrize('runtime', ['kubernetes', 'k8s'])
def test_kubernetes_runtime_selects_kubernetes_services(runtime):
    from openhands.app_server.config import config_from_env
    from openhands.app_server.sandbox.kubernetes_sandbox_service import (
        KubernetesSandboxServiceInjector,
    )
    from openhands.app_server.sandbox.kubernetes_sandbox_spec_service import (
        KubernetesSandboxSpecServiceInjector,
    )

    env = _get_clean_env()
    env['RUNTIME'] = runtime

    with patch.dict(os.environ, env, clear=True):
        config = config_from_env()

    assert isinstance(config.sandbox, KubernetesSandboxServiceInjector)
    assert isinstance(config.sandbox_spec, KubernetesSandboxSpecServiceInjector)


def test_default_runtime_is_unchanged():
    """The kubernetes branch must not disturb the docker default."""
    from openhands.app_server.config import config_from_env
    from openhands.app_server.sandbox.docker_sandbox_service import (
        DockerSandboxServiceInjector,
    )

    env = _get_clean_env()

    with patch.dict(os.environ, env, clear=True):
        config = config_from_env()

    assert isinstance(config.sandbox, DockerSandboxServiceInjector)
