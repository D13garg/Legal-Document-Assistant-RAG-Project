"""What one deployment fixes about grounded generation (ADR-029): model, template, bounds."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lda.modules.ai_gateway.api import GenerationParameters
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.domain.answer_contract import ContractLimits
from lda.shared.kernel.errors import ValidationError

MODEL_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
TEMPLATE_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")
CONFIG_ID = re.compile(r"rag-[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class GroundingPolicy:
    model: str
    template_id: str
    template_version: int
    parameters: GenerationParameters
    contract: ContractLimits
    max_sources: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or MODEL_LABEL.fullmatch(self.model) is None
            or not isinstance(self.template_id, str)
            or TEMPLATE_LABEL.fullmatch(self.template_id) is None
            or type(self.template_version) is not int
            or not 1 <= self.template_version <= 1_000_000
            or not isinstance(self.parameters, GenerationParameters)
            or not isinstance(self.contract, ContractLimits)
            or type(self.max_sources) is not int
            or self.max_sources < 1
        ):
            raise ValidationError(codes.INVALID_INPUT, detail="policy")

    @property
    def max_markers(self) -> int:
        return self.contract.max_statements
