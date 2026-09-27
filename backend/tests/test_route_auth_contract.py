"""Fail closed if a new API endpoint forgets its authentication gate.

This is a structural guard, not a substitute for the per-role and owner-scope
HTTP/WebSocket integration tests. Public endpoints are kept in a small explicit
allowlist so that adding a route cannot silently make it public.
"""

import ast
from pathlib import Path


API_ROOT = Path(__file__).parents[1] / "app" / "api"

# Public by design: credential entry/session bootstrap and the non-sensitive
# incident classification dictionary. dev-token has its own fail-closed flag
# and remains hidden in production.
PUBLIC_HTTP_ROUTES = {
    ("auth.py", "post", "/login"),
    ("auth.py", "post", "/dev-token"),
    ("http/ekp.py", "get", "/groups"),
    ("http/ekp.py", "get", "/signs"),
}

# Routes that centralize ownership + authentication checks in a shared helper.
DELEGATED_HTTP_AUTH = {
    ("http/sessions.py", "get", "/{session_id}/report"): "_report_data",
    ("http/sessions.py", "get", "/{session_id}/report.csv"): "_report_data",
    ("http/sessions.py", "get", "/{session_id}/report.pdf"): "_report_data",
}

# Each tuple is the exact positional Role allowlist passed to a route's
# require(request, ...). An empty tuple means any authenticated principal, with
# resource ownership checked in the handler. The outer tuple preserves routes
# that intentionally apply more than one gate (e.g. authentication then role).
HTTP_ROLE_GATE_POLICY = {
    ("http/admin.py", "get", "/config.xml"): (("ADMIN",),),
    ("http/admin.py", "get", "/users"): (("ADMIN",),),
    ("http/admin.py", "post", "/users"): (("ADMIN",),),
    ("http/admin.py", "patch", "/users/{user_id}"): (("ADMIN",),),
    ("http/admin.py", "get", "/audit"): (("ADMIN",),),
    ("http/admin.py", "get", "/audit.csv"): (("ADMIN",),),
    ("http/admin.py", "get", "/diagnostics"): (("ADMIN",),),
    ("http/admin.py", "get", "/diagnostics.json"): (("ADMIN",),),
    ("http/admin.py", "get", "/status"): (("ADMIN",),),
    ("http/admin.py", "get", "/backups"): (("ADMIN",),),
    ("http/admin.py", "post", "/backups"): (("ADMIN",),),
    ("http/groups.py", "get", ""): (("ADMIN", "INSTRUCTOR"),),
    ("http/groups.py", "post", ""): (("ADMIN", "INSTRUCTOR"),),
    ("http/groups.py", "patch", "/{group_id}/owner"): (("ADMIN",),),
    ("http/groups.py", "put", "/{group_id}/trainees/{trainee_id}"): (("ADMIN", "INSTRUCTOR"),),
    ("http/groups.py", "get", "/{group_id}/analytics"): (("ADMIN", "INSTRUCTOR"),),
    ("http/groups.py", "post", "/{group_id}/analytics/insight"): (("INSTRUCTOR",),),
    ("http/materials.py", "get", ""): ((), ("ADMIN", "INSTRUCTOR")),
    ("http/materials.py", "post", ""): (("INSTRUCTOR",),),
    ("http/materials.py", "patch", "/{material_id}"): (("INSTRUCTOR",),),
    ("http/materials.py", "delete", "/{material_id}"): (("INSTRUCTOR",),),
    ("http/materials.py", "put", "/{material_id}/assign/{trainee_id}"): (("INSTRUCTOR",),),
    ("http/materials.py", "put", "/{material_id}/assign-group/{group_id}"): (("INSTRUCTOR",),),
    ("http/materials.py", "delete", "/{material_id}/assign/{trainee_id}"): (("INSTRUCTOR",),),
    ("http/materials.py", "post", "/{material_id}/complete"): (("TRAINEE",),),
    ("http/materials.py", "post", "/{material_id}/start"): (("TRAINEE",),),
    ("http/materials.py", "get", "/{material_id}/download"): ((),),
    ("http/scenario_submissions.py", "post", ""): (("TRAINEE",),),
    ("http/scenario_submissions.py", "get", ""): (("ADMIN", "INSTRUCTOR", "TRAINEE"),),
    ("http/scenario_submissions.py", "post", "/{submission_id}/review"): (("ADMIN", "INSTRUCTOR"),),
    ("http/scenarios.py", "post", "/drafts/from-template"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/generate"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/generate-from-description"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "get", "/drafts/{scenario_id}"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "patch", "/drafts/{scenario_id}"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/{scenario_id}/revise"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/{scenario_id}/validate"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/{scenario_id}/grammar-check"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/drafts/{scenario_id}/approve"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "get", ""): (("ADMIN", "INSTRUCTOR", "TRAINEE"),),
    ("http/scenarios.py", "delete", "/{scenario_id}"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "post", "/{scenario_id}/restore"): (("INSTRUCTOR",),),
    ("http/scenarios.py", "get", "/{scenario_id}"): (("ADMIN", "INSTRUCTOR", "TRAINEE"),),
    ("http/sessions.py", "get", "/dds-history"): (("INSTRUCTOR", "TRAINEE"),),
    ("http/sessions.py", "get", "/active"): (("INSTRUCTOR",),),
    ("http/sessions.py", "post", ""): (("INSTRUCTOR",),),
    ("http/sessions.py", "get", "/{session_id}"): ((),),
    ("http/sessions.py", "get", "/{session_id}/checklist"): ((),),
    ("http/sessions.py", "get", "/{session_id}/recording.wav"): (("INSTRUCTOR", "TRAINEE"),),
    ("http/sessions.py", "patch", "/{session_id}/report"): (("INSTRUCTOR",),),
    ("http/sessions.py", "post", "/{session_id}/findings/{index}/review"): (("INSTRUCTOR",),),
    ("http/sessions.py", "post", "/{session_id}/findings"): (("INSTRUCTOR",),),
    ("http/sessions.py", "get", ""): ((),),
    ("http/trainees.py", "get", "/{trainee_id}/certificate.pdf"): ((),),
    ("http/trainees.py", "get", ""): (("ADMIN", "INSTRUCTOR"),),
    ("http/trainees.py", "get", "/{trainee_id}/profile"): ((),),
}

AUTH_SESSION_HTTP_ROUTES = {
    ("auth.py", "post", "/logout"),
    ("auth.py", "get", "/me"),
}

WEBSOCKET_ROLE_POLICY = {
    ("call.py", "/ws/call/{session_id}"): {"INSTRUCTOR", "TRAINEE"},
    ("control.py", "/ws/control/{session_id}"): {"INSTRUCTOR"},
    ("observe.py", "/ws/observe/{session_id}"): {"ADMIN", "INSTRUCTOR"},
    ("station.py", "/ws/station/{session_id}"): {"INSTRUCTOR", "TRAINEE"},
}


def _route_declaration(node: ast.FunctionDef | ast.AsyncFunctionDef):
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        method = decorator.func.attr.lower()
        if method not in {"get", "post", "put", "patch", "delete", "websocket"}:
            continue
        path = decorator.args[0] if decorator.args else None
        if isinstance(path, ast.Constant) and isinstance(path.value, str):
            return method, path.value
    return None


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id if isinstance(call.func, ast.Name) else call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (isinstance(call.func, ast.Name) or isinstance(call.func, ast.Attribute))
    }


def _required_role_gates(node: ast.AST) -> tuple[tuple[str, ...], ...]:
    gates = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "require":
            continue
        roles = tuple(sorted(
            argument.attr
            for argument in call.args[1:]
            if isinstance(argument, ast.Attribute)
            and isinstance(argument.value, ast.Name)
            and argument.value.id == "Role"
        ))
        gates.append(roles)
    return tuple(sorted(gates))


def test_every_http_route_has_an_authentication_gate_or_explicit_public_reason():
    discovered_public: set[tuple[str, str, str]] = set()
    missing: list[str] = []
    discovered_delegated: set[tuple[str, str, str]] = set()

    sources = [*API_ROOT.glob("*.py"), *(API_ROOT / "http").glob("*.py")]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        relative = source.relative_to(API_ROOT).as_posix()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = _route_declaration(node)
            if route is None:
                continue
            method, path = route
            key = (relative, method, path)
            calls = _called_names(node)
            if key in PUBLIC_HTTP_ROUTES:
                discovered_public.add(key)
                continue
            delegated_helper = DELEGATED_HTTP_AUTH.get(key)
            if delegated_helper and delegated_helper in calls:
                discovered_delegated.add(key)
            elif not calls.intersection({"require", "current"}):
                missing.append(f"{relative}:{node.name} ({method.upper()} {path})")

    assert discovered_public == PUBLIC_HTTP_ROUTES, (
        "Public endpoint allowlist drifted; review each newly removed/renamed route "
        f"and keep the allowlist exact. Missing: {PUBLIC_HTTP_ROUTES - discovered_public}; "
        f"unexpected: {discovered_public - PUBLIC_HTTP_ROUTES}"
    )
    assert discovered_delegated == set(DELEGATED_HTTP_AUTH), (
        "Delegated-auth routes drifted; re-check their shared guard: "
        f"missing {set(DELEGATED_HTTP_AUTH) - discovered_delegated}"
    )
    assert not missing, "HTTP routes without require/current authentication gate: " + "; ".join(missing)

    for (relative, _, _), helper_name in DELEGATED_HTTP_AUTH.items():
        tree = ast.parse((API_ROOT / relative).read_text(encoding="utf-8"))
        helper = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == helper_name
        )
        assert "require" in _called_names(helper), (
            f"delegated helper {relative}:{helper_name} must enforce authentication itself"
        )


def test_every_websocket_route_checks_a_principal_before_serving():
    missing: list[str] = []
    for source in (API_ROOT / "ws").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = _route_declaration(node)
            if route is None or route[0] != "websocket":
                continue
            if not _called_names(node) & {"principal_of", "session_socket"}:
                missing.append(f"{source.name}:{node.name} ({route[1]})")

    assert not missing, "WebSocket routes without principal check: " + "; ".join(missing)


def test_session_socket_entry_checks_a_principal():
    """Сокеты занятия делегируют проверку принципала общему входу."""
    tree = ast.parse((API_ROOT / "ws" / "session.py").read_text(encoding="utf-8"))
    entry = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "session_socket"
    )
    assert "principal_of" in _called_names(entry)


def test_http_routes_match_the_reviewed_role_gate_matrix():
    found: dict[tuple[str, str, str], tuple[tuple[str, ...], ...]] = {}
    session_guards: set[tuple[str, str, str]] = set()
    discovered_routes: set[tuple[str, str, str]] = set()
    for source in [*API_ROOT.glob("*.py"), *(API_ROOT / "http").glob("*.py")]:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        relative = source.relative_to(API_ROOT).as_posix()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = _route_declaration(node)
            if route is None:
                continue
            method, path = route
            key = (relative, method, path)
            discovered_routes.add(key)
            if key in HTTP_ROLE_GATE_POLICY:
                found[key] = _required_role_gates(node)
            elif key in AUTH_SESSION_HTTP_ROUTES:
                if "current" in _called_names(node):
                    session_guards.add(key)

    reviewed_routes = (
        set(HTTP_ROLE_GATE_POLICY)
        | set(DELEGATED_HTTP_AUTH)
        | set(PUBLIC_HTTP_ROUTES)
        | AUTH_SESSION_HTTP_ROUTES
    )
    assert discovered_routes == reviewed_routes, (
        "Every HTTP route must be categorized in the reviewed matrix; "
        f"unreviewed={discovered_routes - reviewed_routes}, stale={reviewed_routes - discovered_routes}"
    )
    assert set(found) == set(HTTP_ROLE_GATE_POLICY), (
        "The HTTP role matrix must enumerate every protected route; "
        f"missing={set(HTTP_ROLE_GATE_POLICY) - set(found)}, "
        f"unexpected={set(found) - set(HTTP_ROLE_GATE_POLICY)}"
    )
    differences = {
        key: (HTTP_ROLE_GATE_POLICY[key], found[key])
        for key in HTTP_ROLE_GATE_POLICY
        if HTTP_ROLE_GATE_POLICY[key] != found[key]
    }
    assert not differences, f"HTTP route role-gate drift: {differences}"
    assert session_guards == AUTH_SESSION_HTTP_ROUTES


def test_websocket_routes_match_the_reviewed_role_matrix():
    found: dict[tuple[str, str], set[str]] = {}
    for source in (API_ROOT / "ws").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = _route_declaration(node)
            if route is None or route[0] != "websocket":
                continue
            roles = {
                item.attr for item in ast.walk(node)
                if isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id == "Role"
            }
            found[(source.name, route[1])] = roles

    assert found == WEBSOCKET_ROLE_POLICY, f"WebSocket role policy drift: {found}"


def test_dev_token_remains_runtime_gated():
    source = (API_ROOT / "auth.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dev_token"
    )
    calls_and_names = {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(target) if isinstance(node, ast.Attribute)}
    assert "dev_auth_bypass" in calls_and_names | attributes
    assert "demo_no_db" in calls_and_names | attributes
