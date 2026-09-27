"""Tool-spawn MCP policy must be reflected in graph-bound tools, not help docs."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon import constants
from daemon.manager import InstanceManager
from daemon.registry import AgentMetadata, ToolFilter
from daemon.mcp.models import McpToolSchema
from daemon.services.instance_lifecycle import InstanceLifecycleService, _SpawnResult
from daemon.services.mcp_service import McpService
from daemon.tools._tool_registry import scan_tools_for_full_docs
from daemon.tools.instance import _apply_tool_filter, create_instance_tools

from tests.test_spawn_team_members import _patch_heavy_helpers
from tests.unit.test_llm_config_override import create_mock_config, create_mock_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id,full_access,expected_write", [
    ("designer", True, True),
    ("developer", False, False),
    ("worker", False, False),
    ("designer", False, False),
    ("developer", True, True),
    ("worker", True, True),
])
async def test_tool_spawn_binds_policy_filtered_schemas(agent_id, full_access, expected_write):
    """Exercise wrapper → preload → lifecycle → graph with real schema filters.

    The test server's read-only declaration is synthetic; this does not
    assert the live OpenDesign server has that declaration or that any
    particular agent has a full-access opt-out in production metadata.
    """
    manager = create_mock_manager(create_mock_config())
    manager.config.llm.allowed_models = []
    with patch.object(constants, "SYSTEM_DEFAULT_PROJECT_ID", "project"):
        await _spawn_with_policy(manager, agent_id, full_access, expected_write)


async def _spawn_with_policy(manager, agent_id, full_access, expected_write, *, entry_point=None):
    manager._project_repository.get.return_value = SimpleNamespace(project_id="project")
    manager._mcp_server_repository = MagicMock()
    manager._mcp_server_repository.list_mcp_servers.return_value = [
        SimpleNamespace(name="policy-server")
    ]
    manager._mcp_service = McpService(manager)
    service = manager._mcp_service
    service.get_schemas_for_server = AsyncMock(return_value=[
        McpToolSchema(name=name, server_name="policy-server", description=name,
                      input_schema={"type": "object", "properties": {}})
        for name in ("get_file", "write_file")
    ])
    # Simulate a builtin declaring read-only schemas; exercise the real
    # per-agent policy lookup (rather than stubbing its decision).
    builtin_registry = MagicMock()
    builtin_registry.get_by_name.return_value = SimpleNamespace(read_only_tools=True)
    service._get_resilience_config = MagicMock(return_value=SimpleNamespace(
        read_tool_patterns=("get_",), write_tool_patterns=("write_",),
    ))
    service._get_per_server_timeout = MagicMock(return_value=None)
    service._get_tool_call_timeout = MagicMock(return_value=120)
    # is_read_tool strips only the first prefix component; use a simple
    # prefix to keep this test about policy, not classifier naming.
    service._get_tool_name_prefix = MagicMock(return_value="policy")
    manager._lifecycle_service = InstanceLifecycleService(manager, MagicMock())
    manager.engine = MagicMock()
    manager.write_guard = MagicMock()
    manager.shared_meta_kv_repo = MagicMock()
    manager.message_metadata_repo = MagicMock()
    manager._notification_broadcaster = None
    # Bind the production manager entry points on the lightweight fixture.
    manager.spawn_instance = InstanceManager.spawn_instance.__get__(manager)
    manager.ensure_mcp_preloaded = InstanceManager.ensure_mcp_preloaded.__get__(manager)
    manager.spawn_instance_with_mcp = InstanceManager.spawn_instance_with_mcp.__get__(manager)
    metadata = AgentMetadata(
        id=agent_id, name=agent_id.title(), path=Path(f"agents/{agent_id}"),
        tools=ToolFilter(allow=["mcp"]),
        mcp_full_access=["policy-server"] if full_access else [],
    )
    registry = MagicMock()
    registry.resolve_to_id.return_value = agent_id
    registry.get_version.return_value = metadata
    registry.get_resolved.return_value = metadata
    bound = []
    if entry_point == "spawn_councilor":
        manager.config.llm.allowed_models = ["test-model"]
        manager._lifecycle_service._resolve_model_override = MagicMock(
            return_value="test-model"
        )
    manager._instance_repository.get.return_value = SimpleNamespace(project_id="project")
    manager._instance_repository.count_children.return_value = 0

    def capture_graph(**kwargs):
        bound.extend(t.name for t in kwargs["tools"])
        return MagicMock()

    def bind_tools(mgr, iid, aid, version_tag=None):
        tools = service.get_mcp_tools(iid)
        scan_tools_for_full_docs(tools)
        return _apply_tool_filter(tools, aid, [t.name for t in tools], version_tag)

    with patch("daemon.services.mcp_service.create_lazy_mcp_tools",
               side_effect=lambda server_name, schemas, **kw: [
                   SimpleNamespace(name=f"mcp_{server_name}_{s['name']}") for s in schemas
               ]), \
         patch("daemon.mcp.builtin_servers.get_registry", return_value=builtin_registry), \
         patch("daemon.manager.load_and_cache_prompt", return_value=("prompt", 10)), \
         patch("daemon.services.instance_lifecycle._apply_post_cache_appends",
               return_value=("prompt", "en")), \
         patch("daemon.registry.get_registry", return_value=registry), \
         patch("daemon.services.instance_lifecycle.get_registry", return_value=registry), \
         patch("daemon.manager.create_instance_tools", side_effect=bind_tools), \
         patch("daemon.manager.build_instance_graph", side_effect=capture_graph), \
         patch("daemon.services.main_loop_bridge.MainLoopBridge.run_async_no_wait",
               side_effect=lambda coro: coro.close() or False), \
         patch("daemon.tools.instance._check_team_membership", return_value=None), \
         patch.object(InstanceLifecycleService, "_spawn_instance_db_sync", return_value=_SpawnResult(
             created=True, parent_id=None, agent_id=agent_id, project_id="project",
             created_at=datetime.now(timezone.utc).isoformat(), inherited_source=False,
         )):
        # Registry access inside mcp_service is imported dynamically; the
        # patched daemon.registry.get_registry supplies the policy metadata.
        if entry_point is None:
            result = await manager.spawn_instance_with_mcp(
                agent_id=agent_id, instance_id="12345678-1234-1234-1234-123456789012",
                version_tag=None, return_validated_model=True,
            )
            spawned_id = result[0]
        else:
            # Keep the production tool closures intact; only disable unrelated
            # factory helpers. The real manager wrapper still preloads policy
            # and builds the graph with its resulting MCP tool list.
            factory_patches = _patch_heavy_helpers()
            for factory_patch in factory_patches:
                factory_patch.start()
            try:
                tools = create_instance_tools(
                    manager, "parent-instance-id",
                    "governor" if entry_point == "spawn_councilor" else "leader",
                )
            finally:
                for factory_patch in reversed(factory_patches):
                    factory_patch.stop()
            spawn_tool = next(t for t in tools if t.name == entry_point)
            if entry_point == "spawn_councilor":
                response = await spawn_tool.coroutine(
                    councilor_agent_id=agent_id, model="test-model",
                    initial_message="Inspect the synthetic policy",
                )
            else:
                response = await spawn_tool.coroutine(agent_id=agent_id, project_id="project")
            assert "Successfully spawned" in response, response
            assert len(manager.instances) == 1
            spawned_id = next(iter(manager.instances))
            assert spawned_id in response
    assert "mcp_policy-server_get_file" in bound
    assert ("mcp_policy-server_write_file" in bound) is expected_write
    assert service.get_mcp_tools(spawned_id)
    assert manager.instances[spawned_id][0] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["spawn_instance", "spawn_councilor"])
async def test_agent_facing_spawn_binds_preloaded_policy(entry_point):
    """Tool coroutine → real wrapper → graph for a synthetic read-only server.

    No claim is made about production OpenDesign policy or agent entitlements.
    """
    manager = create_mock_manager(create_mock_config())
    manager.config.llm.allowed_models = []
    with patch.object(constants, "SYSTEM_DEFAULT_PROJECT_ID", "project"):
        await _spawn_with_policy(
            manager, "developer", full_access=False, expected_write=False,
            entry_point=entry_point,
        )


@pytest.mark.asyncio
async def test_wrapper_cleanup_on_failed_spawn():
    """The pre-generated ID's MCP cache is released when spawn rejects."""
    manager = MagicMock()
    manager.instances = {}
    manager._mcp_service.close_connections = AsyncMock()
    manager.ensure_mcp_preloaded = AsyncMock()
    manager.spawn_instance = MagicMock(side_effect=ValueError("Max children"))
    registry = MagicMock()
    registry.resolve_to_id.return_value = "worker"
    registry.get_version.return_value = None
    registry.get_resolved.return_value = None
    with patch("daemon.registry.get_registry", return_value=registry):
        with pytest.raises(ValueError, match="Max children"):
            await InstanceManager.spawn_instance_with_mcp(
                manager, instance_id="12345678-1234-1234-1234-123456789012",
                agent_id="worker",
            )
    manager.ensure_mcp_preloaded.assert_awaited_once()
    manager._mcp_service.close_connections.assert_awaited_once_with(
        "12345678-1234-1234-1234-123456789012"
    )


@pytest.mark.asyncio
async def test_preload_exception_degraded_success():
    """MCP preload failure logs warning and permits spawn with degraded tools.

    Degraded-success contract: if preload_mcp_tools raises, ensure_mcp_preloaded
    suppresses the error so spawn_instance_with_mcp continues to spawn the
    instance without blocking execution.
    """
    manager = create_mock_manager(create_mock_config())
    manager.config.llm.allowed_models = []
    manager._project_repository.get.return_value = SimpleNamespace(project_id="project")
    manager._instance_repository.count_children.return_value = 0
    manager.engine = MagicMock()
    manager.write_guard = MagicMock()
    manager.shared_meta_kv_repo = MagicMock()
    manager.message_metadata_repo = MagicMock()
    manager._notification_broadcaster = None

    manager.spawn_instance = InstanceManager.spawn_instance.__get__(manager)
    manager.ensure_mcp_preloaded = InstanceManager.ensure_mcp_preloaded.__get__(manager)
    manager.spawn_instance_with_mcp = InstanceManager.spawn_instance_with_mcp.__get__(manager)

    manager._mcp_service = MagicMock()
    manager._mcp_service.preload_mcp_tools = AsyncMock(
        side_effect=RuntimeError("MCP server connection timed out")
    )
    manager._mcp_service.get_mcp_tools.return_value = []
    manager._lifecycle_service = InstanceLifecycleService(manager, MagicMock())

    metadata = AgentMetadata(
        id="developer", name="Developer", path=Path("agents/developer"),
        tools=ToolFilter(allow=["mcp"]),
    )
    registry = MagicMock()
    registry.resolve_to_id.return_value = "developer"
    registry.get_version.return_value = metadata
    registry.get_resolved.return_value = metadata

    with patch("daemon.registry.get_registry", return_value=registry), \
         patch("daemon.services.instance_lifecycle.get_registry", return_value=registry), \
         patch("daemon.manager.load_and_cache_prompt", return_value=("prompt", 10)), \
         patch("daemon.services.instance_lifecycle._apply_post_cache_appends",
               return_value=("prompt", "en")), \
         patch("daemon.services.main_loop_bridge.MainLoopBridge.run_async_no_wait",
               side_effect=lambda coro: coro.close() or False), \
         patch("daemon.manager.build_instance_graph", return_value=MagicMock()), \
         patch.object(InstanceLifecycleService, "_spawn_instance_db_sync", return_value=_SpawnResult(
             created=True, parent_id=None, agent_id="developer", project_id="project",
             created_at=datetime.now(timezone.utc).isoformat(), inherited_source=False,
         )):
        result = await manager.spawn_instance_with_mcp(
            agent_id="developer",
            instance_id="12345678-1234-1234-1234-123456789012",
            return_validated_model=True,
        )
        assert result[0] == "12345678-1234-1234-1234-123456789012"
        assert "12345678-1234-1234-1234-123456789012" in manager.instances
