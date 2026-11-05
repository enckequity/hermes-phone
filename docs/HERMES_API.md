# Hermes plugin API (verified from source)

Source: `https://github.com/NousResearch/hermes-agent` shallow clone at HEAD `ed1afb08`, cloned to
`/tmp/claude-0/-home-user/a7c37ab2-7017-5f22-8673-9872c7f08dba/scratchpad/hermes-agent`.
All paths below are relative to that clone. Line numbers are from that commit. Nothing from the
clone was installed or executed; only files were read.

Conventions: `NOT FOUND` means the symbol or behavior does not exist in the source. Snippets are
verbatim excerpts, shortened with `...` where marked.

---

## 1. `plugin.yaml` manifest

### 1a. Top-level fields

Known fields (`hermes_cli/plugins_manifest.py:32-40`):

```python
_KNOWN_MANIFEST_FIELDS: set[str] = {
    "name", "version", "description", "author", "requires_env", "provides_tools", "provides_hooks",
    "kind", "hooks", "label", "optional_env", "platforms", "external_dependencies",
    "pip_dependencies", "provides_browser_providers", "provides_web_providers",
    "manifest_version", "api_version", "requires_plugins", "python_dependencies", "config_schema",
    "license", "homepage", "tags", "capabilities", "emits", "listens", "hermes", "depends",
    "requires_hermes", "python_runtime", "provides_locales", "requires_auth", "oauth", "config_keys",
}
```

Unknown fields are not fatal: they log a warning (v2+) or a debug line (v1) and the plugin still loads
(`plugins_manifest.py`, `_parse_manifest_v2_fields`, starting at line 162, which computes `unknown = sorted(...)`).

Supported manifest schema: `plugins_manifest.py:42` `SUPPORTED_MANIFEST_VERSION = 2`. An absent
`manifest_version` means 1.

Plugin kinds (`plugins_manifest.py:27`):

```python
_VALID_PLUGIN_KINDS: set[str] = {"standalone", "backend", "exclusive", "platform", "model-provider"}
```

Field parsing (`hermes_cli/plugins_manifest.py:551-586`, `parse_manifest_file`), verbatim excerpt:

```python
        name = data.get("name", plugin_dir.name)            # line 564
        key = f"{prefix}/{plugin_dir.name}" if prefix else name
        ...
            requires_env=data.get("requires_env", []),       # line 573
            provides_tools=data.get("provides_tools", []),
            # ``hooks:`` is the spelling the bundled manifests carried for months; ...
            provides_hooks=data.get("provides_hooks", data.get("hooks", [])), source=source, path=str(plugin_dir),
```

Summary of what the loader accepts:

| Field | Type / format | Source |
|---|---|---|
| `name` | string; defaults to the directory name | `plugins_manifest.py:564` |
| `version` | string (`str(...)`) | `plugins_manifest.py:571` |
| `description` | string | `plugins_manifest.py:572` |
| `author` | string or mapping, displayed via `_display_author` | `plugins_manifest.py:572` |
| `requires_env` | list; item = bare `NAME` string OR mapping with `name` (see 1c) | `plugins_manifest.py:573` |
| `provides_tools` | list of tool names | `plugins_manifest.py:574` |
| `provides_hooks` (alias `hooks`) | list of `VALID_HOOKS` names (see 2c) | `plugins_manifest.py:577` |
| `kind` | one of the five kinds above; default `standalone` | `plugins_manifest.py:27`, `_manifest_kind` near line 529 |
| `requires_hermes` | version spec string, e.g. `">=0.19"` or `">=0.19, <1.0"` | `plugins_manifest.py:578`; validated by `plugin_validate.py:115` |
| `manifest_version` | int; default 1 | `_parse_manifest_v2_fields` |
| `api_version` | int, optional | `_parse_manifest_v2_fields` |
| `requires_plugins` | list of plugin id strings or `{id, version_range}` mappings | `_dependency_entry`, `plugins_manifest.py:123` |
| `python_dependencies` | list of requirement strings | see 1d |
| `config_schema` | mapping of key to spec (see 1b) | `_config_schema_entries`, `plugins_manifest.py:142` |
| `config_keys` | list of dotted paths, at least two segments (e.g. `memory.retaindb`) | `_config_key_path`, `plugins_manifest.py:110` |
| `capabilities` | list, parsed by `_parse_declared_capabilities` | `plugins_manifest.py:574` |
| `license`, `homepage`, `tags` | strings / list | `_parse_manifest_v2_fields` |
| `emits`, `listens` | lists (reserved) | `plugins_manifest.py:580` |
| `provides_locales` | language-pack ids | `parse_provides_locales`, `plugins_manifest.py:422` |
| `platforms` | list (`linux`, `macos`, `windows`), used by the catalog and gating | `plugins/google_meet/plugin.yaml` example |
| `optional_env`, `oauth`, `requires_auth`, `python_runtime`, `external_dependencies`, `pip_dependencies` | listed as known in `_KNOWN_MANIFEST_FIELDS` | `plugins_manifest.py:32-40` |

`provides_middleware` is NOT in `_KNOWN_MANIFEST_FIELDS` (it appears only in the catalog's
`capabilities:` block, see 7), but `plugin_validate.py` does compare `provides_middleware` (see 4).

### 1b. `config_schema` format

`hermes_cli/plugins_manifest.py:44-52`:

```python
_CONFIG_SCHEMA_TYPES: dict[str, tuple] = {
    "str": (str,), "string": (str,), "int": (int,), "integer": (int,), "float": (int, float),
    "number": (int, float), "bool": (bool,), "boolean": (bool,), "list": (list,), "array": (list,),
    "dict": (dict,), "object": (dict,),
    # ``secret`` values live in ``.env`` (see hermes_cli.plugins_settings), so a config.yaml copy is
    # only ever a stray string.
    "secret": (str,),
}
```

Per key the spec is a mapping (`_config_schema_entries`, `plugins_manifest.py:142-159`). Keys the code
reads: `type`, `default`, `required` (bool, checked by `validate_config_schema` at line 206), `description`,
`env` (for `secret`), `secret: true`, `choices` / `enum`. Shape check (`plugin_validate.py:131-161`):
`required` must be a boolean, `type` must be one of the table above (case-insensitive), and each spec must
be a mapping.

Example, shape only (not from a shipped plugin):

```yaml
config_schema:
  timeout:
    type: int
    default: 180
    description: Seconds before giving up
  api_key:
    type: secret
    env: MYPLUGIN_API_KEY
```

Secret storage: `hermes_cli/plugins_settings.py:70-80`. A `type: secret` field is written to `.env`,
never `config.yaml`. The `.env` name is `env:` if set, else `<PLUGIN_ID>_<KEY>` upper-snaked.

Settings path (`hermes_cli/plugins.py:266`, `PluginContext.get_config`) reads
`plugins.entries.<plugin_id>.settings.<key>`, falling back to the legacy `config` subtree.

### 1c. `requires_env` item format

`hermes_cli/plugin_validate.py:164-185`, verbatim core of the check:

```python
def _check_requires_env(report: ValidationReport, manifest: dict) -> None:
    raw = manifest.get("requires_env") or []
    ...
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "")
        else:
            problems.append(f"requires_env[{i}]: must be a string or mapping")
            continue
        if not _UPPER_SNAKE_RE.match(name):
            problems.append(
                f"requires_env[{i}]: {name!r} is not UPPER_SNAKE_CASE"
```

`_UPPER_SNAKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")` (`plugin_validate.py`, module top).

Accepted item shapes:

```yaml
requires_env:
  - MY_TOKEN                      # bare string
  - name: MY_TOKEN                # mapping; "name" is required
    description: Token for the API
    url: https://example.com/keys
    password: true                # optional; also inferred from the *_TOKEN / *_SECRET style suffixes
    category: messaging           # optional, default "messaging" in the platform path
```

Keys recognized by `hermes_cli/config.py:3858-3870` (`_platform_manifest_env_entries`): `name`,
`description`, `url`, `password`, `secret`, `prompt`, `category`. `password` / `secret` mark a value as a
secret.

### 1d. `python_dependencies`

`plugins_manifest.py:181-186` (verbatim comment and call):

```python
    # python_dependencies — validated and surfaced ONLY; never auto-installed.
    pydeps = _manifest_list(
        data, key, "python_dependencies", "a list of requirement strings",
```

Items must be non-empty strings, otherwise they are skipped with a warning
(`plugins_manifest.py`, the same call). Plugins can also declare deps in `pyproject.toml`
(`[project].dependencies`), read by `pm/plugin_declarations.py:118` `read_python_declaration`.
Installation is performed by the `pm` install path, not the manifest parser.

### 1e. How the entry module is found

- **No `entry` or `module` field exists.** NOT FOUND in `_KNOWN_MANIFEST_FIELDS`, in
  `parse_manifest_file`, and in the loader.
- The entry module is **`__init__.py` at the plugin root** (not a `main.py`, not a package
  name lookup). `hermes_cli/plugins_loader.py:814-816`:

  ```python
        init_file = plugin_dir / "__init__.py"
        if not init_file.exists():
            raise FileNotFoundError(f"No __init__.py in {plugin_dir}")
  ```

- It is loaded by path with `spec_from_file_location(module_name, init_file, submodule_search_locations=[str(plugin_dir)])`
  (`plugins_loader.py:828-829`). Module name is `hermes_plugins.<slug>`, where the slug is the manifest key
  with `/` replaced by `__` and `-` by `_` (`plugins_loader.py:789-797`). Relative imports work because the
  plugin dir is the package.
- The function called is `register` (`plugins_loader.py:618-620`):

  ```python
            register_fn = getattr(module, "register", None)
            ...
            register_fn(ctx)
  ```

  A missing `register` sets `loaded.error = "no register() function"` (`plugins_loader.py:617`).
- Discovery roots (`hermes_cli/plugins.py:1-8` docstring): bundled `<repo>/plugins/<name>/`,
  user `~/.hermes/plugins/<name>/`, project `./.hermes/plugins/<name>/` (only with
  `HERMES_ENABLE_PROJECT_PLUGINS`), and pip packages in the entry-point group
  `hermes_agent.plugins` (`hermes_cli/plugins_discovery.py:29`, `ENTRY_POINTS_GROUP = "hermes_agent.plugins"`).
- Manifest file names: `plugin.yaml` or `plugin.yml` (`plugins_discovery.py:165`).
- Category layout: `<root>/<cat>/<name>/plugin.yaml` gives key `cat/name` (`plugins_discovery.py:141-186`).
  Example: `plugins/web/exa/plugin.yaml`.
- Portable packages (`plugin.json`, no `register`) are a separate format (`plugin_validate.py:638`).

---

## 2. The `ctx` object passed to `register(ctx)`

The object is `PluginContext` (`hermes_cli/plugins.py:230`, `class PluginContext(PluginCapabilityCtxMixin)`).
Constructed per plugin at `plugins_loader.py:586` as `ctx = PluginContext(manifest, self)`, then
`register_fn(ctx)` (`plugins_loader.py:620`).

Useful read-only properties: `plugin_id` (`plugins.py:245-247`, registry key), `manifest`, `has_plugin(plugin_id)`
(`plugins.py:250`), `profile_name` (`plugins.py:401`), `session_cwd()` (`plugins.py:597`),
`session_surface()` (`plugins.py:602`), `current_cron_execution()` (`plugins.py:411`),
`state` (`plugins.py:283`, JSON state facade), `llm`, `subagent_lifecycle`, `delegation_backends`.

### 2a. `register_tool`

`hermes_cli/plugins.py:463-466`, verbatim signature:

```python
    def register_tool(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable | None = None, requires_env: list | None = None, is_async: bool = False,
        description: str = "", emoji: str = "", override: bool = False,
    ) -> Optional[PluginRegistration]:
```

- `toolset` is REQUIRED (second positional). Schema is an OpenAI-style `{"name", "description", "parameters"}` dict.
- Override of a built-in needs `override=True` AND `plugins.entries.<plugin_id>.allow_tool_override: true`
  (`plugins.py:472-474`, raises `PluginToolOverrideError`).
- Handler call (`tools/registry.py:983-988`):

  ```python
            kwargs = _kwargs_accepted_by(entry.handler, kwargs)
            ...
                result = _run_async(entry.handler(args, **kwargs))   # is_async
            ...
                result = entry.handler(args, **kwargs)
  ```

  So the handler signature is **`handler(args: dict, **kwargs)`**. The `kwargs` are narrowed to what
  the handler's signature declares (`_kwargs_accepted_by`, `tools/registry.py:1112`). Common kwargs:
  `task_id`, `session_id`, `user_task`, `parent_agent`. Return value: a JSON string (or dict; the
  registry normalizes it).
- `check_fn` gates availability; `requires_env` is a list of env names for the registry;
  `is_async=True` means the handler is a coroutine function.
- Returns a `PluginRegistration` handle, or `None` if shadowed without `override` (`plugins.py:481-483`).

### 2b. `dispatch_tool`

`hermes_cli/plugins.py:676-686`:

```python
    def dispatch_tool(self, tool_name: str, args: dict, **kwargs) -> str:
        """Dispatch a tool call through the registry with the parent agent (when available)
        resolved automatically; returns the handler's JSON string. ``kwargs`` forward to dispatch."""
        from tools.registry import registry
        ...
        return registry.dispatch(tool_name, args, scope=self._manager.scope_key, **kwargs)
```

Signature: `dispatch_tool(tool_name: str, args: dict, **kwargs) -> str`. It returns the handler output;
`registry.dispatch` (`tools/registry.py:972-973`) returns `str | dict`. Unknown tool returns
`tool_error("Unknown tool: ...")`.

### 2c. `register_hook`

`hermes_cli/plugins.py:935-937`:

```python
    def register_hook(self, hook_name: str, callback: Callable, *, hold_stream: bool = False) -> PluginRegistration:
        """Register a lifecycle hook callback (``PluginDispatchMixin.track_hook``)."""
        return self._manager.track_hook(self, hook_name, callback, hold_stream=hold_stream)
```

Valid hook names: `hermes_cli/plugins.py:109` `VALID_HOOKS: set[str] = {...}` (lines 109-204). Complete
list extracted from that literal:

```
agent_loop_stopped api_request_error gateway_platform_event kanban_task_blocked kanban_task_claimed
kanban_task_completed on_human_input_request on_human_input_resolved on_interim_message
on_kanban_dispatch_tick on_kanban_task_updated on_kanban_worker_exited on_kanban_worker_spawned
on_kanban_worker_stale_claim on_room_member_activity on_session_delete on_session_end
on_session_finalize on_session_reset on_session_start on_skill_lifecycle on_stream_delta on_stream_end
on_stream_start post_api_request post_approval_response post_auxiliary_call post_compaction
post_gateway_admission post_llm_call post_tool_call pre_api_request pre_approval_request
pre_auxiliary_call pre_command pre_gateway_dispatch pre_llm_call pre_tool_call pre_transcription
pre_verify subagent_start subagent_stop transform_api_error_classification transform_llm_output
transform_terminal_output transform_tool_result
```

Notes:
- `"hold_stream"` is only meaningful for `transform_llm_output` (`plugins.py:935`, `track_hook` docstring at
  `plugins_dispatch.py:567-570`).
- Unknown names warn but are stored (`plugins_dispatch.py:567-568`); `plugin_dev.py:470` makes an unknown name a
  doctor ERROR.
- `SHELL_UNSUPPORTED_HOOKS = {"transform_api_error_classification"}` (`plugins.py:208`).
- A hook callback must accept `**kwargs` for `plugin doctor` (see 4).

Callback kwargs (`plugins_dispatch.py:250-263`): the payload is filtered to the declared names for narrow
signatures and passed in full to `**kwargs` callbacks:

```python
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            return dict(payload)
```

Invocation entry point: `hermes_cli/lifecycle.py:30` `def invoke_hook(hook_name: str, **kwargs: Any) -> list[Any]`,
which calls `_observe` then `_plugin_hooks`. Dispatch goes through `plugins.py:1800` `invoke_hook`.

#### `pre_tool_call`

Kwargs, `hermes_cli/plugins.py:1923-1928`, verbatim:

```python
    hook_results = invoke_lifecycle_hook(
        "pre_tool_call", tool_name=tool_name, args=args if isinstance(args, dict) else {},
        task_id=task_id, session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id,
        api_request_id=api_request_id, middleware_trace=list(middleware_trace or []),
    )
```

Allowed return values, `plugins.py:1907-1912` docstring plus the loop at `plugins.py:1930-1960`:

- `{"action": "block", "message": "<text>"}`: veto. The `message` becomes the tool result. A block
  without a non-empty message is ignored (`plugins.py`, `if action == "block" and not message: continue`).
- `{"action": "approve", "message": "<optional>", "rule_key": "<optional>"}`: escalate to the human
  approval gate. Precedence is block over approve over none (`plugins.py:1908-1912`).
- `{"action": "modify", "args": {...}}`: shallow-merged into the tool args before dispatch
  (`plugins.py:1937-1941`). This is an extra action not listed in the original question.
- Anything else is ignored.

#### `pre_llm_call`

Kwargs, `agent/turn_context.py:862-872`:

```python
        _pre_results = _invoke_hook(
            "pre_llm_call",
            session_id=agent.session_id,
            task_id=effective_task_id,
            turn_id=turn_id,
            user_message=original_user_message,
            conversation_history=list(messages),
            is_first_turn=(not bool(conversation_history)),
            model=agent.model,
            platform=getattr(agent, "platform", None) or "",
            parent_session_id=getattr(agent, "_parent_session_id", None) or "",
            sender_id=getattr(agent, "_user_id", None) or "",
        )
```

Return shapes, `agent/turn_context.py:885-900`:

- `{"action": "block", "message": "..."}`: veto the turn before inference (appended to `blocks`).
- `{"context": "..."}`: text injected into the **user message** (never the system prompt), spilled to disk if oversized.
- A bare non-empty `str` is also accepted as context (`agent/turn_context.py:897-898`, `elif isinstance(r, str) and r.strip()`).

#### Other hook returns (for reference)

- `pre_verify`: `{"action": "continue", "message"}` or `{"decision": "block", "reason"}` (`plugins.py:116-118`).
- `pre_gateway_dispatch`: `{"action": "skip", "reason"}`, `{"action": "rewrite", "text"}` (`plugins.py:136-137`).
- `transform_llm_output`: return a replacement string, first non-None wins (`plugins.py:113`).
- `transform_api_error_classification`: returns a classification dict (`plugins.py:128-131`); not a shell hook.

### 2d. `register_command` (in-session slash command)

`hermes_cli/plugins.py:649-652`:

```python
    def register_command(
        self, name: str, handler: Callable, description: str = "", args_hint: str = "",
        argument_mode: str | None = None,
    ) -> Optional[PluginRegistration]:
```

Docstring (`plugins.py:650-652`): handler is `fn(raw_args: str) -> str | None` (sync or async). It may also
declare session-context keyword parameters. Invocation is in `hermes_cli/plugin_command_dispatch.py:11-32`:

```python
def invoke_plugin_command(handler: Callable, raw_args: str, **context: Any) -> Any:
    """Call *handler* once with *raw_args* plus the *context* keywords it declares.
    Every surface passes the same context (``source``, ``session_id``, ``session_key``,
    ``platform``); a handler opts in by naming any of them as keyword-capable parameters, or
    takes them all with ``**kwargs``. Legacy ``fn(raw_args)`` handlers and opaque signatures keep
    the one-argument call.
```

So: `def handler(raw_args: str, **kwargs)` or `def handler(raw_args)`. Names are lowercased, leading `/`
stripped, spaces to `-` (`plugins.py:656`). Built-in names are rejected (`plugins.py:663`).

### 2e. `register_cli_command`

`hermes_cli/plugins.py:635-637`:

```python
    def register_cli_command(
        self, name: str, help: str, setup_fn: Callable, handler_fn: Callable | None = None,
        description: str = "",
    ) -> PluginRegistration:
```

- `setup_fn` receives the argparse subparser (`plugins.py:638-639`).
- `handler_fn` becomes `set_defaults(func=...)` (`hermes_cli/main.py:3248-3249`), and is run as
  `rc = args.func(args)` (`hermes_cli/main.py:3547`). So the handler is `handler(args: argparse.Namespace)`.

### 2f. `register_skill`

Defined in `hermes_cli/plugins_content.py:32-35` and bound onto `PluginContext`:

```python
def register_skill(
    self, name: str, path: Path, description: str = "",
    frontmatter: Optional[Mapping[str, Any]] = None,
) -> PluginRegistration:
```

- `name` matches `[a-zA-Z0-9_-]+`, and must not contain `:` (`plugins_content.py:42-46`).
- `path` must exist (it is the `SKILL.md`), else `FileNotFoundError` (`plugins_content.py:51-52`).
- Resolvable as `'<plugin_name>:<name>'` through `skill_view()` (`plugins_content.py:36-37`).

### 2g. `post_message` / `apost_message` and editing

`hermes_cli/plugins.py:607-616`:

```python
    def post_message(self, text: str) -> Any:
        """Post *text* into the session bound to this call; the handle's ``edit(text)`` updates it in
        place. Never raises (``ok=False`` + ``error`` with no conversation): ``plugin_messages.py``."""
        from hermes_cli.plugin_messages import post
        return post(self, text)

    async def apost_message(self, text: str) -> Any:
        """``post_message`` for async code on the gateway loop (edit with ``await handle.aedit``)."""
        from hermes_cli.plugin_messages import apost
        return await apost(self, text)
```

- Returns a `PluginMessage` (`hermes_cli/plugin_messages.py:25`). It never raises; a refused post is a handle
  with `ok == False` and `error` set.
- Read properties: `ok` (`plugin_messages.py:40`), `error` (`:44`), `message_id` (`:33-38`), `pending` (`:48`), `result`.
- Edit in place: `handle.edit(text) -> dict` (sync, from worker threads; `plugin_messages.py:82`), or
  `await handle.aedit(text) -> dict` on the gateway loop (`plugin_messages.py:76`). Result dict:
  `{"ok", "message_id", "edited", ["fallback"]}` (docstring at `plugin_messages.py:83`).
- The target is the session bound to the current call. A plugin cannot name a chat. Outside a session the
  result is `{"ok": False, "error": "no_conversation"}`.

### 2h. `ask_choice`

`hermes_cli/plugins.py:618-621`:

```python
    def ask_choice(self, question: str, choices: list) -> Optional[str]:
        """From a sync slash-command handler: ask its user to pick one of *choices* (``str`` or
        ``{value, label?, description?}``); the value, or None if unanswerable: ``plugin_choices.py``."""
```

Implementation `hermes_cli/plugin_choices.py:81`, `def ask_choice(question: str, choices: list[Choice]) -> Optional[str]`.
Returns the chosen `value`, or `None` when no asker is bound, the call is on an event loop, or the user
cancels. Only a sync slash-command handler can use it (`plugin_choices.py:81-90`).
Choice dict keys: `value` (required), `label`, `description` (`plugin_choices.py:58-70`).

### 2i. Other ctx methods that matter

- `register_memory_provider(provider)` `plugins.py:725`.
- `register_middleware(kind, callback)` `plugins.py:939`.
- `register_context_engine(engine)` `plugins.py:688`.
- `register_platform(...)` `plugins.py:779`.
- `inject_message(content, role="user", *, session_key=None, origin=None) -> bool` `plugins.py:588-590`.
- `on_unload(callback)` `plugins.py:416` (cleanup callback).
- `spawn_task(coro, *, name=None)` `plugins.py:425`.
- `register_approval_transport(name, present_fn)` `plugins.py:437`.
- `set_config(key, value)` `plugins.py:278` writes `plugins.entries.<id>.settings.<key>`.

### 2j. Secrets

- **Plugin ctx has NO secret accessor.** `PluginContext` has no `get_secret` method. NOT FOUND on `PluginContext`
  (`hermes_cli/plugins.py`).
- The repo's own rule (`plugin-catalog/README.md`, rule 11) says secrets come through
  `agent.secret_scope.get_secret(NAME)`. Definition, `agent/secret_scope.py:230`:

  ```python
  def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
      """Resolve a credential by env-var name, honoring the active profile scope.
  ```

  Also `get_secret_str(name, default="")` at `agent/secret_scope.py:272`.
- Non-secret settings: `ctx.get_config(key, default)` (`plugins.py:266`). Secret `config_schema` fields live in `.env`.

---

## 3. `plugin_data_dir` and `plugin_db`

Import path: `from plugins.plugin_storage import plugin_data_dir, plugin_db`
(file `plugins/plugin_storage.py`). Use is in the repo at `plugins/hermes-achievements/dashboard/plugin_api.py:152`
(`from plugins.plugin_storage import plugin_data_dir`). The validator's allow-list also names it
(`hermes_cli/plugin_validate_private_imports.py:39`).

`plugins/plugin_storage.py:27`:

```python
def plugin_data_dir(name: str) -> Path:
    """Return (and create) ``<hermes home>/plugin-data/<name>/``; resolves ``get_hermes_home()`` on
    every call so it follows the active profile — don't cache across profile switches."""
```

`plugins/plugin_storage.py:36`:

```python
def plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection:
    """Open ``<data dir>/<filename>``. WAL so a dashboard reader and a tool writer coexist;
    ``check_same_thread=False`` for the threaded FastAPI/tool env — caller owns transactions."""
```

- `plugin_data_dir(name) -> pathlib.Path`, creates `<hermes home>/plugin-data/<name>/`.
- `plugin_db(name, filename="data.db") -> sqlite3.Connection` (WAL via `hermes_cli.sqlite_util.open_db`,
  foreign keys on, `check_same_thread=False`).
- Name rule `_NAME_RE = ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$` and no `..` (`plugin_storage.py:19-24`). Filename must have no
  path separators (`plugin_storage.py:39`).
- Secrets are NOT stored here (module docstring, `plugin_storage.py:3-8`).
- The `state` property on `ctx` (`plugins.py:283-285`) is a separate JSON-state facade:
  `PluginState(self.plugin_id, self.manifest.data_namespace)`.

---

## 4. `hermes plugins validate` and `hermes plugins doctor --ci`

Argparse, `hermes_cli/subcommands/plugins.py`:

- `validate` (lines 60-65): positional `path`, `--install-deps`, `--json`. **There is NO `--ci` flag on validate.**
- `doctor` (lines 162-167): positional `target` (default `.`), and `--ci`: "Exit non-zero when validation reports an error".

Dispatch: `hermes_cli/plugins_cmd.py:1114-1117` (`validate` to `_catalog().cmd_validate`) and
`plugins_cmd.py:1131` (`doctor` to `cmd_plugin_doctor(args.target, ci=...)`).

### 4a. `hermes plugins validate`

Implementation: `hermes_cli/plugin_validate.py`, entry `validate_plugin_dir` at line 502. Command wrapper:
`hermes_cli/plugins_cmd_catalog.py:880-910` `cmd_validate`, which prints each check and exits `report.exit_code`
(0 if all checks pass, else 1). Exit is 0/1 regardless of flags. It is the catalog admission gate.

Static checks, in order (`plugin_validate.py:527-553`):

1. `manifest`: `plugin.yaml` (or `plugin.yml`) must parse and be a mapping. If absent, a `plugin.json` makes it a
   portable package (`_validate_portable_plugin`, `plugin_validate.py:638`). Otherwise fail.
2. `manifest fields`: **`name`, `version`, `description` must all be present** (`_check_manifest_fields`, line 101).
   This is stricter than the loader, which defaults `name` to the directory name.
3. `requires_hermes`: must parse as a spec like `>=0.19` or `>=0.19, <1.0` (`_check_requires_hermes`, line 115;
   `_requires_hermes_spec_valid`, line 81).
4. `config schema`: shape check, see 1b (`_check_config_spec`, line 131).
5. `requires_env`: list, items are str or mapping, names UPPER_SNAKE_CASE (`_check_requires_env`, line 164).
6. `oauth`: shape, https and declared env vars (`_check_oauth`, line 188; uses `hermes_cli/plugin_oauth.py`).
7. `loadable`: needs `__init__.py`, `desktop/plugin.js`, `plugin.json`, or `provides_locales` plus `locales/`
   (`_LOADABLE_ENTRYPOINTS` line 578, `_check_loadable` line 581).
8. `python dependencies`: `pm/plugin_declarations.read_python_declaration` must parse. Direct URL requirements are
   WARNINGS. It checks that requirements are well-formed PEP 508 (`_check_python_dependencies`, line 596). **It does
   NOT check for upper bounds** (NOT FOUND in validate; the upper-bound check is in doctor, see 4b).
9. `capability probe` (`_check_capabilities`, line 407): runs `register(ctx)` in a **subprocess** (`_PROBE_SCRIPT`,
   `plugin_validate.py:211`; timeout `_PROBE_TIMEOUT = 30` at line 36) with a `RecordingContext` stub
   (line 227) and `HERMES_HOME` set to a scratch dir. The stub's `register_tool`, `register_hook`,
   `register_middleware`, `register_command`, `register_cli_command`, `register_platform` record names. Every other
   `PluginContext` method is a no-op. **This probe does NOT block network** (no socket patch in the subprocess).
10. Declared vs registered: each of `provides_tools`, `provides_hooks`, `provides_middleware` is diffed against the
    recorded registrations. Undeclared registrations FAIL (`declared tools|hooks|middleware`). Declared-but-not-registered
    only WARN.
11. `built-in tool collisions` (`_check_builtin_collisions`, line 477): a tool name equal to a built-in registry tool FAILS.
12. `trusted inbound` (`_check_trusted_inbound`): a platform declaring `trusted_inbound` on a core platform FAILS.
13. `security scan` (`_check_security_scan`, line 621, runs `tools/plugin_guard.scan_plugin`): `dangerous` FAILS;
    `caution` is a warning.
14. `check_core_override`, `check_private_imports`, `check_desktop_surface`, `check_language_packs`: imported from
    `hermes_cli/plugin_validate_*.py`. `check_private_imports` warns on undocumented core imports, and
    `plugin_validate_private_imports.py:30-45` holds the public allow-list (includes `plugins.plugin_storage`,
    `plugins.plugin_utils`, `agent.secret_scope`, `hermes_cli.plugins:PluginContext`).
15. Isolation audit (`plugin_isolation_audit.audit_plugin_dir`) is informational and never fails.

### 4b. `hermes plugins doctor` (and `--ci`)

Implementation: `hermes_cli/plugin_dev.py`, `doctor_plugin(target)` at line 438. CLI wrapper
`hermes_cli/plugins_cmd.py:1065-1072`:

```python
def cmd_plugin_doctor(target: str = ".", *, ci: bool = False) -> None:
    """Validate one plugin through runtime discovery and registration."""
    from hermes_cli.plugin_dev import doctor_plugin
    report = doctor_plugin(target)
    _console().print(report.format_text())
    if ci and not report.ok:
        raise SystemExit(1)
```

`DoctorReport.ok` (`plugin_dev.py:221-222`): `return all(finding.level != "error" for finding in self.findings)`.
So `--ci` fails on **errors only**. Warnings never fail it.

**Network**: YES, blocked during load. `plugin_dev.py:27-28` `_deny_network` raises `RuntimeError("network access is disabled while Plugin Doctor runs")`.
`plugin_dev.py:94-96` patches `socket.create_connection`, `socket.socket.connect`, `socket.socket.connect_ex`. This stays in
effect for the whole import and `register()` (`_doctor_runtime`, line 60). It also sets a temporary `HERMES_HOME`,
`HERMES_BUNDLED_PLUGINS`, and `HERMES_ENABLE_PROJECT_PLUGINS=0` (`plugin_dev.py:89-93`).

Checks that produce **errors** (fail `--ci`):

- Loader failure: no valid manifest, multiple manifests, `Plugin registration failed: <error>`, or the record not enabled
  (`_doctor_runtime`, `plugin_dev.py:111-127`).
- `provides_hooks must be a list` / `provides_tools must be a list` (`plugin_dev.py:460` and following).
- `provides_hooks entries must be strings` (`plugin_dev.py:468`).
- `unknown hook '<name>' in provides_hooks` (`plugin_dev.py:470`).
- `registered unknown hook '<name>'` (`plugin_dev.py:474`).
- `hook callback '<name>' for '<hook>' must accept **kwargs for forward compatibility` (`plugin_dev.py:476-480`, via `_accepts_var_kwargs`, line 332).

Checks that produce **warnings** only (do not fail `--ci`):

- `manifest declares <kind> '<name>' but registration did not add it` / `registration adds <kind> '<name>' not listed in provides_<kind>s` (`plugin_dev.py:489-491`). Note: doctor compares tools and hooks only, not middleware.
- `manifest_version N is newer than this Hermes supports` (`_check_manifest_v2`, line 340).
- `requires plugin '<id>' (<range>)`: version ranges are advisory (`_check_manifest_v2`).
- `python_dependencies entry '<req>' has no upper bound` (`_check_manifest_v2`, line 383). Only when neither `<` nor `==` nor `~=`
  appears in the requirement, so a bare floor (`>=1.0`) warns.
- `declared python_dependencies not installed` (`_check_manifest_v2`).
- `config_schema key '<k>' declares unknown type '<t>'`.
- Desktop half-copy staleness (`_check_desktop_half_copy`, line 400).

---

## 5. Built-in memory tool

Name: **`memory`**.

- `tools/memory_tool.py:373-375`: `MEMORY_SCHEMA = {"name": "memory", ...}`.
- `tools/memory_tool.py:468-476`:

  ```python
  registry.register(
      name="memory",
      toolset="memory",
      schema=MEMORY_SCHEMA,
      handler=lambda args, **kw: memory_tool(
          action=args.get("action", ""), target=args.get("target"), store=kw.get("store"),
          **{k: args.get(k) for k in ("content", "old_text", "new_text", "operations")}),
  ```
- Args: `action`, `target`, `content`, `old_text`, `new_text`, `operations` (list of `{action, content?, old_text?}`).
  The underlying function is `memory_tool(action, target="memory", content, old_text, new_text, operations, store)`
  at `tools/memory_tool.py:222-224`.
- From a plugin, `ctx.dispatch_tool("memory", {"action": "add", "content": "..."})`. The `store` kwarg is
  supplied by the caller's kwargs, not automatically. Operations are serialized through the registry scope
  (`plugins.py:686`).

---

## 6. Cron: one-shot scheduling from a plugin

- **No plugin ctx method for scheduling. NOT FOUND** on `PluginContext` (`hermes_cli/plugins.py`). The only cron-related
  ctx method is read-only `current_cron_execution()` (`plugins.py:411-414`).
- Programmatic API that exists: `cron/jobs.py:1809` `def create_job(prompt, schedule, name=None, repeat=None, deliver=None, origin=None, ...) -> dict`.
  Schedule strings are parsed by `cron/jobs.py:787` `parse_schedule`: `"30m"` and `"every 30m"` are recurring intervals;
  an ISO timestamp is a **one-shot** (`"once"` kind, docstring at `cron/jobs.py:788-790`).
- Registration with the active scheduler provider: `cron/scheduler.py:3999` `create_job_with_scheduler_registration(**kwargs)`.
- No in-tree plugin calls `create_job`. The plugin would import it directly. Nothing in the plugin contract guarantees it.

---

## 7. `plugin-catalog/README.md` and an example entry

Entry schema, verbatim from `plugin-catalog/README.md` (inside the "Entry schema" yaml block):

```yaml
name: example-plugin        # [a-z0-9_-]{1,64}, the catalog key
repo: https://github.com/owner/repo   # https:// only
sha: <40-hex commit sha>    # mandatory exact pin
subdir: ""                  # optional; plain relative path inside the repo
description: One-line description.
maintainer: OwnerName
tier: community             # official | verified | community (default community);
category: memory            # desktop | memory | platform | web | tools | voice | automation | models | general
                            # (default desktop)
requires_hermes: ">=0.19"   # optional; enforced at install and update (also declare it in plugin.yaml)
docs_url: ""                # optional
version: "1.4.0"            # optional human label for the sha (quote it)
image: ""                   # optional https image on a GitHub host, 2:1
screenshots: []             # optional, up to 6 https images on a GitHub host
readme: true                # optional, default true
platforms: []               # optional, e.g. [linux, macos]; empty = all
title: ""                   # optional human name on cards
onboarding: false           # optional; official or verified entries only
known_issues: []            # optional strings shown at the install prompt
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

**Required keys** (enforced by `scripts/validate_plugin_catalog.py:91`):

```python
REQUIRED_KEYS = ("name", "repo", "sha", "description", "maintainer")
```

Key-format rules (`scripts/validate_plugin_catalog.py`):

- `NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}\Z")` (line 46).
- `SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")` (line 47): exactly 40 lowercase hex.
- `tier` in `official|verified|community` (default `community`). `category` default `desktop`. `platforms` in `linux|macos|windows`.
- Unknown top-level keys only WARN (`KNOWN_KEYS`, line 59; it also lists `attested_binaries`, which the README does not document).
- `capabilities` keys: `provides_tools`, `provides_hooks`, `provides_middleware`, `requires_env` (`CAPABILITY_KEYS`, line 51).
- `image` and `screenshots` must be `https` on `github.com` or `*.githubusercontent.com` (`_is_allowed_image_url`).

Example entry: `plugin-catalog/shipframe.yaml`, the required fields verbatim:

```yaml
name: shipframe
repo: https://github.com/juanitourquiza/shipframe
sha: 2d0a246206b7a3235c3167af5512d8dd4d8a29dd
description: >-
  AI coding workflows for teams that plan, prove, and ship ...
maintainer: juanitourquiza
tier: community
category: tools
title: ShipFrame
docs_url: https://shipframe.hackeruna.com/
version: "1.1.0"
platforms: []
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

Note: `plugin-catalog/README.md` says admission is via PR, every entry is pinned to a 40-hex SHA, and CI
(`.github/workflows/plugin-catalog-ci.yml`) clones each changed entry at its pin and runs `hermes plugins validate`.

---

## 8. Example plugin in the repo to mirror

Caveat: `plugins/AGENTS.md` says reference/docs-companion plugins (`example-dashboard`, `plugin-llm-example`, ...)
live in the separate repo `NousResearch/hermes-example-plugins`, not in-tree. The in-tree examples below are real
shipped plugins.

Best small template with hooks and a slash command: **`plugins/disk-cleanup/`**.

Tree:

```
plugins/disk-cleanup/
  __init__.py      # register(ctx) + hook callbacks
  plugin.yaml
  README.md
  disk_cleanup.py  # logic module, imported as `from . import disk_cleanup as dg`
```

`plugins/disk-cleanup/plugin.yaml` (verbatim):

```yaml
name: disk-cleanup
version: 2.0.0
description: "Auto-track and clean up ephemeral files (test scripts, temp outputs, cron logs) created during Hermes sessions. Runs via plugin hooks — no agent action required."
author: "@LVT382009 (original), NousResearch (plugin port)"
provides_hooks:
  - pre_tool_call
  - post_tool_call
  - on_session_end
```

`plugins/disk-cleanup/__init__.py:231-236` (`register`, verbatim):

```python
def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command("disk-cleanup", handler=_handle_slash,
                         description="Track and clean up ephemeral Hermes session files.")
```

Note: `register_command(... handler=...)` is called with `handler` as a keyword. Hook callbacks use `**_: Any` to
absorb extra kwargs (`disk-cleanup/__init__.py`, `_on_pre_tool_call(tool_name="", args=None, task_id="", session_id="", tool_call_id="", **_)`).

Tool-registering example: **`plugins/google_meet/`** (`plugin.yaml` with `provides_tools`, plus `register_tool` in a loop):

`plugins/google_meet/__init__.py:42-57` (verbatim core):

```python
def register(ctx) -> None:
    """Register tools, CLI, and lifecycle hooks (called once by the plugin loader)."""
    ...
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset="google_meet", schema=schema, handler=handler,
                          check_fn=check_meet_requirements, emoji=emoji)
    ctx.register_cli_command(
        name="meet", help="Google Meet bot (join, transcribe, follow up)",
        setup_fn=_register_meet_cli, handler_fn=_meet_command,
        description=("Let the hermes agent join a Google Meet call and scrape live "
                     "captions into a transcript. See: hermes meet setup"))
    ctx.register_hook("on_session_end", _on_session_end)
```

Its `plugin.yaml` declares `kind: standalone`, `platforms: [linux, macos]`, `provides_tools: [meet_join, meet_leave, meet_status, meet_transcript, meet_say]`, `provides_hooks: [on_session_end]`.

Minimal hook-only manifest: `plugins/security-guidance/plugin.yaml` (`provides_hooks: [transform_tool_result, pre_tool_call]`).
`plugins/security-guidance/__init__.py:131-133` registers `pre_tool_call` and `transform_tool_result` via `ctx.register_hook`.

Repo-only fixture showing the minimum manifest that loads: `tests/hermes_cli/fixtures/plugin_compat_legacy/plugin.yaml`
(`name`, `version`, `description`, `provides_hooks`, plus an unknown future field).

---

## 9. How plugin tests build a fake ctx

There is no shared `ctx` fixture for plugins. Three patterns exist.

**A. Real `PluginContext` around a bare `PluginManager`** (most direct).
`tests/agent/test_post_compaction_hook.py:17-24`:

```python
@pytest.fixture
def ctx(monkeypatch):
    mgr = PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
    return PluginContext(PluginManifest(name="compaction-observer", source="user"), mgr)
```

**B. Real discovery from a temp `HERMES_HOME`** (what `plugins/AGENTS.md` asks for).
`tests/agent/test_side_question_lease.py`: `_install_plugin` (near line 27) writes `plugin.yaml` and `__init__.py` under
`Path(os.environ["HERMES_HOME"]) / "plugins" / name`. `_load` (just above line 52) writes `config.yaml` with `plugins.enabled`, then:

```python
def _load(enabled: list[str], granted: list[str]) -> plugins_mod.PluginManager:
    ...
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins(force=True)
    return plugins_mod.get_plugin_manager()

def _ctx(manager, name: str) -> PluginContext:
    return PluginContext(manager._plugins[name].manifest, manager)
```

**C. Tiny duck-typed stub** when only one or two registrars are used.
`tests/gateway/test_yuanbao_plugin_registration.py:6-11`:

```python
class _Ctx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
```

**Doctor-style tests** (fastest for manifest and hook contract checks): `tests/hermes_cli/test_plugin_dev.py:10-55`.
Writes a plugin dir under `tmp_path` and calls `doctor_plugin(plugin)`; asserts `report.ok` and message substrings.
`_make_plugin` in `tests/hermes_cli/test_plugin_validate.py:20` builds a plugin dir and calls `validate_plugin_dir(d)`.

Tests live under `tests/plugins/` (per `plugins/AGENTS.md`) and `tests/hermes_cli/`.

---

## 10. `pyproject.toml` (hermes-agent)

- Package name: `hermes-agent` (`pyproject.toml:6`, `name = "hermes-agent"`). Version placeholder `0.0.0` (line 7).
- Python: `requires-python = ">=3.11,<3.15"` (`pyproject.toml:15`). A comment at lines 10-14 says 3.14 is the newest
  wheel target, and the range exists for older installs.
- Build system: setuptools (`pyproject.toml:578`, `[build-system]`).
- CLI entry points (`pyproject.toml:586-589`):

  ```toml
  [project.scripts]
  hermes = "hermes_cli.main:main"
  hermes-agent = "agent.legacy_cli:main"
  hermes-acp = "acp_adapter.entry:main"
  ```

  The primary CLI name is **`hermes`**, which is `hermes_cli.main:main`.

---

## Summary of NOT FOUND items

| Asked | Result |
|---|---|
| Manifest `entry` or `module` field | NOT FOUND. Entry is `__init__.py` at the plugin root, `register(ctx)`. |
| `ctx` secret accessor | NOT FOUND on ctx. Use `agent.secret_scope.get_secret` (`agent/secret_scope.py:230`). |
| Plugin-facing one-shot cron API | NOT FOUND on ctx. Only `cron/jobs.py:1809 create_job` exists and no plugin uses it. |
| `hermes plugins validate --ci` | NOT FOUND. Validate always exits 0/1. Only `doctor` has `--ci`. |
| Network blocked during `validate` | NOT FOUND (only `doctor` blocks sockets, `plugin_dev.py:94-96`). |
| Upper-bound check in `validate` | NOT FOUND (only `doctor` warns, `plugin_dev.py:383`). |
| `ctx.register_hook` or `ctx.register_tool` kwarg named `entry` | Not applicable. |

Unexpected findings worth noting:

- `pre_tool_call` also accepts `{"action": "modify", "args": {...}}` (`plugins.py:1937`).
- `validate` requires `version` and `description` in `plugin.yaml`, which the loader does not (`plugin_validate.py:101`).
- `validate`'s capability probe runs `register()` in a subprocess with no network block, while `doctor` blocks it.
- `doctor` treats an unknown hook name in `provides_hooks` as an error. The loader only warns.
