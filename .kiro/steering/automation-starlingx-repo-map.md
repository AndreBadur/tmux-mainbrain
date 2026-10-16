---
inclusion: manual
title: StarlingX Test Automation — Repo Map & SDET Onboarding Guide
scope: ~/automation/starlingx  (opendev.org/starlingx/test — the "stx-test" repo)
purpose: >
  A reusable, realm-level map of the StarlingX test-automation framework. Written so a
  senior SDET (human or agent) can write IDIOMATIC tests here without re-discovering the
  framework. Covers: how tests are structured/discovered/run; the keyword (helper) API; the
  marker/capability-gating model; the house-style rules a reviewer enforces; and the broad
  test surface (incl. PTP/ts2phc precedent).
how_to_use: >
  Load this file as a resource when generating, reviewing, or debugging tests in
  ~/automation/starlingx. Treat PROVEN facts (read directly from source) as reliable; treat
  PRESUMED facts (reasoned from strong signals) as hypotheses to confirm against live code.
  Always prefer REUSING an existing keyword over writing raw SSH/CLI. Cite the real paths
  below when navigating.
provenance_legend:
  PROVEN:   file/code was read directly during the mapping (2025, HEAD of the local checkout).
  PRESUMED: reasoned from naming, imports, or partial reads; verify before relying on it.
---

# StarlingX Test Automation — Repo Map

> Repo identity (PROVEN): this is the OpenDev **`starlingx/test`** repo ("stx-test"), cloned
> at `~/automation/starlingx`. Python **3.11**, `pipenv`-managed, Gerrit-reviewed
> (`git review`), pre-commit enforced. `~/automation/` (the parent) is a separate umbrella
> with its own `framework/ config/ wra/ wrc/ wrcp/ wro/`; the test code mapped here lives
> entirely under `~/automation/starlingx/` and imports from its OWN top-level packages
> (`framework.`, `config.`, `keywords.`, `testcases.`). (PRESUMED: the parent dirs are
> sibling projects, not imported by the stx-test suite — all imports seen are repo-local.)

---

## 0. TL;DR for a test-coder (the 10-second model)

- A test is a **plain `def test_*()` function** (NO class, NO `self`) in `testcases/...`.
- It gets an SSH connection via `LabConnectionKeywords().get_active_controller_ssh()`.
- It drives the system through **Keyword classes** (`keywords/...`), each taking an
  `SSHConnection` in its constructor. **Never** issue raw SSH if a keyword exists.
- It asserts through the **validation framework** (`framework/validation/validation.py`):
  `validate_equals(...)`, `validate_equals_with_retry(...)`, `fail(...)` — **never a bare
  `assert`** in real tests.
- It gates on hardware/lab shape with **pytest markers** (`@mark.p0`, `@mark.lab_has_*`).
- It cleans up with **`request.addfinalizer(...)`** (NOT yield fixtures), and in teardown it
  **downloads logs** for the record.
- It documents itself with **Google-style docstrings** (Test Steps / Preconditions / Notes).

---

## 1. FRAMEWORK SKELETON — structure, discovery, execution

### 1.1 It is pytest — but normally run through a custom executor (PROVEN)
- Config lives in **`testcases/pytest.ini`** (NOT repo root). Key `addopts = --tb=no -s`
  — stacktraces are suppressed because the framework logs them itself (see
  `testcases/conftest.py::log_exception`), and `-s` streams all output.
- **`tox.ini`** (repo root) only defines `docs`/`releasenotes` sphinx envs — tox is NOT the
  test runner. (PROVEN)
- The real entrypoint is the **custom harness**
  **`framework/runner/scripts/test_executor.py`** (PROVEN). Invocation (from README, PROVEN):
  ```bash
  python framework/runner/scripts/test_executor.py \
      --tests_location=testcases/cloud_platform/sanity \
      --lab_config_file=config/lab/files/custom_config.json5
  ```
  `test_executor.main()` builds a `TestCapabilityMatcher`, resolves the list of runnable
  tests (by folder, or by `--test_plan_id` from a DB), then runs **each test in its own
  `pytest.main([...])` call** with a `ResultCollector` plugin. It can also take
  `--session_id`, `--jenkins_log_location`, `--repository`.
- You CAN also run bare `pytest testcases/...` for a single test during dev, but then the
  capability-gating in §3 does NOT filter for you (the matcher is executor-side).

### 1.2 conftest / pytest hooks (PROVEN — `testcases/conftest.py`)
- `pytest_addoption` → wires `ConfigurationFileLocationsManager.add_options` and
  `RunContextLoader.add_options` through a `SafeOptionParser` (adds all the `--*_config_file`
  flags).
- `pytest_sessionstart` → builds `ConfigurationFileLocationsManager`,
  `ConfigurationManager.load_configs(...)`, loads run context, logs the resolved config.
- `pytest_runtest_setup/call/teardown` → reset step counters, attach a **per-testcase log
  handler**, and print **banners** (`framework/logging/log_banners`) for Setup/Execution/
  Teardown stages.
- `pytest_runtest_makereport` → on exception, `log_exception` prints a cleaned traceback
  (skips `_pytest`/`pluggy`/the keyword-wrapper frames); removes the per-test log handler at
  teardown. This is WHY `--tb=no` is set.

### 1.3 Config system (PROVEN — `config/`)
- **`config/configuration_manager.py`** exposes a **singleton** `ConfigurationManager`
  (instance of `ConfigurationManagerClass`). `load_configs()` loads ~20 domain configs, each
  from a `config/<domain>/files/default.json5` unless overridden by a `--<domain>_config_file`
  CLI flag. Config files are **JSON5** (`.json5`).
- Getters you will use constantly: `ConfigurationManager.get_lab_config()`,
  `.get_ptp_config()`, `.get_logger_config()`, `.get_docker_config()`, `.get_k8s_config()`,
  `.get_security_config()`, `.get_backup_restore_config()`, etc. (full list in that file).
- **Lab config** = `config/lab/files/default.json5` (floating IP, lab type, `use_jump_host`,
  `jump_server_config`, node list + **node_capabilities**, credentials). Templates like
  `template_simplex.json5` exist as bases (PRESUMED from README).
- **Lab objects**: `config/lab/objects/lab_config.py` (`LabConfig`) and `.../node.py` (`Node`
  with `get_node_capabilities()`, `get_ip()`, `get_bm_ip()/get_bm_port()`, `get_type()`).
  (PROVEN for `node.py`; `LabConfig` PRESUMED from heavy usage — `get_floating_ip()`,
  `get_admin_credentials()`, `is_use_jump_server()`, `get_jump_host_configuration()`,
  `get_node(name)`, `get_subcloud(name)`, `get_ssh_port()`, `get_lab_capabilities()`,
  `get_lab_name()`, `get_secondary_system_controller_config()`.)
- **Resource path helper (PROVEN usage)**: `framework/resources/resource_finder.py`
  → `get_stx_resource_path("resources/...")` and `get_stx_repo_root()`. Use these to resolve
  files under `resources/` (e.g. PTP json5 templates) — never hardcode absolute paths.

### 1.4 Logging (PROVEN usage; module PRESUMED internally)
- `from framework.logging.automation_logger import get_logger`. The singleton logger offers
  `log_info / log_error / log_debug / log_exception / log_warning`, plus domain-specific
  `log_keyword`, `log_ssh`, and stage helpers `log_teardown_step`, step counters, and
  `get_test_case_log_dir()` / `get_log_folder()` / `get_test_case_resources_log_location()`.
- Keyword invocations and SSH commands are auto-logged (see §2.1), so a well-written test's
  log reads like a narrated transcript.

### 1.5 Validation framework (PROVEN — `framework/validation/validation.py`)
This is the assertion layer. **Module-level functions** (not methods):
| Function | Signature (abbrev.) | Behavior |
|---|---|---|
| `validate_equals` | `(observed, expected, description)` | logs success or logs Expected/Observed + `raise Exception("Validation Failed")`. |
| `validate_not_equals` | `(observed, expected, description)` | inverse. |
| `validate_str_contains` | `(observed:str, expected:str, description)` | substring check. |
| `validate_list_contains` | `(observed, expected_values, description)` | membership. |
| `validate_greater_than` / `validate_greater_than_or_equal` / `validate_less_than_or_equal` | `(observed:int, baseline:int, description)` | numeric compare. |
| `validate_none` / `validate_not_none` | `(observed, description)` | None checks. |
| `validate_is_digit` | `(observed:str, description)` | `.isdigit()`. |
| `fail` | `(message)` | logs + `raise AssertionError`. The sanctioned explicit failure. |
| **`validate_equals_with_retry`** | `(function_to_execute, expected, description, timeout=30, polling_sleep_time=5, failure_values=())` | **Polls** `function_to_execute()` until it returns `expected`; `TimeoutError` on timeout; **fail-fast** if the observed value is in `failure_values` (`ValidationFailureError`). `failure_values` MUST be a sequence, not a raw str (raises `TypeError`). Returns the value. |
| `validate_not_equals_with_retry` / `validate_str_contains_with_retry` / `validate_list_contains_with_retry` / `validate_greater_than_with_retry` / `validate_not_none_with_retry` | `(function_to_execute, ..., timeout=30, polling_sleep_time=5)` | retry variants. |

Retry functions accept a **`ValidationResponse`** (`framework/validation/validation_response.py`,
PRESUMED shape: `get_value_to_validate()` + `get_value_to_return()`) so you can validate one
value but return a different one. **This is the idiomatic replacement for `time.sleep()` +
re-check loops** — never hand-roll polling.

### 1.6 Exceptions (PROVEN names)
`framework/exceptions/` → `ValidationFailureError`, `KeywordException`, `keyword_exception`.
Keywords raise `KeywordException` on operational failure; validation raises
`ValidationFailureError` / `TimeoutError` / `Exception("Validation Failed")`.

---

## 2. KEYWORD / HELPER INVENTORY — the vocabulary you write in

> A **Keyword** is a class under `keywords/` that wraps a cohesive capability. Convention:
> constructor takes an `SSHConnection` (`def __init__(self, ssh_connection: SSHConnection)`),
> methods perform actions and return parsed objects or primitives. Keyword directories mirror
> subsystems. **Reuse first** — the inventory is large.

### 2.1 BaseKeyword — the auto-logging + validation base (PROVEN — `keywords/base_keyword.py`)
- Every keyword SHOULD subclass **`BaseKeyword`**. It overrides `__getattribute__` to wrap
  **every** public method call in `on_every_keyword(name, *args, **kwargs)`, which emits a
  `get_logger().log_keyword("name(args...)")` line. So just by calling keywords you get a
  self-documenting log. (Note the positional-only `name, /` guard — don't shadow it.)
- Provides shared validators used from inside keywords:
  `validate_success_return_code(ssh)` (asserts rc==0), `validate_cmd_rejection_return_code(ssh)`
  (asserts rc!=0), `validate_success_status_code(rest_response, expected=200)`, and
  `pretty_print(value)`.

### 2.2 Connection layer (PROVEN) — the single most-used family
**`keywords/cloud_platform/ssh/lab_connection_keywords.py` → `LabConnectionKeywords(BaseKeyword)`**
is the gateway to every host. Methods (all return `SSHConnection`):
- `get_active_controller_ssh()` — the workhorse; connects to the lab floating IP (through a
  jump host if `use_jump_server`).
- `get_standby_controller_ssh()` — resolves standby via `SystemHostListKeywords`.
- `get_ssh_for_hostname(hostname)` — generic; for `worker`/`storage` node types it routes to
  `get_ssh_pass` (sshpass via the controller), else a direct/jump connection.
- `get_compute_ssh(name)` / `get_storage_ssh(name)` — real SSH via **ProxyCommand** through
  the controller (full SFTP).
- `get_ssh_pass(host_name)` — controller connection reconfigured to `sshpass` onto a node.
- `get_subcloud_ssh(name)` / `get_subcloud_controller0_ssh(name)` /
  `get_subcloud_factory_ssh(name)` — distributed-cloud subcloud access (floating IP, OAM IP
  via proxy, or factory IP/creds).
- `get_secondary_active_controller_ssh()` — secondary system controller.
- `ping_host(hostname, count=3) -> bool`, `test_ssh_connectivity(hostname) -> bool`.
- `create_bmc_ssh_tunnel(host_name)` — **context manager** yielding an `SSHTunnel` to the BMC.

**`framework/ssh/ssh_connection.py` → `SSHConnection`** (PROVEN) — the paramiko wrapper. Key
API a keyword uses:
- `send(cmd, command_timeout=None, reconnect_timeout=None, get_pty=False) -> list[str]`
  (returns output **lines**; combines stderr).
- `send_as_sudo(cmd, ...)` — interactive sudo (PTY).
- `send_as_sudo_non_interactive(cmd, ...)` — feeds password via stdin, discards sudo's stderr
  prompt; **use this when the prompt `@` char or PTY wrapping corrupts output**, or for
  pipelines (wrap in `bash -c`).
- `send_expect_prompts(cmd, prompts: list[PromptResponse], ...)` — interactive prompt/response.
- `get_return_code()` (PRESUMED method name; `last_return_code` is set after each send) —
  checked via `BaseKeyword.validate_success_return_code`.
- `get_sftp_client()` — paramiko SFTP for upload/download.
- Auto-reconnect loop with `reconnect_timeout` (default 600s), per-command default 60s.
- `SSHConnectionManager` (`framework/ssh/ssh_connection_manager.py`) static factory:
  `create_ssh_connection(host, user, pass, name=, ssh_port=, jump_host=)` and
  `create_proxy_ssh_connection(target_host, ..., first_jump_host=, second_jump_host=, ...)`.
- `SSHTunnel` / `SSHTunnelInfo` for port-forward tunnels (BMC/Redfish).

### 2.3 File / command helpers (PROVEN — `keywords/files/file_keywords.py` → `FileKeywords`)
Constructed with an `SSHConnection`. High-reuse methods:
- Transfer: `upload_file(local, remote, overwrite=True)`, `download_file(remote, local)`
  (SFTP). **Log download in teardown uses `FileKeywords(ssh).download_file(...)`.**
- Existence: `file_exists(name)`, `validate_file_exists_with_sudo(path)`.
- Read: `read_file(path)` (`cat`), `read_file_with_sudo(path)`,
  `read_large_file(name, grep_pattern=None)` (batched sed for >10k-line files),
  `read_file_with_pattern_range(path, start, end, grep_pattern=None)` (sed between patterns),
  `get_file_line_count(path)` + `read_file_from_line(path, start_line, grep_pattern=None)`
  (bookmark-a-log-then-read-new-lines idiom — timestamp-independent).
- Write/create: `create_file_with_echo`, `create_file_with_heredoc(path, content,
  delimiter="EOF", is_sudo=False)`, `generate_file_from_template(template_file,
  replacement_dictionary, target_file_name, target_remote_location)` (**Jinja2** render +
  upload).
- Dirs/cleanup: `create_directory[_with_sudo]`, `delete_file`, `delete_directory`,
  `delete_folder_with_sudo`, `get_files_in_dir(dir, is_sudo=False)`,
  `get_directory_size_kb(path, exclude_dirs=None, is_sudo=True)` (`du -sk`).
- Misc: `rename_file`, `move_file`, `copy_file(sudo=False)`, `make_executable`,
  `is_valid_gzip(path)`, `find_in_tgz(path, grep_pattern, is_sudo=False)`,
  `rsync_to_remote_server(...)` / `rsync_from_remote_server(...)` (sshpass+rsync),
  `create_file_to_fill_disk_space(dest_dir, file_size=None)` (dd to a size or to ENOSPC).
- Note the house pattern: paths are **`shlex.quote`-ed**, pipelines run under one sudo via
  `bash -c`, and `grep -F` is used for literal matching. Mirror this when adding methods.
- Related: `keywords/files/curl_keywords.py` (`CurlKeywords`), `yaml_keywords.py`.

### 2.4 systemctl / service control (PROVEN — `keywords/linux/systemctl/`)
- `systemctl_keywords.py` → `SystemCTLKeywords(BaseKeyword)`:
  `systemctl_start/stop/restart(service_name, instance_name)` → runs
  `systemctl <verb> <service_name>@<instance_name>.service` via `send_as_sudo` (note the
  **systemd instance `@` template** convention, e.g. `ptp4l@ptp1`).
- Also: `systemctl_is_active_keywords.py`, `systemctl_status_keywords.py`
  (`SystemCTLStatusKeywords.get_status(service_name)`), `systemctl_show_keywords.py`.

### 2.5 Cloud-platform `system` CLI keyword tree (PROVEN layout — `keywords/cloud_platform/system/`)
Mirrors the `system`/`dcmanager`/`fm` CLIs. Each subdir wraps a command group and parses
output via `system_table_parser.py` / `system_vertical_table_parser.py`. Notable:
- `host/` — `SystemHostListKeywords` (`get_active_controller()`, `get_standby_controller()`),
  `SystemHostLockKeywords` (`lock_host`/`unlock_host(..., exclude_alarm_ids=[...])`,
  `unlock_host_without_alarm_check`), `SystemHostRebootKeywords`, `SystemHostSwactKeywords`,
  host objects (`system_host_show_output.py`, `host_capabilities_object.py`).
- `application/` — `SystemApplicationListKeywords.get_system_application_list()`.
- `fault_management` (alarms, `fm alarm-list`), `health`/`health_query`, `interface`,
  `network`, `oam`, `storage`, `ceph`, `kubernetes`, `certificate`, `kube_rootca`,
  `service`/`servicegroup`/`servicenode`, `addrpool`, `datanetwork`, `license`, `load`,
  `registry`, `vault`, `ptp` (see §5).
- `keywords/cloud_platform/` siblings: `dcmanager/` (distributed cloud), `applications/`,
  `backup_restore/`, `containers/`, `helm/`, `rest/` (REST-API clients + `RestResponse`),
  `security/`, `swmanager/`/`sw_patch/`/`upgrade/` (USM/patching), `sysctl/`, `version_info/`,
  `virtctl/`, `networking/`, `fault_management/`.

### 2.6 Other keyword families (PROVEN dir listing)
- `keywords/k8s/` — large kubectl/helm tree: `pods`, `deployments`, `daemonsets`, `namespace`,
  `node`, `service`, `secret`, `configmap`, `pv`/`pvc`, `storageclass`, `crd`, `custom`,
  `kubevirt`/`vm`, `sriov_fec_node_config`, `telemetry`, `raw_metrics`, etc. Base:
  `k8s_base_keyword.py`, `k8s_command_wrapper.py`, `k8s_table_parser_base.py`.
- `keywords/linux/` — OS-level (systemctl above, plus general linux cmd wrappers).
- `keywords/docker/`, `keywords/crictl/`, `keywords/ceph/`, `keywords/network/`,
  `keywords/openstack/`, `keywords/openssl/`, `keywords/ostree/`, `keywords/libvirt/`,
  `keywords/bmc/` (+ `framework/redfish/`), `keywords/server/`, `keywords/python/`,
  `keywords/kpi/`, `keywords/system_test/`.

### 2.7 PTP keyword family (PROVEN — the richest worked example; see §5 for ts2phc)
Two homes:
- **`keywords/ptp/`** — protocol-level: `gnss_keywords.py` (`get_pci_slot_name`,
  `extract_gnss_port`), `phc_ctl_keywords.py`, `sma_keywords.py`, `proxmox_keywords.py`,
  `proxmox_ptp_verification_keywords.py`, `pmc/` (`PMCKeywords.pmc_get_port_data_set(...)`,
  `pmc_get_default_data_set` objects), `ptp4l/`, `cat/` (`CatPtpConfigKeywords`,
  `CatClockConfKeywords`, `CatPtpCguKeywords.cat_ptp_cgu`), and **`setup/`** (see §5.2).
- **`keywords/cloud_platform/system/ptp/`** — StarlingX-integration level:
  `ptp_setup_executor_keywords.py` (`PTPSetupExecutorKeywords`),
  `ptp_scenario_executor_keywords.py` (`PTPScenarioExecutorKeywords.execute_test_scenario`),
  `ptp_teardown_executor_keywords.py`, `ptp_status_collector_keywords.py`,
  `ptp_verify_config_keywords.py`, `gnss_monitoring_keywords.py`, `ptp_readiness_keywords.py`,
  `ptp_parameters_parser.py`, and the `system ptp-*` CLI wrappers
  (`system_ptp_instance_keywords.py`, `system_host_ptp_instance_keywords.py`,
  `system_ptp_interface_keywords.py`, `system_ptp_parameter_keywords.py`,
  `system_ptp_instance_parameter_keywords.py`, `system_host_if_ptp_keywords.py`,
  `system_ptp_show_keywords.py`).

---

## 3. MARKERS & CAPABILITY GATING (PROVEN)

### 3.1 Where markers are declared
All custom markers are registered in **`testcases/pytest.ini`** under `markers=`. Two groups:
- **Priority markers**: `p0`, `p1`, `p2`, `p3`.
- **Capability markers** (`lab_*` / `subcloud_lab_*`) — a test declares the lab/HW shape it
  needs. Real names (verbatim subset, PROVEN):
  `lab_is_simplex`, `lab_is_duplex`, `lab_is_aio`, `lab_is_virtual`, `lab_is_ipv4`,
  `lab_is_ipv6`, `lab_has_dual_stack`, `lab_has_standby_controller`,
  `lab_has_secondary_system_controller`, `lab_has_worker`, `lab_has_min_2_workers`,
  `lab_has_min_3_workers`, `lab_has_compute`/`_min_2_compute`/`_min_3_compute`,
  `lab_has_subcloud`/`_min_2_subclouds`, `subcloud_lab_has_compute`,
  `subcloud_lab_is_simplex`/`_duplex`/`_standard`, `lab_has_low_latency`/
  `lab_has_non_low_latency`, `lab_has_sriov`, `lab_has_hyperthreading`/
  `lab_has_no_hyperthreading`, `lab_has_processor_min_2`, `lab_has_page_size_1G`,
  `lab_has_ae_interface`, `lab_has_bond_interface`, `lab_has_physical_interface_min_2`,
  `lab_has_storage`/`_storage_6_osd`/`_min_2_osd`/`_min_3_osd`, `lab_has_free_disk`/
  `_min_2_free_disk`, `lab_has_min_space_30G`, `lab_has_ptp`, `lab_has_dx_plus_westport`,
  `lab_has_gnr_d`, `lab_has_rook_ceph`, `lab_has_ceph`/`_ceph_rbd`/`_cephfs`,
  `lab_has_lvm`/`_lvm_thin_cgts_vg`/`_lvm_thin_dedicated`,
  `lab_has_netapp_iscsi`/`_fc`/`_nfs`, `lab_has_n3000`, `lab_has_fpga`,
  `lab_has_acc100`/`_acc200`, `lab_has_columbiaville`, `lab_has_qat`, `lab_has_dsa`,
  `lab_has_ovs`, `lab_has_linux_cpu_metrics`, `lab_has_hardware`, `lab_has_bmc_redfish`,
  `lab_dell_storage`. (Full list — including TODO `lab_has_bmc_ipmi` etc. — in the file.)

### 3.2 How gating actually works (PROVEN — the key idiom)
Gating is **NOT** a `pytest.mark.skip` hook. It is done by the executor's
**`framework/runner/objects/test_capability_matcher.py` → `TestCapabilityMatcher`**:
1. `TestCapabilityMatcher.get_list_of_tests(folder)` runs `pytest --collect-only` with
   **`CollectionPlugin`** (`framework/pytest_plugins/collection_plugin.py`), yielding
   `TestCase` objects carrying their markers + priority.
2. `_filter_tests` keeps a test **iff** `set(test_markers).issubset(lab_capabilities)` — i.e.
   **every capability marker the test declares must be present in the lab's capabilities**
   (priority markers `p0..p3` are stripped out first and don't gate). A test with NO markers
   always runs.
3. Lab capabilities come from `lab_config.get_lab_capabilities()` (folder mode) or from the
   **DB** (`LabCapabilityOperation`) in test-plan mode. The capability vocabulary is persisted
   in `framework/database/objects/capability.py` (`capability_marker`) and matched in SQL
   (`run_content_operation.py`).
- **Consequence for a test-coder**: a marker is a *promise* checked against lab config; adding
  a `lab_has_X` marker with no corresponding lab capability silently excludes the test from a
  run. Keep markers truthful and minimal. `lab_capability_scanner.py` (`scripts/`) can
  populate lab capabilities (README, PRESUMED).

### 3.3 Priority
`CollectionPlugin.PRIORITIES = ["p0","p1","p2","p3"]`; priority is extracted and stored
separately on `TestCase` (used for selection/reporting, not capability gating).

---

## 4. CONVENTIONS & ANTI-PATTERNS — the house style a reviewer enforces

### 4.1 Hard rules (PROVEN — CONTRIBUTING.rst, pyproject.toml, pydocstyle.ini, .flake8, .pre-commit-config.yaml)
- **Python 3.11**, `pipenv`. Pre-commit hooks are mandatory (`pre-commit install`), and a
  **pre-push** hook runs **unit tests** (`pre-commit install --hook-type pre-push`).
- Formatting: **black** + **isort** (`profile=black`, `py311`). isort line length **200**;
  black `line-length = 9999` (preview) → **black effectively does not wrap** — keep logical
  lines readable yourself; CONTRIBUTING states the human limit is **200 chars**.
- **f-strings only** — never `%` or `.format()`.
- **Type hints required** on every function signature (args + return) — PEP 484.
- **Google-style docstrings required** on **all functions and classes** (enforced by
  `pydocstyle` + `pydoclint` + `interrogate`). Must include **`Args:`** and a **`Returns:`**
  section whose type matches the return hint (`pydoclint check-return-types=true`). Module
  docstrings NOT required; `__init__` docstrings optional (`allow-init-docstring`). `Raises:`
  checking is **skipped** (`skip-checking-raises`), but existing code documents `Raises:`
  anyway — follow suit.
- Lint: **flake8** (`.flake8`). Commits are **Gerrit** (`git commit -s` sign-off + `git
  review`); follow OpenStack commit-message guidelines. Jenkins/Zuul CI (`.zuul.yaml`).

### 4.2 Test-writing do's and don'ts (PROVEN from real tests, esp.
`testcases/cloud_platform/regression/ptp/test_ptp_westport_tgm_tbc.py`)
DO:
- Write tests as **module-level `def test_*()` functions** decorated with `@mark.<priority>`
  and the needed `@mark.lab_has_*`. Take `request` as a param when you need teardown.
- Get connections via `LabConnectionKeywords()`; drive the system through **keyword objects**;
  parse via the provided parsers/objects — **don't** scrape raw strings in the test body.
- Assert with the **validation functions** (`validate_equals`, `*_with_retry`, `fail`), always
  passing a human-readable `validation_description`.
- Use **`*_with_retry` instead of `time.sleep`** for anything eventual (alarms clearing, port
  states settling). (No bare `sleep` loops in tests — polling lives in the framework.)
- **Teardown via `request.addfinalizer(finalizer_fn)`** — define a nested finalizer and
  register it; the established pattern **collects status and downloads `/var/log/user.log`**
  (and subsystem logs) into `get_logger().get_test_case_log_dir()` for post-mortem. See
  `teardown_collect_ptp_status_and_logs(request)`.
- **Narrate with the logger**: `get_logger().log_info("Delete all PTP configuration")`,
  `log_teardown_step(...)`. Combined with BaseKeyword auto-logging, the log is the story.
- Document each test with a Google docstring containing **Test Steps / Preconditions / Notes**
  (the PTP tests are the gold-standard template).
- Prefer **declarative scenario data** driving an executor
  (`PTPScenarioExecutorKeywords.execute_test_scenario(scenario, request)`) over imperative
  step soup, when the subsystem provides such an executor.

DON'T:
- **No bare `assert`** in real tests (the only `assert` seen is in `hello_world_test.py`,
  explicitly flagged "Should be removed when we have real tests" — treat it as a non-example).
- No `print()` — use `get_logger()`.
- No **yield-fixtures / ad-hoc pytest fixtures** for setup/teardown in the test body — the
  house uses `request.addfinalizer`. (PRESUMED as the enforced norm: no custom `@pytest.fixture`
  seen in the mapped tests; conftest only defines hooks.)
- No raw SSH/CLI when a keyword exists — extend the keyword layer instead.
- No hardcoded absolute paths — resolve with `get_stx_resource_path(...)`.
- Don't widen a test's `lab_has_*` markers beyond what it truly needs (see §3.2).

### 4.3 Keyword-writing conventions (PROVEN)
- Subclass `BaseKeyword`; constructor takes `ssh_connection: SSHConnection`.
- Return parsed **objects** (there is a strong `objects/` + `*_table_parser` pattern), not raw
  lines, to callers.
- `shlex.quote` user/path args; run sudo pipelines under `bash -c`; validate return codes with
  `self.validate_success_return_code(self.ssh_connection)`.
- Raise `KeywordException` with context on failure; log via the inherited auto-logging.

### 4.4 "Prove-the-negative-first" / TEST_SCOPE_VALIDATION (PRESUMED)
No literal `TEST_SCOPE_VALIDATION` symbol was found in the mapping. The equivalent idiom that
IS present: scenarios assert an **alarm_set** state after an induced fault and then
**alarm_clear** after recovery (see the westport scenarios), i.e. prove the system reacts to
the bad state before proving it recovers. Treat an explicit "prove-the-negative-first" rule as
UNCONFIRMED — follow the alarm-set→alarm-clear scenario shape as the de-facto pattern.

---

## 5. TEST SURFACE INVENTORY (broad) + PTP/ts2phc precedent

### 5.1 Shape of `testcases/` (PROVEN)
- `testcases/pytest.ini`, `testcases/conftest.py`, then two top trees:
  **`testcases/cloud_platform/`** (the bulk) and **`testcases/openstack/`**.
- Under `cloud_platform/`: `sanity/`, `functional/`, **`regression/`**, `performance/`,
  `kpi/`, `patching/`, `upgrade/`, `apps_setup/`, `backup_and_restore/`, `bios/`,
  `k8s_operator_framework/`, `system_setup/`, `system_test/`, plus `hello_world_test.py`
  (demo only).
- **`testcases/cloud_platform/regression/`** subsystems (PROVEN): `alarm/`, `applications/`,
  `containers/`, `dc/` (distributed cloud), `dsa/`, `k8s_operator_framework/`, `networking/`,
  `nfd/`, `platform/`, `platform_services/`, `power_metrics/`, **`ptp/`**,
  `ptp_metrics_exporter/`, `qat/`, `rest/`, `security/`, `snmp/`, `storage/`.
- Mirror structure exists in `config/` (per-subsystem configs), `resources/` (data files,
  incl. `resources/ptp/*.json5`), `web_pages/` (Selenium page objects — `web_pages/`,
  `framework/web/`, Chrome WebDriver per README), `unit_tests/` (framework's own unit tests,
  run by the pre-push hook), `portal/`, `scripts/` (`lab_capability_scanner.py`, etc.).

### 5.2 PTP / ts2phc support (PROVEN — fair look, not over-weighted)
PTP is the most mature worked example and has **first-class ts2phc support**:
- **Tests**: `testcases/cloud_platform/regression/ptp/` — `test_ptp_westport_tgm_tbc.py`
  (TGM/TBC scenarios: interface up/down, SMA disable/enable, GNSS off/on, with alarm +
  PMC-value verification), `test_gnss_monitoring.py`, `cli_error_cases/`, and a thorough
  `README.rst` (PTP concepts, topology, clock-class/accuracy/port-state reference tables,
  diagnostic commands). Marker: **`@mark.lab_has_dx_plus_westport`** (its config
  `ptp_data_westport_dx_plus_tgm_tbc.json5`) and `@mark.lab_has_ptp` / `lab_has_gnr_d`.
- **Setup objects** (`keywords/ptp/setup/object/`): `PTPSetup` aggregates `PTP4LSetup`,
  `PHC2SysSetup`, **`TS2PHCSetup`**, `ClockSetup`, `PTPHostInterfaceSetup`. `PTPSetup.__init__`
  reads `ptp_instances["ts2phc"]` → builds `TS2PHCSetup` entries (PROVEN).
- **Setup reader** (`keywords/ptp/setup/ptp_setup_reader.py` → `PTPSetupKeywords`): renders a
  **JSON5 template** (`resources/ptp/*.json5`) through **Jinja2** using the ptp-config host
  dictionary + `resources/ptp/ptp_default_status_values.json5`, producing a `PTPSetup`.
  Also `filter_and_render_ptp_config(...)` (render a partial setup for selected instances with
  deep overrides) and `get_base_pmc_values(...)`. This template+override mechanism is the
  model for data-driven PTP tests.
- **Executors** (`keywords/cloud_platform/system/ptp/`):
  `PTPSetupExecutorKeywords.add_all_ptp_configurations()` iterates ptp4l/phc2sys/**ts2phc**/
  clock setup lists and `add_all_ptp_configurations_for_ts2phc_service()` →
  `ptp_instance_add_and_assign_hosts(obj, "ts2phc")` (PROVEN). `PTPTeardownExecutorKeywords.
  delete_all_ptp_configurations()`, `PTPVerifyConfigKeywords.verify_all_ptp_configurations()`,
  `PTPStatusCollectorKeywords.collect_all_ptp_status()`,
  `PTPScenarioExecutorKeywords.execute_test_scenario(scenario, request)`.
- **ts2phc specifics**: config file path convention
  `/etc/linuxptp/ptpinstance/ts2phc-<instance>.conf`; `gnss_monitoring_keywords.
  verify_ts2phc_config_serialport(instance, expected)` validates `nmea_serialport`;
  `pmc_get_default_data_set_object.get_ts2phc_nmea_serialport()`; `GnssKeywords.extract_gnss_port`
  reads the ts2phc instance parameters (PROVEN). linuxptp services run as systemd instances
  (`ts2phc@<name>`, `ptp4l@<name>`), controlled via `SystemCTLKeywords`.

> ts2phc is well-supported precedent. A new ts2phc/PTP test should: define/extend a
> `resources/ptp/*.json5` template, render it via `PTPSetupKeywords`, apply with
> `PTPSetupExecutorKeywords`, verify with `PTPVerifyConfigKeywords` /
> `PTPScenarioExecutorKeywords`, gate with the right `lab_has_*` marker, and download logs in
> an `addfinalizer` teardown.

---

## 6. QUICK REFERENCE — canonical imports (PROVEN, copy-paste)
```python
from pytest import mark
from framework.logging.automation_logger import get_logger
from framework.resources.resource_finder import get_stx_resource_path
from framework.validation.validation import validate_equals, validate_equals_with_retry, fail
from keywords.cloud_platform.ssh.lab_connection_keywords import LabConnectionKeywords
from keywords.files.file_keywords import FileKeywords
# ... subsystem keywords, e.g.:
from keywords.cloud_platform.system.host.system_host_list_keywords import SystemHostListKeywords
```
Skeleton of an idiomatic test:
```python
@mark.p1
@mark.lab_has_ptp
def test_something(request):
    """One-line summary.

    Test Steps:
        - ...
    Preconditions:
        - ...
    """
    ssh = LabConnectionKeywords().get_active_controller_ssh()

    def _teardown():
        get_logger().log_teardown_step("Downloading logs")
        FileKeywords(ssh).download_file("/var/log/user.log",
            f"{get_logger().get_test_case_log_dir()}/user.log")
    request.addfinalizer(_teardown)

    get_logger().log_info("Do the thing")
    observed = SomeKeywords(ssh).do_the_thing()
    validate_equals(observed, "expected", "the thing is correct")
```

---

## 7. OPEN / UNVERIFIED (honest limits)
- `LabConfig` / `LoggerConfig` / `ValidationResponse` method surfaces are PRESUMED from usage,
  not read in full.
- The exact `SSHConnection.get_return_code()` name and `ResultCollector`/`RunContextLoader`
  internals were inferred from call sites, not fully read.
- No literal `TEST_SCOPE_VALIDATION` / "prove-the-negative-first" rule was found — the
  equivalent is the alarm-set→alarm-clear scenario idiom (§4.4).
- Whether bare `pytest` (bypassing `test_executor.py`) is an accepted dev workflow is PRESUMED.
- Marker gating is confirmed executor-side (`TestCapabilityMatcher`); a parallel runtime
  `pytest_runtest_setup` skip based on markers was NOT found and is assumed absent.
