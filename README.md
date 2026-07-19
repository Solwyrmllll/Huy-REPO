# Huy-REPO
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