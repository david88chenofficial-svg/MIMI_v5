import json
from dataclasses import dataclass, field
from pathlib import Path

from agents import ModelSettings
from openai.types.shared import Reasoning

from MIMI_agents import (
    Coder_agent,
    Coder_secretary,
    ConversationSupervisor_agent,
    Planner_agent,
    TaskBreaker_agent,
    Verifier_agent,
)


MODEL_CAPABILITIES_PATH = Path(__file__).resolve().with_name("MIMI_model_capabilities.json")
MODEL_CAPABILITIES_DATA = json.loads(MODEL_CAPABILITIES_PATH.read_text(encoding="utf-8"))
MODEL_CATALOG_METADATA = {
    key: value
    for key, value in MODEL_CAPABILITIES_DATA.items()
    if key != "models"
}
MODEL_CATALOG = {
    model["id"]: {
        key: value
        for key, value in model.items()
        if key != "id"
    }
    for model in MODEL_CAPABILITIES_DATA["models"]
}


AGENT_MODEL_TARGETS = {
    "planner": Planner_agent,
    "task_breaker": TaskBreaker_agent,
    "coder": Coder_agent,
    "verifier": Verifier_agent,
    "supervisor": ConversationSupervisor_agent,
    "coder_secretary": Coder_secretary,
}


AGENT_CAPABILITY_REQUIREMENTS = {
    "planner": {
        "capabilities": ["responses", "streaming", "image_input", "web_search"],
        "tools": ["web_search"],
    },
    "task_breaker": {
        "capabilities": ["responses", "structured_output", "function_calling"],
        "tools": ["function_calling"],
    },
    "coder": {
        "capabilities": ["responses", "streaming", "image_input"],
        "tools": [],
    },
    "verifier": {
        "capabilities": ["responses", "streaming", "image_input", "web_search"],
        "tools": ["web_search"],
    },
    "supervisor": {
        "capabilities": ["responses"],
        "tools": [],
    },
    "coder_secretary": {
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
    return [
        capability.replace("_", " ")
        for capability in requirements.get("capabilities", [])
        if not capabilities.get(capability, False)
    ]


def model_supports_agent(model: str, agent_key: str) -> bool:
    return not model_incompatibilities(model, agent_key)


def model_options_for_agent(agent_key: str) -> list[str]:
    return [
        model
        for model in MODEL_CATALOG
        if model_supports_agent(model, agent_key)
    ]


def setting_options_for_agent(model: str, agent_key: str) -> dict[str, list[str]]:
    spec = MODEL_CATALOG.get(model, {})
    requirements = AGENT_CAPABILITY_REQUIREMENTS.get(agent_key, {})
    blocked_efforts = set()
    for tool_name in requirements.get("tools", []):
        tool_constraint = spec.get("tool_constraints", {}).get(tool_name, {})
        blocked_efforts.update(tool_constraint.get("blocked_reasoning_efforts", []))

    return {
        "reasoning_effort": ["default"] + [
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
    options = setting_options_for_agent(model, agent_key)
    defaults = {}
    for setting_name, values in options.items():
        selected = lowest_setting(values, setting_name)
        if selected is not None:
            defaults[setting_name] = selected
    return defaults


OPENAI_MODEL_OPTIONS = list(MODEL_CATALOG)


@dataclass
class AgentModelConfig:
    models: dict[str, str] = field(default_factory=dict)
    settings: dict[str, dict[str, str]] = field(default_factory=dict)

    def validate(self) -> None:
        errors = []

        for agent_key, model in self.models.items():
            if not model:
                continue
            if agent_key not in AGENT_MODEL_TARGETS:
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
            if agent_key not in AGENT_MODEL_TARGETS:
                errors.append(f"Unknown agent settings: {agent_key}.")
                continue
            if not isinstance(settings, dict):
                errors.append(f"Settings for {agent_key} must be an object.")
                continue

            model = self.models.get(agent_key) or AGENT_MODEL_TARGETS[agent_key].model
            if model not in MODEL_CATALOG:
                errors.append(f"Cannot validate settings for unknown model: {model}.")
                continue
            if model_incompatibilities(model, agent_key):
                continue

            allowed_settings = setting_options_for_agent(model, agent_key)
            for setting_name, value in settings.items():
                if setting_name not in allowed_settings:
                    errors.append(f"Unknown setting for {agent_key}: {setting_name}.")
                    continue
                if value == "default":
                    continue
                if value not in allowed_settings[setting_name]:
                    allowed = ", ".join(allowed_settings[setting_name])
                    errors.append(
                        f"{model} does not allow {setting_name}={value} for "
                        f"{agent_key}. Choose: {allowed}."
                    )

        if errors:
            raise ValueError(" ".join(errors))

    def normalized(self) -> dict[str, str]:
        return {
            key: model
            for key, model in self.models.items()
            if model and key in AGENT_MODEL_TARGETS and model_supports_agent(model, key)
        }

    def normalized_settings(self) -> dict[str, dict[str, str]]:
        selected_models = self.normalized()
        normalized = {}
        for key, settings in self.settings.items():
            if key not in AGENT_MODEL_TARGETS or not isinstance(settings, dict):
                continue
            model = selected_models.get(key) or AGENT_MODEL_TARGETS[key].model
            allowed_settings = setting_options_for_agent(model, key)
            agent_settings = {
                setting_name: value
                for setting_name, value in settings.items()
                if value != "default"
                and setting_name in allowed_settings
                and value in allowed_settings[setting_name]
            }
            if agent_settings:
                normalized[key] = agent_settings
        return normalized


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
    selected_settings = config.normalized_settings()
    original_models = {}
    for key, agent in AGENT_MODEL_TARGETS.items():
        original_models[key] = {
            "model": agent.model,
            "model_settings": agent.model_settings,
        }
        if key in selected_models:
            selected_model = selected_models[key]
            model_changed = selected_model != agent.model
            agent.model = selected_model
            if model_changed:
                agent.model_settings = model_settings_with_overrides(
                    agent.model_settings,
                    default_settings_for_agent(agent.model, key),
                )
        if key in selected_settings:
            agent.model_settings = model_settings_with_overrides(
                agent.model_settings,
                selected_settings[key],
            )
    return original_models


def restore_agent_models(original_models: dict[str, dict]) -> None:
    for key, original in original_models.items():
        agent = AGENT_MODEL_TARGETS.get(key)
        if agent is not None:
            agent.model = original["model"]
            agent.model_settings = original["model_settings"]
