"""Claude API 호출을 한곳에 모은 얇은 래퍼.

- 모든 호출은 구조화 출력(Pydantic 모델)으로 받아서, 파싱 실패 없이 그래프 상태에 넣는다.
- 호출마다 토큰 사용량을 모아 두었다가 로그(ops.agent_runs)에 남긴다.
- 설정은 .env 로 바꾼다:
    ANTHROPIC_API_KEY   (필수)
    AGENT_MODEL         SQL 생성·답변용 모델 (기본 claude-opus-5-5)
    AGENT_FAST_MODEL    질문 분류용 모델 (기본 AGENT_MODEL 과 같음. 비용을 줄이려면 claude-haiku-5-5)
    AGENT_FALLBACKS     on/off. 안전 분류기가 거절하면 서버가 다른 모델로 다시 시도 (기본 on)
"""

import os
from dataclasses import dataclass, field
from typing import TypeVar

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from kbo_ai_platform.db.connection import PROJECT_ROOT

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """모델이 답변을 거절함."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    models: set[str] = field(default_factory=set)

    def add(self, model: str, usage) -> None:
        self.calls += 1
        self.models.add(model)
        self.input_tokens += (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.output_tokens += usage.output_tokens or 0


class ClaudeLLM:
    def __init__(self, client: anthropic.Anthropic | None = None):
        load_dotenv(PROJECT_ROOT / ".env")
        self.client = client or anthropic.Anthropic()
        self.model = os.getenv("AGENT_MODEL", DEFAULT_MODEL)
        self.fast_model = os.getenv("AGENT_FAST_MODEL", self.model)
        self.fallbacks = os.getenv("AGENT_FALLBACKS", "on").lower() not in ("off", "0", "false")

    def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        usage: Usage,
        *,
        fast: bool = False,
        effort: str = "medium",
        max_tokens: int = 8000,
    ) -> T:
        model = self.fast_model if fast else self.model
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            # 시스템 프롬프트(스키마 설명서)는 매번 같아서 캐시하면 두 번째 질문부터 입력 비용이 크게 준다.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if self.fallbacks and model.startswith(("claude-opus-5", "claude-fable", "claude-sonnet-5-5")):
            response = self.client.beta.messages.parse(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = self.client.messages.parse(**kwargs)
        usage.add(response.model, response.usage)
        if response.stop_reason == "refusal":
            raise LLMRefusal("모델이 이 질문에 답하지 않기로 했습니다.")
        if response.parsed_output is None:
            raise RuntimeError(f"구조화 출력을 받지 못했습니다 (stop_reason={response.stop_reason}).")
        return response.parsed_output
