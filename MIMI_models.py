"""Model selection shared by the dashboard and the runtime."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from agents import ModelSettings
from openai.types.shared import Reasoning

from MIMI_agents import Documentation_agent, Planner_agent, TaskBreaker_agent, Verifier_agent


MODEL_CAPABILITIES_PATH = Path(__file__).resolve().with_name("MIMI_model_capabilities.json")
MODEL_CAPABILITIES_DATA = json.loads(MODEL_CAPABILITIES_PATH.read_text(encoding="utf-8"))
MODEL_CATALOG_METADATA = {
    key: value for key, value in MODEL_CAPABILITIES_DATA.items() if key != "models"
}
MODEL_CATALOG = {
    model["id"]: {key: value for key, value in model.items() if key != "id"}
    for model in MODEL_CAPABILITIES_DATA["models"]
}


# The coding model is configured here but invoked through the Codex SDK, so it is
# not an Agents SDK target.
AGENT_MODEL_TARGETS = {
    "planner": Planner_agent,
    "task_breaker": TaskBreaker_agent,
    "verifier": Verifier_agent,
    "documentation": Documentation_agent,
}
MODEL_DEFAULTS = {
    **{key: agent.model for key, agent in AGENT_MODEL_TARGETS.items()},
    "coder": "gpt-5.6-luna",
}

AGENT_CAPABILITY_REQUIREMENTS = {
    "planner": {
        "runtime": "responses",
        "capabilities": ["responses", "streaming", "image_input"],
        "tools": [],
    },
    "task_breaker": {
        "runtime": "responses",
        "capabilities": ["responses", "structured_output"],
        "tools": [],
    },
    "coder": {
        "runtime": "codex",
        "capabilities": ["responses", "streaming", "image_input"],
        "tools": [],
    },
    "verifier": {
        "runtime": "responses",
        "capabilities": [
            "responses",
            "streaming",
            "image_input",
            "structured_output",
        ],
        "tools": [],
    },
    "documentation": {
        "runtime": "responses",
        "capabilities": ["responses", "structured_output"],
        "tools": [],
    },
}

SETTING_PREFERENCE = {
    "reasoning_effort": ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
    "verbosity": ["default", "low", "medium", "high"],
}


def model_incompatibilities(model: str, agent_key: str) -> list[str]:
    spec = MODEL_CATALOG.get(model)
    requirements = AGENT_CAPABILITY_REQUIREMENTS.get(agent_key)
    if spec is None:
        return ["unknown model"]
    if requirements is None:
        return ["unknown agent"]
    capabilities = spec.get("capabilities", {})
    incompatibilities = [
        capability.replace("_", " ")
        for capability in requirements.get("capabilities", [])
        if not capabilities.get(capability, False)
    ]
    required_runtime = requirements.get("runtime")
    if required_runtime and required_runtime not in spec.get("runtimes", []):
        incompatibilities.append(f"{required_runtime} runtime")
    return incompatibilities


def model_supports_agent(model: str, agent_key: str) -> bool:
    return not model_incompatibilities(model, agent_key)


def model_options_for_agent(agent_key: str) -> list[str]:
    return [model for model in MODEL_CATALOG if model_supports_agent(model, agent_key)]


def setting_options_for_agent(model: str, agent_key: str) -> dict[str, list[str]]:
    spec = MODEL_CATALOG.get(model, {})
    requirements = AGENT_CAPABILITY_REQUIREMENTS.get(agent_key, {})
    blocked_efforts: set[str] = set()
    for tool_name in requirements.get("tools", []):
        constraints = spec.get("tool_constraints", {}).get(tool_name, {})
        blocked_efforts.update(constraints.get("blocked_reasoning_efforts", []))
    return {
        "reasoning_effort": ["default"]
        + [
            effort
            for effort in spec.get("reasoning_efforts", [])
            if effort not in blocked_efforts
        ],
        "verbosity": ["default"] + list(spec.get("verbosity", [])),
    }


def lowest_setting(options: list[str], setting_name: str) -> str | None:
    for value in SETTING_PREFERENCE[setting_name]:
        if value in options:
            return value
    return options[0] if options else None


def default_settings_for_agent(model: str, agent_key: str) -> dict[str, str]:
    return {
        setting_name: selected
        for setting_name, values in setting_options_for_agent(model, agent_key).items()
        if (selected := lowest_setting(values, setting_name)) is not None
    }


OPENAI_MODEL_OPTIONS = list(MODEL_CATALOG)


@dataclass
class AgentModelConfig:
    models: dict[str, str] = field(default_factory=dict)
    settings: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Smooth migration for saved dashboard payloads from MIMI v5's old names.
        if "documentation" not in self.models and self.models.get("coder_secretary"):
            self.models["documentation"] = self.models["coder_secretary"]
        if "documentation" not in self.settings and self.settings.get("coder_secretary"):
            self.settings["documentation"] = self.settings["coder_secretary"]
        for legacy_key in ("coder_secretary", "supervisor"):
            self.models.pop(legacy_key, None)
            self.settings.pop(legacy_key, None)

    def validate(self) -> None:
        errors: list[str] = []
        for agent_key, model in self.models.items():
            if not model:
                continue
            if agent_key not in AGENT_CAPABILITY_REQUIREMENTS:
                errors.append(f"Unknown agent: {agent_key}.")
                continue
            if model not in MODEL_CATALOG:
                errors.append(f"Unknown model for {agent_key}: {model}.")
                continue
            missing = model_incompatibilities(model, agent_key)
            if missing:
                errors.append(
                    f"{model} cannot be used by {agent_key}; missing: {', '.join(missing)}."
                )

        for agent_key, settings in self.settings.items():
            if agent_key not in AGENT_CAPABILITY_REQUIREMENTS:
                errors.append(f"Unknown agent settings: {agent_key}.")
                continue
            if not isinstance(settings, dict):
                errors.append(f"Settings for {agent_key} must be an object.")
                continue
            model = self.models.get(agent_key) or MODEL_DEFAULTS[agent_key]
            if model not in MODEL_CATALOG:
                errors.append(f"Cannot validate settings for unknown model: {model}.")
                continue
            if model_incompatibilities(model, agent_key):
                continue
            allowed = setting_options_for_agent(model, agent_key)
            for setting_name, value in settings.items():
                if setting_name not in allowed:
                    errors.append(f"Unknown setting for {agent_key}: {setting_name}.")
                elif value != "default" and value not in allowed[setting_name]:
                    errors.append(
                        f"{model} does not allow {setting_name}={value} for {agent_key}. "
                        f"Choose: {', '.join(allowed[setting_name])}."
                    )
        if errors:
            raise ValueError(" ".join(errors))

    def normalized(self) -> dict[str, str]:
        return {
            key: model
            for key, model in self.models.items()
            if model
            and key in AGENT_CAPABILITY_REQUIREMENTS
            and model_supports_agent(model, key)
        }

    def normalized_settings(self) -> dict[str, dict[str, str]]:
        selected_models = self.normalized()
        normalized: dict[str, dict[str, str]] = {}
        for key, settings in self.settings.items():
            if key not in AGENT_CAPABILITY_REQUIREMENTS or not isinstance(settings, dict):
                continue
            model = selected_models.get(key) or MODEL_DEFAULTS[key]
            allowed = setting_options_for_agent(model, key)
            values = {
                name: value
                for name, value in settings.items()
                if value != "default" and name in allowed and value in allowed[name]
            }
            if values:
                normalized[key] = values
        return normalized


def selected_model(config: AgentModelConfig | None, agent_key: str) -> str:
    config = config or AgentModelConfig()
    return config.normalized().get(agent_key) or MODEL_DEFAULTS[agent_key]


def selected_settings(config: AgentModelConfig | None, agent_key: str) -> dict[str, str]:
    config = config or AgentModelConfig()
    return config.normalized_settings().get(agent_key, {})


def model_settings_with_overrides(base_settings, overrides: dict[str, str]) -> ModelSettings:
    kwargs = {
        field_name: getattr(base_settings, field_name, None)
        for field_name in ModelSettings.__dataclass_fields__
    }
    if "reasoning_effort" in overrides:
        effort = overrides["reasoning_effort"]
        kwargs["reasoning"] = None if effort == "default" else Reasoning(effort=effort)
    if "verbosity" in overrides:
        verbosity = overrides["verbosity"]
        kwargs["verbosity"] = None if verbosity == "default" else verbosity
    return ModelSettings(**kwargs)


def apply_agent_models(config: AgentModelConfig | None) -> dict[str, dict]:
    config = config or AgentModelConfig()
    config.validate()
    selected_models = config.normalized()
    selected_overrides = config.normalized_settings()
    originals: dict[str, dict] = {}
    for key, agent in AGENT_MODEL_TARGETS.items():
        originals[key] = {"model": agent.model, "model_settings": agent.model_settings}
        if key in selected_models:
            changed = selected_models[key] != agent.model
            agent.model = selected_models[key]
            if changed:
                agent.model_settings = model_settings_with_overrides(
                    agent.model_settings, default_settings_for_agent(agent.model, key)
                )
        if key in selected_overrides:
            agent.model_settings = model_settings_with_overrides(
                agent.model_settings, selected_overrides[key]
            )
    return originals


def restore_agent_models(originals: dict[str, dict]) -> None:
    for key, original in originals.items():
        agent = AGENT_MODEL_TARGETS.get(key)
        if agent is not None:
            agent.model = original["model"]
            agent.model_settings = original["model_settings"]
