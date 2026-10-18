# Sandboxed code analysis: threat model and configuration

Feature 10 lets an agent write Python and run it over a dataset the researcher attached
(CodeAct, Wang et al., ICML 2024, arXiv:2402.01030; Biomni, doi:10.1101/2025.05.30.656746; BixBench,
arXiv:2503.00096; Data Interpreter, arXiv:2402.18679). This page explains what the sandbox defends against,
what it does not, and how to run it in production.

Code: `phronesis/agentic/sandbox/policy.py` (AST policy), `phronesis/agentic/sandbox/runner.py` (subprocess),
`phronesis/agentic/sandbox/__init__.py` (dataset scoping), `phronesis/agentic/analyst.py` (the CodeAct phase),
`phronesis/extensions/sandbox.py` (the `analysis_run_python` registry tool).

## Threat model

The code is untrusted even though our own model writes it. The model reads text it did not write: the question,
column names and cell values of the uploaded file, earlier tool output, and retrieved papers and web pages. Any
of these can carry instructions aimed at the model. This is indirect prompt injection (Greshake et al. 2023,
AISec '23, arXiv:2302.12173). An injected instruction could ask the model to write code that reads secrets,
calls out to the network, or reads another user's files.

Assets we protect:
- server secrets: API keys and tokens in the process environment;
- other users' uploads, and anything else on the server's disk;
- the network position of the server (no outbound calls from analysis code);
- the availability of the API process (no runaway CPU, memory or output).

Out of scope: the integrity of the analysis itself. Code can compute a wrong number, and the model can misread
it. That risk is handled by showing the code, its output and the dataset with the answer, by the verifier, and
by allowing "insufficient information" as an answer.

## The layers

None of these layers is a security boundary on its own, and together they are still **defence in depth, not a
boundary against a determined attacker.** A Python process with numpy, pandas and scanpy loaded has a very large
attack surface, and pure-Python sandboxes have a long history of escapes.

1. **Spotlighting of untrusted text** (Hines et al. 2024, arXiv:2403.14720). The dataset preview, previous
   outputs and the question reach the model inside `<untrusted_data>` blocks, with instruction-like spans
   neutralised (`phronesis/agentic/guardrails.py`). The system prompt says this text is data.
2. **AST policy** (`policy.py`), applied before anything runs:
   - an import allowlist: numpy, pandas, scipy, sklearn, matplotlib, anndata, scanpy, statsmodels (if
     installed), math, statistics, json, re, collections, itertools, functools, typing, datetime;
   - banned names, and the same names as attributes (`pandas.io.common.os`): os, sys, subprocess, socket,
     shutil, ctypes, importlib, builtins, pickle, marshal, pathlib;
   - banned calls: eval, exec, compile, `__import__`, globals, locals, vars, setattr, delattr, breakpoint, input;
     getattr and hasattr only with a plain, non-dunder string literal;
   - any name or attribute starting with `__`, frame, generator and traceback internals (`gi_frame`,
     `f_globals`, `tb_frame`, ...), and format strings that reach dunders;
   - `open()` only in read mode, on a relative path inside the work directory (the input files, by name);
   - file writers (`to_csv`, `savefig`, `np.save`, `write_h5ad`, ...) only to a literal relative path inside the
     work directory, never over an input file;
   - pickle-based I/O (`read_pickle`, `to_pickle`, `allow_pickle=True`), which runs code on load.
3. **A separate process.** `sys.executable -I` (isolated mode: PYTHON* variables ignored, no user site-packages,
   the script directory not on `sys.path`). `-S` is not used because the scientific stack lives in
   site-packages. The working directory is a fresh temporary `work/` folder that holds only read-only copies of
   the attached files. The bootstrap, code and output files live in a sibling folder the code cannot name
   (the policy rejects `..`). Everything is deleted afterwards.
4. **Runtime guards in the child**, installed by the bootstrap before user code runs: user code executes with a
   restricted builtins dict. That dict has an `__import__` that re-checks the allowlist, a read-only `open()`
   confined to the work directory (this catches computed paths the AST cannot see), a `getattr` that refuses
   dunder and banned names, and no eval, exec, compile, vars or globals. stdout and stderr are capped.
5. **No secrets in the environment.** The child's environment is built from an allowlist: PATH,
   SYSTEMROOT/WINDIR, PATHEXT, TEMP/TMP (pointing into the run's own temp folder), PYTHONHASHSEED, and the
   thread and backend settings the runner sets itself (MPLBACKEND=Agg, MPLCONFIGDIR, NUMBA_CACHE_DIR, *_NUM_THREADS=1).
   API keys and tokens are never copied. `tests/test_sandbox.py` checks this.
6. **No network.** Before user code runs, `socket.socket` is replaced by a subclass whose constructor raises,
   and `create_connection`, `getaddrinfo`, `gethostbyname` and related functions raise `PermissionError`. This is a
   monkeypatch inside the interpreter, and it is the weakest layer. Native extensions that open sockets
   themselves are not covered. Production must also block the network at the OS or container level.
7. **Limits.** A wall-clock timeout (default 60 s) kills the whole process tree (`taskkill /T` on Windows, the
   process group on POSIX). On POSIX the bootstrap sets RLIMIT_AS (default 2048 MB), RLIMIT_CPU, RLIMIT_NOFILE
   and RLIMIT_FSIZE. On Windows it assigns itself to a Job Object with a per-process memory limit, a CPU-time
   limit and an active-process limit of 1, so the code cannot start child processes. This step is best effort:
   if it fails, the run continues with only the timeout. Output is capped (256 KB stdout, 3 figures).
8. **Dataset scoping.** The model never names a path. The analyst phase binds a scope that holds the run's user
   id and the ids of the datasets attached to this question. The tool resolves the requested ids through
   `DatasetStore.resolve_for_user` for that user only, checks that each path lies under the upload root, and
   copies the files in. A call with no bound scope reads no files. The planner cannot use the tool to reach
   another user's upload or a server path.
9. **Off by default.** Nothing runs unless `PHRONESIS_SANDBOX=on`. When it is off the tool is not registered,
   and if it is called anyway it refuses.

Known gaps (do not rely on the in-process layers for these):
- Native code in allowed packages (numpy, scipy, h5py, numba) can do anything the OS lets the process do.
- A library function that writes files internally, or that evaluates strings, is outside what the AST can see.
- The matplotlib font cache directory (`<tmp>/phronesis-sandbox-mpl`) is shared between runs so the cache is not
  rebuilt every time. One run could therefore alter it for the next run.
- Memory and CPU limits are best effort on Windows.

## Production setup

Run the sandbox child inside an OS-level isolation boundary. Any one of these works:
- **A container** per run (Docker or Podman) with `--network none`, `--read-only`, a tmpfs work directory,
  the dataset mounted read-only, `--memory`/`--cpus`/`--pids-limit`, `--cap-drop ALL`,
  `--security-opt no-new-privileges` and a non-root user. This matches the BixBench harness, which runs its agent
  in a Docker image.
- **gVisor** (`runsc`) as the container runtime, which adds a user-space kernel between the code and the host.
- **firejail** or **bubblewrap** on a single Linux host: `--net=none`, a private /tmp, read-only binds of the
  input files, and seccomp.

In each case, keep the in-process layers above as well. They give useful errors back to the model and cut the
attack surface. To switch, replace the `subprocess.Popen` call in `runner.py` with the container invocation; the
bootstrap and the JSON output contract stay the same.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PHRONESIS_SANDBOX` | off | `on` enables the `analysis_run_python` tool and the analyst phase |
| `PHRONESIS_SANDBOX_TIMEOUT_S` | 60 | Wall-clock limit per cell (5 to 110 s; the registry timeout is 120 s) |
| `PHRONESIS_SANDBOX_MEMORY_MB` | 2048 | Memory limit per cell (RLIMIT_AS or the Job Object) |
| `agent_options.enable_code_analysis` | true | A per-request switch; false skips the analyst phase |

The analyst phase also needs an agentic framework, a CSV/TSV/h5ad attachment, and a question with analysis
intent (analyse, compute, statistic, correlate, regress, differential, survival, plot, distribution, compare
groups, test whether). When no model is available it does not run model-written code. Instead it runs a fixed
descriptive-statistics script (describe, plus group summaries when a grouping column is obvious). That script
passes the same policy.

## References

- Greshake, K. et al. 2023. Not What You've Signed Up For: Compromising Real-World LLM-Integrated Applications
  with Indirect Prompt Injection. AISec '23. arXiv:2302.12173, doi:10.1145/3605764.3623985.
- Hines, K. et al. 2024. Defending Against Indirect Prompt Injection Attacks With Spotlighting. arXiv:2403.14720.
- Wang, X. et al. 2024. Executable Code Actions Elicit Better LLM Agents (CodeAct). ICML 2024. arXiv:2402.01030.
- Huang, K. et al. 2025. Biomni: A General-Purpose Biomedical AI Agent. bioRxiv. doi:10.1101/2025.05.30.656746.
- Mitchener, L. et al. 2025. BixBench. arXiv:2503.00096.
- Hong, S. et al. 2024. Data Interpreter: An LLM Agent For Data Science. arXiv:2402.18679.
