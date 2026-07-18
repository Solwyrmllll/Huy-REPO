"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          NIMO AGENT v10.0 — ENGINEERING WORKFLOW EDITION                    ║
║          Đồng hành: Lê Quang Huy                                            ║
║                                                                              ║
║  KIẾN TRÚC LÕI:                                                              ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │  WORKFLOW STATE MACHINE (6 phase, hard-gated)                       │    ║
║  │                                                                     │    ║
║  │  /spec ──► /plan ──► /build ──► /test ──► /review ──► /ship        │    ║
║  │     │        │         │          │          │           │          │    ║
║  │   GATE      GATE      GATE       GATE       GATE        GATE       │    ║
║  │  (spec    (plan     (build     (tests     (review    (checklist    │    ║
║  │  saved)   saved)    saved)     pass)      approved)   green)       │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  FILE PERSISTENCE: mỗi phase tự động lưu artifact ra disk                   ║
║    .nimo/spec.md  .nimo/plan.md  .nimo/src/  .nimo/tests/                   ║
║    .nimo/review.md  .nimo/ship.md                                            ║
║                                                                              ║
║  BUILD→TEST LOOP: /build tự động trigger /test sau mỗi slice                ║
║  3-AGENT FANOUT: /review chạy code-reviewer + test-engineer + security      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import sys, io, os, re, json, logging, time, asyncio, subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv(os.path.expanduser("~/.env_nimo"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_log = logging.getLogger("nimo")
console = Console()

# ════════════════════════════════════════════════════════════════════════
# [1] CONFIG
# ════════════════════════════════════════════════════════════════════════
VERSION        = "10.0.0"
PRIMARY_MODEL  = "nousresearch/hermes-3-llama-3.1-405b"
COMPRESS_MODEL = "nousresearch/hermes-3-llama-3.1-8b"
OR_BASE        = "https://openrouter.ai/api/v1"
OR_SITE        = "https://github.com/nimo-agent"
OR_APP         = f"Nimo Agent v{VERSION}"
MAX_CTX        = 128_000
COMPRESS_AT    = 0.90

TEMP: Dict[str, float] = {
    "spec": 0.40, "plan": 0.30, "build": 0.35, "test": 0.10,
    "review": 0.20, "ship": 0.20, "chat": 0.50, "compress": 0.05,
    "arch": 0.65, "debug": 0.10,
    # personas
    "persona_reviewer": 0.20, "persona_tester": 0.10, "persona_security": 0.10,
}
MAXTOK: Dict[str, int] = {
    "spec": 8000, "plan": 8000, "build": 16384, "test": 8000,
    "review": 8000, "ship": 6000, "chat": 8000, "compress": 1500,
    "arch": 16384, "debug": 8192,
    "persona_reviewer": 6000, "persona_tester": 6000, "persona_security": 6000,
}

# ════════════════════════════════════════════════════════════════════════
# [2] WORKFLOW STATE MACHINE
# ════════════════════════════════════════════════════════════════════════
class Phase(str, Enum):
    IDLE   = "idle"
    SPEC   = "spec"
    PLAN   = "plan"
    BUILD  = "build"
    TEST   = "test"
    REVIEW = "review"
    SHIP   = "ship"
    DONE   = "done"

# Thứ tự phase — dùng để validate hard gate
PHASE_ORDER = [Phase.IDLE, Phase.SPEC, Phase.PLAN, Phase.BUILD,
               Phase.TEST, Phase.REVIEW, Phase.SHIP, Phase.DONE]

# Gate conditions: phase X cần artifact nào để unlock phase kế tiếp
GATE_REQUIRES: Dict[Phase, str] = {
    Phase.SPEC:   "",             # idle → spec: luôn cho phép
    Phase.PLAN:   "spec.md",      # spec → plan: cần spec.md
    Phase.BUILD:  "plan.md",      # plan → build: cần plan.md
    Phase.TEST:   "build_done",   # build → test: cần ít nhất 1 file src
    Phase.REVIEW: "test_passed",  # test → review: cần tests pass
    Phase.SHIP:   "review.md",    # review → ship: cần review.md approved
}

GATE_MESSAGES: Dict[Phase, str] = {
    Phase.PLAN:   "❌ HARD GATE: /plan bị block — chưa có spec.\n   → Chạy /spec <yêu cầu> trước.",
    Phase.BUILD:  "❌ HARD GATE: /build bị block — chưa có plan.\n   → Chạy /plan <task> trước.",
    Phase.TEST:   "❌ HARD GATE: /test bị block — chưa build gì.\n   → Chạy /build <task> trước.",
    Phase.REVIEW: "❌ HARD GATE: /review bị block — tests chưa pass.\n   → Chạy /test và đảm bảo tests xanh trước.",
    Phase.SHIP:   "❌ HARD GATE: /ship bị block — chưa có review approved.\n   → Chạy /review trước.",
}

@dataclass
class WorkflowState:
    """Trạng thái toàn bộ workflow của một project."""
    project_name:   str = ""
    current_phase:  Phase = Phase.IDLE
    phases_done:    List[str] = field(default_factory=list)
    # Artifacts
    spec_content:   str = ""
    plan_content:   str = ""
    build_slices:   List[Dict] = field(default_factory=list)  # {name, file, status}
    test_results:   Dict = field(default_factory=dict)         # {passed, failed, output}
    review_verdict: str = ""   # "APPROVE" | "REQUEST_CHANGES" | ""
    ship_checklist: Dict = field(default_factory=dict)
    # Flags
    tests_passed:   bool = False
    review_approved: bool = False
    # Session
    created_at:     str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at:     str = field(default_factory=lambda: datetime.now().isoformat())

    def touch(self):
        self.updated_at = datetime.now().isoformat()

    def phase_index(self, p: Phase) -> int:
        return PHASE_ORDER.index(p)

    def can_enter(self, target: Phase) -> Tuple[bool, str]:
        """Hard gate check. Returns (ok, error_message)."""
        wf_dir = _wf_dir()
        if target == Phase.SPEC:
            return True, ""
        if target == Phase.PLAN:
            ok = (wf_dir / "spec.md").exists() and bool(self.spec_content)
            return ok, "" if ok else GATE_MESSAGES[Phase.PLAN]
        if target == Phase.BUILD:
            ok = (wf_dir / "plan.md").exists() and bool(self.plan_content)
            return ok, "" if ok else GATE_MESSAGES[Phase.BUILD]
        if target == Phase.TEST:
            src_dir = wf_dir / "src"
            ok = src_dir.exists() and any(src_dir.iterdir())
            return ok, "" if ok else GATE_MESSAGES[Phase.TEST]
        if target == Phase.REVIEW:
            ok = self.tests_passed
            return ok, "" if ok else GATE_MESSAGES[Phase.REVIEW]
        if target == Phase.SHIP:
            ok = self.review_approved
            return ok, "" if ok else GATE_MESSAGES[Phase.SHIP]
        return True, ""

# Singleton workflow state
wf = WorkflowState()

def _wf_dir() -> Path:
    """Thư mục .nimo/ trong working directory."""
    d = Path.cwd() / ".nimo"
    d.mkdir(exist_ok=True)
    (d / "src").mkdir(exist_ok=True)
    (d / "tests").mkdir(exist_ok=True)
    return d

def _save_artifact(filename: str, content: str) -> Path:
    p = _wf_dir() / filename
    p.write_text(content, encoding="utf-8")
    return p

def _load_artifact(filename: str) -> str:
    p = _wf_dir() / filename
    return p.read_text(encoding="utf-8") if p.exists() else ""

def _save_state():
    p = _wf_dir() / "state.json"
    data = asdict(wf)
    data["current_phase"] = wf.current_phase.value
    data["phases_done"]   = [ph for ph in wf.phases_done]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_state():
    p = _wf_dir() / "state.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k == "current_phase":
                wf.current_phase = Phase(v)
            elif hasattr(wf, k):
                setattr(wf, k, v)
    except Exception as e:
        _log.warning(f"State load failed: {e}")

# ════════════════════════════════════════════════════════════════════════
# [3] WORKFLOW PROGRESS DISPLAY
# ════════════════════════════════════════════════════════════════════════
PHASE_ICONS = {
    Phase.IDLE:   "⬜",
    Phase.SPEC:   "📋",
    Phase.PLAN:   "📐",
    Phase.BUILD:  "🔨",
    Phase.TEST:   "🧪",
    Phase.REVIEW: "🔍",
    Phase.SHIP:   "🚀",
    Phase.DONE:   "✅",
}

def _print_workflow_status():
    """Hiển thị progress bar workflow."""
    wf_dir = _wf_dir()
    phases = [Phase.SPEC, Phase.PLAN, Phase.BUILD, Phase.TEST, Phase.REVIEW, Phase.SHIP]
    
    parts = []
    for p in phases:
        icon = PHASE_ICONS[p]
        name = p.value.upper()
        if p.value in wf.phases_done:
            parts.append(f"[bold green]{icon} {name}[/]")
        elif p == wf.current_phase:
            parts.append(f"[bold yellow]▶ {icon} {name}[/]")
        else:
            parts.append(f"[dim]{icon} {name}[/]")
    
    bar = " → ".join(parts)
    project = f"[bold]{wf.project_name}[/] | " if wf.project_name else ""
    console.print(f"\n   {project}{bar}\n")

# ════════════════════════════════════════════════════════════════════════
# [4] LLM ENGINE
# ════════════════════════════════════════════════════════════════════════
def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=OR_BASE,
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        default_headers={"HTTP-Referer": OR_SITE, "X-Title": OR_APP},
    )

def _split_think(text: str) -> Tuple[str, str]:
    m = re.search(r"<think>(.*?)</think>\s*", text, re.DOTALL)
    if m:
        return m.group(1).strip(), text[m.end():].strip()
    return "", text

class NimoSession:
    """Chat session cho một phase, có compress và memory purifier."""

    def __init__(self, system_prompt: str, task: str = "chat"):
        self.task = task
        self.history: List[Dict] = [{"role": "system", "content": system_prompt}]
        self.cot_log: List[str]  = []

    def push(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def _inject_cot(self, cot_hint: str) -> List[Dict]:
        """Inject CoT hint vào user message cuối mà không gây 400 error."""
        msgs = [dict(m) for m in self.history]
        if cot_hint and msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += f"\n\n[REASONING DIRECTIVE — use in <think>: {cot_hint}]"
        return msgs

    async def compress(self):
        est = sum(len(m["content"].split()) * 1.3 for m in self.history)
        if est <= MAX_CTX * COMPRESS_AT or len(self.history) < 6:
            return
        console.print("[dim yellow]   ⟳ Context > 90% — compressing...[/]")
        mid  = len(self.history) // 2
        blob = "\n".join(f"{m['role']}: {m['content'][:1000]}" for m in self.history[1:mid])
        try:
            c    = _client()
            resp = await c.chat.completions.create(
                model=COMPRESS_MODEL,
                messages=[{"role": "user", "content":
                    f"Summarize preserving: filenames, class/fn names, APIs, schemas, bugs, decisions:\n\n{blob}"}],
                temperature=TEMP["compress"], max_tokens=MAXTOK["compress"],
            )
            summary = resp.choices[0].message.content or ""
            self.history = [
                self.history[0],
                {"role": "system", "content": f"[PRIOR CONTEXT SUMMARY]\n{summary}"},
                *self.history[mid:],
            ]
            console.print("[dim green]   ✔ Compressed[/]")
        except Exception as e:
            _log.warning(f"compress failed: {e}")

    async def stream(
        self,
        user_msg: str,
        cot_hint: str = "",
        label: str = "NIMO",
        color: str = "rgb(0,200,255)",
        silent_think: bool = True,
    ) -> str:
        """Stream một turn, trả về answer text."""
        self.push("user", user_msg)
        await self.compress()

        msgs    = self._inject_cot(cot_hint)
        temp    = TEMP.get(self.task, 0.5)
        max_tok = MAXTOK.get(self.task, 8000)
        reply   = ""
        truncated = False

        with Live(Text("⏳ thinking..."), console=console,
                  refresh_per_second=15, transient=True) as live:
            try:
                stream = await _client().chat.completions.create(
                    model=PRIMARY_MODEL, messages=msgs,
                    temperature=temp, max_tokens=max_tok, stream=True,
                )
                async for chunk in stream:
                    if chunk.choices:
                        tok = chunk.choices[0].delta.content or ""
                        fr  = chunk.choices[0].finish_reason
                        if fr == "length":
                            truncated = True
                        reply += tok
                        live.update(Text(reply[-300:]), refresh=True)
            except Exception as e:
                console.print(f"[red]API Error: {e}[/]")
                return ""

        think, answer = _split_think(reply)
        if think:
            self.cot_log.append(think)
            if not silent_think:
                console.print(f"[dim]   🧠 CoT ({len(think.split())} words → Analytic Memory)[/]")
            else:
                console.print(f"[dim]   🧠 [{label}] reasoning: {len(think.split())} words[/]")

        console.print(f"\n[bold {color}]◆ {label}[/]")
        console.print(Markdown(answer))

        if truncated:
            console.print("[bold rgb(255,100,0)]   ⚠ TRUNCATED — chia nhỏ request[/]")

        # Lưu cả think+answer vào history (model cần nhớ reasoning của nó)
        self.push("assistant", reply)
        return answer

# ════════════════════════════════════════════════════════════════════════
# [5] SYSTEM PROMPTS
# ════════════════════════════════════════════════════════════════════════
_BASE_SYSTEM = """\
<|im_start|>system
You are Nimo — Senior Software Architect & Engineering Team Lead.
User: Huy. Address yourself as Nimo, address user as Huy.
Project: Bot management & AI chatbot selling platform.
Stack: FastAPI + PostgreSQL + Redis + Docker + python-telegram-bot/discord.py/ccxt.

MANDATORY: Use <think>...</think> before every non-trivial response.
CODE STANDARDS: async-first Python, type hints, dataclass/pydantic, explicit error handling.
FORMAT: ## headers, ```language code blocks, complete code never truncated.
<|im_end|>"""

_SPEC_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: spec-driven-development
You are writing the project specification — the shared source of truth.
RULES:
- Surface ALL assumptions before writing spec content
- Reframe vague requirements into concrete success criteria
- Spec must cover 6 areas: Objective, Commands, Project Structure, Code Style, Testing Strategy, Boundaries
- Boundaries use 3-tier: Always / Ask First / Never
- End every spec with: Open Questions (anything still unresolved)
- The spec is a LIVING DOCUMENT — update when scope changes
OUTPUT: A complete spec.md document, ready to save.
"""

_PLAN_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: planning-and-task-breakdown
You are breaking down the spec into an ordered implementation plan.
RULES:
- Read-only mode: understand before planning. Do NOT write code.
- Map dependency graph bottom-up (DB schema → models → API → UI)
- Slice VERTICALLY: each task delivers working end-to-end functionality
- Task sizing: XS(1 file) S(1-2) M(3-5) L(5-8) XL(8+=SPLIT IT)
- Every task needs: Description, Acceptance Criteria, Verification command, Dependencies, Files, Size
- Add explicit Checkpoints after every 2-3 tasks
- Flag high-risk tasks to be tackled FIRST (fail fast)
OUTPUT: A complete plan.md document with ordered tasks and checkpoints.
"""

_BUILD_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: incremental-implementation
You are implementing ONE SLICE at a time.
RULES:
- Rule 0: Simplest thing that could work. No premature abstractions.
- Rule 0.5: SCOPE DISCIPLINE — touch ONLY what the current task requires.
  If you notice something outside scope, NOTE IT, don't fix it.
- Rule 1: One thing per slice. Never mix concerns.
- Rule 2: After each slice, code must COMPILE and existing tests must PASS.
- Rule 3: Feature flags for incomplete features.
- Rule 4: Safe defaults — new code defaults to conservative behavior.
- Rule 5: Each slice must be independently revertable.
OUTPUT FORMAT per slice:
  ## SLICE [N]: [name]
  ## SCOPE — what this slice changes and explicitly what it does NOT touch
  ## CODE — complete, working implementation
  ## NOTICED BUT NOT TOUCHING — out-of-scope observations
"""

_TEST_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: test-driven-development
You are writing tests following the TDD cycle.
RULES:
- Write FAILING test first (RED) — a test that passes immediately proves nothing
- Write minimal code to pass (GREEN)
- Refactor while keeping green (REFACTOR)
- Prove-It Pattern for bugs: reproduce with failing test BEFORE fixing
- Test levels: pure logic=unit, crosses boundary=integration, critical flow=e2e
- DAMP over DRY: each test is self-contained and readable
- Mock ONLY at system boundaries (DB, external API, email)
- Prefer: real impl > fake > stub > mock (interaction)
- Test names read like specifications: "does X when Y"
- Arrange-Act-Assert pattern always
OUTPUT: Working test code that follows pytest conventions.
"""

_REVIEW_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: code-review-and-quality
You are conducting a thorough 5-axis code review.
AXES:
1. CORRECTNESS — matches spec? edge cases? race conditions? off-by-one?
2. READABILITY — names clear? control flow obvious? no dead code?
3. ARCHITECTURE — fits existing patterns? clean boundaries? right abstraction?
4. SECURITY — input validated? secrets safe? auth checked? queries parameterized?
5. PERFORMANCE — N+1? unbounded loops? sync where async needed?

SEVERITY LABELS (use on every finding):
- Critical: must fix before merge (security vuln, data loss, broken)
- Important: should fix (missing test, wrong abstraction)  
- Nit: optional minor (naming, style)
- FYI: informational, no action needed

APPROVAL STANDARD: approve when it DEFINITELY IMPROVES overall code health.
Do NOT rubber-stamp. Do NOT soften real issues.
FINAL VERDICT must be: APPROVE or REQUEST CHANGES (explicit, no ambiguity).
"""

_SECURITY_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: security-and-hardening (Security Auditor persona)
You are a Security Engineer. Focus on EXPLOITABLE vulnerabilities, not theoretical risks.
OWASP Top 10 is your minimum baseline.
SEVERITY: Critical (block release) / High (fix before release) / Medium / Low / Info
Every Critical/High finding must include: Location, Description, Impact, Proof of Concept, Recommendation.
"""

_TESTER_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: test-driven-development (Test Engineer persona)  
You are a QA Engineer. Analyze coverage gaps and write tests.
Prove-It Pattern for bugs. Test at the right level.
Coverage scenarios for every function: happy path, empty, boundary, error, concurrency.
"""

_SHIP_SYSTEM = _BASE_SYSTEM + """

ACTIVE SKILL: shipping-and-launch
You are preparing a production launch.
EVERY launch must be: REVERSIBLE (rollback plan), OBSERVABLE (monitoring), INCREMENTAL (staged rollout).
Pre-launch checklist covers: Code Quality, Security, Performance, Accessibility, Infrastructure, Documentation.
Staged rollout: 5% → 25% → 50% → 100% with explicit hold criteria per stage.
Rollout decision thresholds must be defined BEFORE deploying.
ROLLBACK PLAN is mandatory — trigger conditions + steps + time-to-rollback estimate.
"""

# ════════════════════════════════════════════════════════════════════════
# [6] CoT HINTS per phase (inject vào <think> của model)
# ════════════════════════════════════════════════════════════════════════
COT_HINTS: Dict[str, str] = {
    "spec": (
        "Spec-driven-development active. "
        "FIRST: list all assumptions I'm making. "
        "THEN: reframe vague requirements into testable success criteria. "
        "COVER all 6 areas: Objective, Commands, Structure, Style, Testing, Boundaries. "
        "SURFACE: open questions I can't answer without Huy's input."
    ),
    "plan": (
        "Planning active. READ-ONLY MODE — no code yet. "
        "FIRST: draw the dependency graph (DB → models → API → UI). "
        "THEN: slice vertically, not horizontally. "
        "CHECK: every task has acceptance criteria and verification command. "
        "FLAG: which tasks are highest risk and should be tackled first."
    ),
    "build": (
        "Incremental-implementation active. ONE SLICE ONLY. "
        "FIRST: confirm scope — what exactly does this slice change and what does it NOT touch. "
        "THEN: implement the simplest thing that could work. "
        "CHECK: does this compile? do existing tests still pass? "
        "NOTE (but do NOT fix): anything outside scope I noticed."
    ),
    "test": (
        "TDD active. CYCLE: RED (write failing test) → GREEN (minimal code) → REFACTOR. "
        "FIRST: write the test. It MUST fail initially. "
        "THEN: write minimal implementation. "
        "CHECK: test names read as specs? edge cases covered? mocks only at boundaries?"
    ),
    "review": (
        "Code review active. 5-axis review: correctness, readability, architecture, security, performance. "
        "REVIEW TESTS FIRST — they reveal intent and coverage. "
        "LABEL every finding: Critical / Important / Nit / FYI. "
        "END with explicit APPROVE or REQUEST CHANGES. No ambiguity."
    ),
    "ship": (
        "Shipping-and-launch active. "
        "FIRST: run through pre-launch checklist — Code, Security, Performance, Infra, Docs. "
        "THEN: define staged rollout thresholds. "
        "THEN: write rollback plan with explicit trigger conditions. "
        "VERIFY: every launch is reversible, observable, incremental."
    ),
}

# ════════════════════════════════════════════════════════════════════════
# [7] PHASE HANDLERS
# ════════════════════════════════════════════════════════════════════════

async def phase_spec(arg: str) -> None:
    """Phase 1: Spec-driven development."""
    wf.current_phase = Phase.SPEC
    if not wf.project_name:
        wf.project_name = arg.split()[0] if arg.split() else "project"
    _print_workflow_status()

    sess = NimoSession(_SPEC_SYSTEM, task="spec")
    
    prompt = f"""Viết spec đầy đủ cho project này:

"{arg}"

Spec phải cover 6 areas: Objective, Commands, Project Structure, Code Style, Testing Strategy, Boundaries.
Surface tất cả assumptions ngay đầu. Kết thúc với Open Questions.
Output format: markdown document hoàn chỉnh, sẵn sàng lưu vào spec.md.
"""
    answer = await sess.stream(prompt, cot_hint=COT_HINTS["spec"],
                               label="NIMO [SPEC]", color="rgb(100,200,255)")
    
    if answer:
        wf.spec_content = answer
        path = _save_artifact("spec.md", f"# Spec: {wf.project_name}\n\n{answer}")
        wf.phases_done.append(Phase.SPEC.value)
        wf.touch()
        _save_state()
        console.print(f"\n[bold green]   ✔ spec.md saved → {path}[/]")
        console.print("[dim]   → Tiếp theo: /plan <task cụ thể cần implement>[/]")

async def phase_plan(arg: str) -> None:
    """Phase 2: Planning & task breakdown."""
    ok, msg = wf.can_enter(Phase.PLAN)
    if not ok:
        console.print(f"[bold red]{msg}[/]")
        return

    wf.current_phase = Phase.PLAN
    _print_workflow_status()

    sess = NimoSession(_PLAN_SYSTEM, task="plan")
    
    spec_ctx = f"SPEC ĐÃ ĐƯỢC APPROVED:\n\n{wf.spec_content}" if wf.spec_content else ""
    prompt = f"""{spec_ctx}

Tạo implementation plan cho task sau:
"{arg}"

Mỗi task cần: Description, Acceptance Criteria (testable), Verification command, Dependencies, Files touched, Size (XS/S/M/L).
Sắp xếp theo dependency graph. Thêm Checkpoints sau mỗi 2-3 tasks.
Đặt high-risk tasks lên đầu (fail fast).
Output: plan.md hoàn chỉnh.
"""
    answer = await sess.stream(prompt, cot_hint=COT_HINTS["plan"],
                               label="NIMO [PLAN]", color="rgb(100,255,200)")
    
    if answer:
        wf.plan_content = answer
        path = _save_artifact("plan.md", answer)
        wf.phases_done.append(Phase.PLAN.value)
        wf.touch()
        _save_state()
        console.print(f"\n[bold green]   ✔ plan.md saved → {path}[/]")
        console.print("[dim]   → Tiếp theo: /build <tên slice đầu tiên>[/]")
        # Print task summary
        _print_task_summary(answer)

def _print_task_summary(plan_md: str):
    """Trích task list từ plan.md và hiển thị summary."""
    tasks = re.findall(r"[-*] \[[ x]\] Task\s*\d*:?\s*(.+)", plan_md)
    if tasks:
        console.print("\n[dim]   📋 Tasks extracted:[/]")
        for i, t in enumerate(tasks[:10], 1):
            console.print(f"   [dim]{i}. {t.strip()[:80]}[/]")
        if len(tasks) > 10:
            console.print(f"   [dim]   ... và {len(tasks)-10} tasks nữa[/]")

async def phase_build(arg: str) -> None:
    """Phase 3: Incremental build + auto-test sau mỗi slice."""
    ok, msg = wf.can_enter(Phase.BUILD)
    if not ok:
        console.print(f"[bold red]{msg}[/]")
        return

    wf.current_phase = Phase.BUILD
    _print_workflow_status()

    slice_num = len(wf.build_slices) + 1
    sess = NimoSession(_BUILD_SYSTEM, task="build")

    ctx_parts = []
    if wf.spec_content:
        ctx_parts.append(f"SPEC (context):\n{wf.spec_content[:2000]}...")
    if wf.plan_content:
        ctx_parts.append(f"PLAN (context):\n{wf.plan_content[:2000]}...")
    if wf.build_slices:
        done = [s["name"] for s in wf.build_slices]
        ctx_parts.append(f"SLICES ĐÃ BUILD: {', '.join(done)}")

    # Đọc existing src files để inject context
    src_dir = _wf_dir() / "src"
    existing_files = list(src_dir.glob("**/*.py"))[:5]
    for f in existing_files:
        ctx_parts.append(f"EXISTING: {f.name}\n```python\n{f.read_text()[:500]}```")

    ctx = "\n\n---\n\n".join(ctx_parts)

    prompt = f"""{ctx}

Implement SLICE {slice_num}: "{arg}"

Quy tắc bắt buộc:
1. Chỉ implement đúng scope của slice này
2. Code phải complete và runnable (không truncate)  
3. Phải include type hints và error handling
4. Sau code: liệt kê NOTICED BUT NOT TOUCHING (ghi nhận, không sửa)

Lưu files vào: .nimo/src/<module_name>.py
"""
    answer = await sess.stream(prompt, cot_hint=COT_HINTS["build"],
                               label=f"NIMO [BUILD — Slice {slice_num}]", color="rgb(255,200,0)")

    if not answer:
        return

    # Extract và lưu code blocks
    saved_files = _extract_and_save_code(answer, _wf_dir() / "src")
    
    slice_info = {
        "name": arg,
        "slice_num": slice_num,
        "files": saved_files,
        "status": "built",
    }
    wf.build_slices.append(slice_info)

    if Phase.BUILD.value not in wf.phases_done:
        wf.phases_done.append(Phase.BUILD.value)
    wf.touch()
    _save_state()

    if saved_files:
        console.print(f"\n[bold green]   ✔ Saved: {', '.join(saved_files)}[/]")

    # ── AUTO-TEST sau mỗi slice ──────────────────────────────────────
    console.print(f"\n[bold rgb(100,255,100)]   🧪 Auto-running /test for slice {slice_num}...[/]")
    await phase_test(f"slice {slice_num}: {arg}", auto_triggered=True)

def _extract_and_save_code(answer: str, target_dir: Path) -> List[str]:
    """Extract code blocks từ markdown và lưu vào target_dir."""
    saved = []
    # Tìm code blocks với filename comment
    patterns = [
        r"#\s*(?:File:|filename:|file:)\s*(.+?\.py)\n(.*?)```",
        r"```python\n#\s*(.+?\.py)\n(.*?)```",
        r"```python\n(.*?)```",
    ]
    
    # Pattern 1: có filename rõ ràng
    for m in re.finditer(r"(?:File: |filename: |# )([a-zA-Z0-9_/]+\.py)\n```python\n(.*?)```",
                         answer, re.DOTALL):
        fname, code = m.group(1).strip(), m.group(2)
        fpath = target_dir / Path(fname).name
        fpath.write_text(code, encoding="utf-8")
        saved.append(fpath.name)

    # Pattern 2: code block với # filename.py sebagai baris pertama
    if not saved:
        for m in re.finditer(r"```python\n#\s*([a-zA-Z0-9_/]+\.py)\n(.*?)```",
                             answer, re.DOTALL):
            fname, code = m.group(1).strip(), m.group(2)
            fpath = target_dir / Path(fname).name
            fpath.write_text(f"# {fname}\n{code}", encoding="utf-8")
            saved.append(fpath.name)

    return saved

async def phase_test(arg: str, auto_triggered: bool = False) -> None:
    """Phase 4: TDD — write tests, run them, report results."""
    ok, msg = wf.can_enter(Phase.TEST)
    if not ok:
        console.print(f"[bold red]{msg}[/]")
        return

    if not auto_triggered:
        wf.current_phase = Phase.TEST
        _print_workflow_status()

    sess = NimoSession(_TESTER_SYSTEM if auto_triggered else _TEST_SYSTEM, task="test")

    # Context: code đã build
    src_dir  = _wf_dir() / "src"
    test_dir = _wf_dir() / "tests"
    
    src_files_content = ""
    for f in sorted(src_dir.glob("*.py"))[:5]:
        src_files_content += f"\n# {f.name}\n```python\n{f.read_text()[:800]}```\n"

    existing_tests = ""
    for f in sorted(test_dir.glob("test_*.py"))[:3]:
        existing_tests += f"\n# {f.name} (existing)\n```python\n{f.read_text()[:400]}```\n"

    prompt = f"""Write tests for: "{arg}"

SOURCE CODE TO TEST:
{src_files_content}

{f'EXISTING TESTS:{existing_tests}' if existing_tests else ''}

TDD RULES:
1. Follow RED→GREEN→REFACTOR cycle
2. Test names must read as specifications
3. Cover: happy path, empty input, boundary, error path, concurrency
4. DAMP over DRY — each test self-contained
5. Mock ONLY at system boundaries (DB, external API)
6. Use pytest. All test functions start with test_

Output: Complete test file, save as test_{arg.split()[0].lower().replace(' ', '_')}.py
"""
    answer = await sess.stream(
        prompt, cot_hint=COT_HINTS["test"],
        label="NIMO [TEST]" if not auto_triggered else "🧪 AUTO-TEST",
        color="rgb(100,255,100)",
    )

    if not answer:
        return

    # Lưu test file
    saved_tests = _extract_and_save_code(answer, test_dir)
    if not saved_tests:
        # Fallback: lưu raw answer nếu không extract được
        fname = f"test_{arg.split()[0].lower()}.py"
        # Chỉ lấy code block
        code_blocks = re.findall(r"```python\n(.*?)```", answer, re.DOTALL)
        if code_blocks:
            (test_dir / fname).write_text(code_blocks[0], encoding="utf-8")
            saved_tests = [fname]

    # ── Chạy tests nếu có pytest ──────────────────────────────────────
    test_result = _run_pytest(test_dir)
    wf.test_results = test_result
    wf.tests_passed = test_result.get("passed", False)

    if wf.tests_passed:
        console.print(f"\n[bold green]   ✅ TESTS PASSED — {test_result.get('summary', '')}[/]")
        if Phase.TEST.value not in wf.phases_done:
            wf.phases_done.append(Phase.TEST.value)
        if not auto_triggered:
            console.print("[dim]   → Gate unlocked! Tiếp theo: /review[/]")
    else:
        console.print(f"\n[bold red]   ❌ TESTS FAILED — {test_result.get('summary', '')}[/]")
        if test_result.get("output"):
            console.print(f"[dim]   Output:\n{test_result['output'][:500]}[/]")
        console.print("[dim]   → Fix code rồi chạy lại /build hoặc /test[/]")

    wf.touch()
    _save_state()

def _run_pytest(test_dir: Path) -> Dict:
    """Chạy pytest và trả về kết quả."""
    test_files = list(test_dir.glob("test_*.py"))
    if not test_files:
        return {"passed": False, "summary": "No test files found", "output": ""}

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", str(test_dir), "-v", "--tb=short", "--no-header", "-q"],
            capture_output=True, text=True, timeout=60,
        )
        output  = result.stdout + result.stderr
        passed  = result.returncode == 0
        # Parse summary line
        summary_m = re.search(r"(\d+ passed|\d+ failed|no tests)", output)
        summary   = summary_m.group(0) if summary_m else f"exit code {result.returncode}"
        return {"passed": passed, "summary": summary, "output": output}
    except FileNotFoundError:
        return {"passed": False, "summary": "pytest not installed", "output": ""}
    except subprocess.TimeoutExpired:
        return {"passed": False, "summary": "timeout after 60s", "output": ""}
    except Exception as e:
        return {"passed": False, "summary": str(e), "output": ""}

async def phase_review(arg: str) -> None:
    """Phase 5: 3-agent fanout — code-reviewer + test-engineer + security-auditor."""
    ok, msg = wf.can_enter(Phase.REVIEW)
    if not ok:
        console.print(f"[bold red]{msg}[/]")
        return

    wf.current_phase = Phase.REVIEW
    _print_workflow_status()

    # Collect all source code
    src_dir   = _wf_dir() / "src"
    test_dir  = _wf_dir() / "tests"

    all_code = ""
    for f in sorted(src_dir.glob("*.py")):
        all_code += f"\n\n# === {f.name} ===\n```python\n{f.read_text()}\n```"
    for f in sorted(test_dir.glob("test_*.py")):
        all_code += f"\n\n# === {f.name} (test) ===\n```python\n{f.read_text()}\n```"

    if not all_code.strip():
        all_code = arg  # Fallback: dùng arg trực tiếp

    spec_ctx = f"SPEC:\n{wf.spec_content[:500]}" if wf.spec_content else ""
    code_ctx = f"{spec_ctx}\n\nCODE TO REVIEW:\n{all_code}"

    console.print("\n[bold rgb(255,215,0)]   🔀 3-AGENT REVIEW FANOUT[/]")
    console.print("[dim]   Running: Code Reviewer + Test Engineer + Security Auditor — parallel...[/]\n")

    # ── Async fanout ──────────────────────────────────────────────────
    async def run_reviewer() -> str:
        s = NimoSession(_REVIEW_SYSTEM, task="persona_reviewer")
        return await s.stream(
            f"5-axis code review:\n\n{code_ctx}",
            cot_hint=COT_HINTS["review"],
            label="🔍 CODE REVIEWER", color="rgb(255,200,0)",
        )

    async def run_tester() -> str:
        s = NimoSession(_TESTER_SYSTEM, task="persona_tester")
        return await s.stream(
            f"Analyze test coverage and identify gaps:\n\n{code_ctx}",
            cot_hint=COT_HINTS["test"],
            label="🧪 TEST ENGINEER", color="rgb(100,255,100)",
        )

    async def run_security() -> str:
        s = NimoSession(_SECURITY_SYSTEM, task="persona_security")
        return await s.stream(
            f"Security audit (OWASP Top 10 baseline):\n\n{code_ctx}",
            label="🛡️ SECURITY AUDITOR", color="rgb(255,100,100)",
        )

    r_review, r_test, r_security = await asyncio.gather(
        run_reviewer(), run_tester(), run_security(),
        return_exceptions=True,
    )

    # ── Process verdicts ──────────────────────────────────────────────
    review_text = str(r_review) if not isinstance(r_review, Exception) else f"ERROR: {r_review}"
    
    # Parse APPROVE / REQUEST CHANGES từ review
    verdict = ""
    if re.search(r"\bAPPROVE\b", review_text, re.IGNORECASE):
        verdict = "APPROVE"
        wf.review_approved = True
    elif re.search(r"\bREQUEST\s+CHANGES\b", review_text, re.IGNORECASE):
        verdict = "REQUEST_CHANGES"
        wf.review_approved = False

    # Merge all review output
    full_review = f"""# Review: {wf.project_name}

## Code Review
{review_text}

## Test Coverage Analysis
{str(r_test) if not isinstance(r_test, Exception) else f'ERROR: {r_test}'}

## Security Audit
{str(r_security) if not isinstance(r_security, Exception) else f'ERROR: {r_security}'}

## Verdict: {verdict or 'PENDING'}
"""
    wf.review_verdict = verdict
    path = _save_artifact("review.md", full_review)

    if Phase.REVIEW.value not in wf.phases_done:
        wf.phases_done.append(Phase.REVIEW.value)
    wf.touch()
    _save_state()

    console.print(f"\n[bold green]   ✔ review.md saved → {path}[/]")

    if verdict == "APPROVE":
        console.print("[bold green]   ✅ VERDICT: APPROVE — Gate unlocked![/]")
        console.print("[dim]   → Tiếp theo: /ship <project_name>[/]")
    elif verdict == "REQUEST_CHANGES":
        console.print("[bold red]   ❌ VERDICT: REQUEST CHANGES — Fix issues rồi /build lại[/]")
    else:
        console.print("[bold yellow]   ⚠ Verdict không rõ — kiểm tra review.md[/]")

async def phase_ship(arg: str) -> None:
    """Phase 6: Shipping & launch — pre-launch checklist + rollout plan."""
    ok, msg = wf.can_enter(Phase.SHIP)
    if not ok:
        console.print(f"[bold red]{msg}[/]")
        return

    wf.current_phase = Phase.SHIP
    _print_workflow_status()

    sess = NimoSession(_SHIP_SYSTEM, task="ship")

    # Load review summary
    review_summary = ""
    if wf.review_verdict:
        review_summary = f"Review verdict: {wf.review_verdict}\n"
    
    slices_summary = "\n".join(f"- {s['name']}" for s in wf.build_slices)
    test_summary   = wf.test_results.get("summary", "unknown")

    prompt = f"""Prepare production launch for: "{arg or wf.project_name}"

PROJECT CONTEXT:
- Spec: {wf.project_name}
- Build slices completed: {len(wf.build_slices)}
{slices_summary}
- Tests: {test_summary}
- {review_summary}

Generate:
1. Pre-launch checklist — run through ALL sections: Code Quality, Security, Performance, Accessibility, Infrastructure, Documentation
2. Feature flag strategy (if applicable)
3. Staged rollout plan: 5% → 25% → 50% → 100% với hold criteria per stage
4. Monitoring setup — what metrics to watch
5. Rollback plan — trigger conditions + steps + time-to-rollback estimate

Output as a complete ship.md document.
"""
    answer = await sess.stream(prompt, cot_hint=COT_HINTS["ship"],
                               label="NIMO [SHIP]", color="rgb(255,100,255)")

    if answer:
        path = _save_artifact("ship.md", answer)
        wf.phases_done.append(Phase.SHIP.value)
        wf.current_phase = Phase.DONE
        wf.touch()
        _save_state()
        console.print(f"\n[bold green]   ✔ ship.md saved → {path}[/]")
        console.print("\n[bold rgb(0,255,100)]   🎉 WORKFLOW COMPLETE! Project ready to ship.[/]")
        _print_workflow_status()

# ════════════════════════════════════════════════════════════════════════
# [8] CHAT (ngoài workflow)
# ════════════════════════════════════════════════════════════════════════
_chat_session: Optional[NimoSession] = None

async def chat_turn(user_input: str) -> None:
    global _chat_session
    if _chat_session is None:
        _chat_session = NimoSession(_BASE_SYSTEM, task="chat")
    await _chat_session.stream(user_input, label="NIMO", color="rgb(0,200,255)")

# ════════════════════════════════════════════════════════════════════════
# [9] COMMAND DISPATCHER
# ════════════════════════════════════════════════════════════════════════
def _arg(user_input: str, prefix: str) -> str:
    return user_input[len(prefix):].lstrip(": ").strip()

def _print_help():
    table = Table(show_header=True, header_style="bold rgb(0,200,255)",
                  border_style="dim", box=None)
    table.add_column("Command", style="white", width=30)
    table.add_column("Phase", style="yellow", width=10)
    table.add_column("Description", style="dim")

    rows = [
        ("/spec <yêu cầu>",       "SPEC",   "Viết spec — shared source of truth. HARD GATE mở /plan"),
        ("/plan <task>",          "PLAN",   "Break spec thành ordered tasks + dependency graph [GATE: cần spec]"),
        ("/build <slice>",        "BUILD",  "Implement 1 slice + auto /test ngay sau đó [GATE: cần plan]"),
        ("/test [scope]",         "TEST",   "TDD: RED→GREEN→REFACTOR, chạy pytest [GATE: cần build]"),
        ("/review [note]",        "REVIEW", "3-agent fanout: reviewer+tester+security [GATE: tests phải pass]"),
        ("/ship <name>",          "SHIP",   "Pre-launch checklist + staged rollout + rollback plan [GATE: cần review]"),
        ("─"*28,                  "─"*8,    "─"*40),
        ("/status",               "INFO",   "Workflow progress + artifact locations"),
        ("/artifacts",            "INFO",   "Xem nội dung artifacts đã lưu"),
        ("/reset-workflow",       "INFO",   "Reset workflow state (giữ chat)"),
        ("/pin <note>",           "INFO",   "Ghim context quan trọng"),
        ("/help",                 "INFO",   "Hiển thị help này"),
    ]
    for cmd, phase, desc in rows:
        table.add_row(cmd, phase, desc)

    console.print(Panel(table, title=f"[bold]NIMO v{VERSION} — Engineering Workflow[/]",
                        border_style="rgb(0,150,80)"))

def _print_artifacts():
    wf_dir = _wf_dir()
    artifacts = [
        ("spec.md",    "Specification"),
        ("plan.md",    "Implementation Plan"),
        ("review.md",  "Review Report"),
        ("ship.md",    "Ship Checklist"),
        ("state.json", "Workflow State"),
    ]
    console.print(f"\n[bold]Artifacts in {wf_dir}/[/]")
    for fname, label in artifacts:
        p = wf_dir / fname
        if p.exists():
            size = p.stat().st_size
            console.print(f"  [green]✔[/] {label:<25} [dim]{fname} ({size:,} bytes)[/]")
        else:
            console.print(f"  [dim]✗ {label:<25} {fname} (not yet)[/]")
    
    src_dir  = wf_dir / "src"
    test_dir = wf_dir / "tests"
    src_files  = list(src_dir.glob("*.py"))
    test_files = list(test_dir.glob("*.py"))
    console.print(f"\n  [green]src/[/]   {len(src_files)} files: {', '.join(f.name for f in src_files)}")
    console.print(f"  [green]tests/[/] {len(test_files)} files: {', '.join(f.name for f in test_files)}")

# Pinned context đơn giản
_pins: List[str] = []

async def dispatch(user_input: str) -> bool:
    """Returns False nếu là exit signal."""
    lower = user_input.lower().strip()

    if lower in {"exit", "quit", "/exit", "/quit"}:
        return False

    # ── Workflow commands ──────────────────────────────────────────────
    if lower.startswith("/spec ") or lower.startswith("/spec:"):
        await phase_spec(_arg(user_input, "/spec"))
        return True
    if lower.startswith("/plan ") or lower.startswith("/plan:"):
        await phase_plan(_arg(user_input, "/plan"))
        return True
    if lower.startswith("/build ") or lower.startswith("/build:"):
        await phase_build(_arg(user_input, "/build"))
        return True
    if lower.startswith("/test") and (len(lower) == 5 or lower[5] in " :"):
        await phase_test(_arg(user_input, "/test"))
        return True
    if lower.startswith("/review") and (len(lower) == 7 or lower[7] in " :"):
        await phase_review(_arg(user_input, "/review"))
        return True
    if lower.startswith("/ship ") or lower.startswith("/ship:") or lower == "/ship":
        await phase_ship(_arg(user_input, "/ship") if len(lower) > 5 else "")
        return True

    # ── Info commands ──────────────────────────────────────────────────
    if lower == "/status":
        _print_workflow_status()
        wf_dir = _wf_dir()
        console.print(f"  Project   : {wf.project_name or '(none)'}")
        console.print(f"  Phase     : {wf.current_phase.value}")
        console.print(f"  Done      : {', '.join(wf.phases_done) or 'none'}")
        console.print(f"  Tests     : {'✅ passed' if wf.tests_passed else '❌ not passed'}")
        console.print(f"  Review    : {wf.review_verdict or 'not done'}")
        console.print(f"  Artifacts : {wf_dir}/")
        return True

    if lower == "/artifacts":
        _print_artifacts()
        return True

    if lower == "/reset-workflow":
        global wf
        wf = WorkflowState()
        state_file = _wf_dir() / "state.json"
        if state_file.exists():
            state_file.unlink()
        console.print("[dim]   ✔ Workflow state reset. Artifacts (files) kept.[/]")
        return True

    if lower.startswith("/pin "):
        note = _arg(user_input, "/pin")
        _pins.append(note)
        console.print(f"[green]   ✔ Pinned #{len(_pins)}: {note[:60]}[/]")
        return True

    if lower in {"/help", "help", "/?"}:
        _print_help()
        return True

    # ── Chat thường ────────────────────────────────────────────────────
    await chat_turn(user_input)
    return True

# ════════════════════════════════════════════════════════════════════════
# [10] MAIN
# ════════════════════════════════════════════════════════════════════════
async def main():
    # Load existing state nếu có
    _load_state()

    console.print(Panel(
        f"[bold rgb(0,255,100)]NIMO v{VERSION} — ENGINEERING WORKFLOW[/]\n\n"
        f"[white]Workflow: /spec → /plan → /build → /test → /review → /ship[/]\n"
        f"[dim]Hard gates enforced. File persistence: ./.nimo/[/]\n"
        f"[dim]Model: {PRIMARY_MODEL}[/]\n\n"
        f"[dim]Gõ /help để xem tất cả commands[/]",
        border_style="rgb(0,150,80)",
        title="[bold]🤖 NIMO BOOT[/]",
    ))

    if wf.project_name:
        console.print(f"[dim]   ↳ Resuming project: {wf.project_name}[/]")
        _print_workflow_status()

    msg_count = 0
    while True:
        try:
            msg_count += 1
            phase_indicator = f"[{wf.current_phase.value}]" if wf.current_phase != Phase.IDLE else ""
            prompt_str = f"[bold orange3]NIMO {phase_indicator} ›[/] "
            user_input = await asyncio.to_thread(console.input, prompt_str)
            user_input = user_input.strip()

            if not user_input:
                msg_count -= 1
                continue

            result = await dispatch(user_input)
            if not result:
                break

        except KeyboardInterrupt:
            break
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")
            _log.exception("Main loop error")

    console.print("[dim]✔ NIMO shutdown.[/]")

if __name__ == "__main__":
    asyncio.run(main())