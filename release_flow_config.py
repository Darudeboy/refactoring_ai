import json
import os
from copy import deepcopy
from typing import Any, Dict, List


def _split_csv(value: str, fallback: List[str]) -> List[str]:
    raw = (value or "").strip()
    if not raw:
        return fallback
    return [part.strip() for part in raw.split(",") if part.strip()]


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _default_profile() -> Dict[str, Any]:
    return {
        "name": "default",
        "workflow_order": [
            "Формирование",
            "Готов к стабилизации",
            "Стабилизация",
            "Готов к ПСИ",
            "ПСИ",
            "Согласование ППСИ",
            "Утверждение ППСИ",
        ],
        "transition_ids": {
            "Готов к стабилизации": "15903",
            "Стабилизация": "15904",
            "Готов к ПСИ": "15307",
            "ПСИ": "10105",
            "Согласование ППСИ": "16311",
            "Утверждение ППСИ": "16312",
        },
        "done_statuses": [
            "Done",
            "Closed",
            "Resolved",
            "Выполнено",
            "Закрыто",
        ],
        "story_rules": {
            "bt_keywords": ["бизнес-треб", "business requirement", "бт", "fr", "functional requirement"],
            "arch_keywords": ["архитект", "architecture", "architectural"],
        },
        "bug_rules": {
            "ct_ift_keywords": ["ct", "ift", "ифт"],
            "ct_ift_allowed_statuses": ["Закрыт", "Closed"],
            "prom_keywords": ["пром", "prom"],
            "prom_expected_statuses": ["Выполнен", "Resolved"],
        },
        "testing_tab": {
            "ift_recommendation_fields": _split_csv(
                os.getenv("RELEASE_FLOW_IFT_RECOMMENDATION_FIELDS", ""),
                ["customfield_ift_recommendation", "customfield_recommendation"],
            ),
            "ift_display_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_IFT_DISPLAY_KEYWORDS", ""),
                ["рекомендация по отчету ифт", "recommendation ift", "ифт"],
            ),
            "ift_approved_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_IFT_APPROVED_KEYWORDS", ""),
                ["рекомендован", "recommended"],
            ),
            "nt_display_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_NT_DISPLAY_KEYWORDS", ""),
                ["рекомендация нт", "нагрузоч", "performance recommendation"],
            ),
            "nt_approved_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_NT_APPROVED_KEYWORDS", ""),
                ["не требуется", "рекомендован", "версия 2 рекомендован", "not required", "recommended"],
            ),
            "dt_display_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_DT_DISPLAY_KEYWORDS", ""),
                ["рекомендация дт", "dt recommendation"],
            ),
            "dt_approved_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_DT_APPROVED_KEYWORDS", ""),
                ["рекомендован", "recommended"],
            ),
            "green_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_RECOMMENDED_GREEN_KEYWORDS", ""),
                ["green", "success", "status-lozenge-success", "aui-lozenge-success"],
            ),
        },
        "distribution_tab": {
            "link_fields": _split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_LINK_FIELDS", ""),
                ["customfield_distribution_link", "customfield_distrib_link"],
            ),
            "link_display_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_LINK_DISPLAY_KEYWORDS", ""),
                ["ссылка на дистрибутив", "distribution link", "дистрибутив ссылка"],
            ),
            "registered_fields": _split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_REGISTERED_FIELDS", ""),
                ["customfield_distribution_registered", "customfield_distrib_registered"],
            ),
            "registered_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_REGISTERED_KEYWORDS", ""),
                ["зарегистрирован", "registered", "yes", "true"],
            ),
            "ke_keywords": _split_csv(
                os.getenv("RELEASE_FLOW_DISTRIBUTION_KE_KEYWORDS", ""),
                ["кэ дистрибутива", "ке дистрибутива", "distribution ke", "ke distribution"],
            ),
        },
        "manual_checks": [
            {
                "id": "decommission_distribution",
                "title": "Проверка выводимых из эксплуатации дистрибутивов",
            },
            {
                "id": "load_test_subtask",
                "title": "Подзадача нагрузочного тестирования",
                "keywords": ["нагрузоч", "load test", "performance test"],
                "required_statuses": ["Закрыто", "Закрыт", "Closed"],
                "required": True,
            },
            {
                "id": "author_supervision_subtask",
                "title": "Подзадача авторского надзора",
                "keywords": ["авторск", "author supervision"],
                "required_statuses": ["Закрыто", "Закрыт", "Closed"],
                "required": True,
            },
            {
                "id": "translations_subtask",
                "title": "Подзадача проверки переводов",
                "keywords": ["перевод", "translation"],
                "required_statuses": ["Закрыто", "Закрыт", "Closed"],
                "required": True,
            },
        ],
    }


def _hotfix_profile(base: Dict[str, Any]) -> Dict[str, Any]:
    profile = deepcopy(base)
    profile["name"] = "hotfix"
    profile["workflow_order"] = _split_csv(
        os.getenv("RELEASE_FLOW_HOTFIX_WORKFLOW_ORDER", ""),
        profile["workflow_order"],
    )
    return profile


def load_release_flow_profiles() -> Dict[str, Dict[str, Any]]:
    base = _default_profile()
    profiles = {
        "default": base,
        "hotfix": _hotfix_profile(base),
    }

    overrides_raw = os.getenv("RELEASE_FLOW_PROFILE_OVERRIDES", "").strip()
    if overrides_raw:
        try:
            overrides = json.loads(overrides_raw)
            if isinstance(overrides, dict):
                for profile_name, payload in overrides.items():
                    if not isinstance(payload, dict):
                        continue
                    if profile_name in profiles:
                        profiles[profile_name] = _merge_dict(profiles[profile_name], payload)
                    else:
                        profiles[profile_name] = _merge_dict(base, payload)
                        profiles[profile_name]["name"] = profile_name
        except Exception:
            # Не валим запуск UI, если в env лежит битый JSON.
            pass

    return profiles


def resolve_profile_name(project_key: str = "", requested_profile: str = "auto") -> str:
    requested = (requested_profile or "auto").strip().lower()
    if requested and requested != "auto":
        return requested

    hotfix_projects = {
        item.upper()
        for item in _split_csv(os.getenv("RELEASE_FLOW_HOTFIX_PROJECTS", ""), ["HOTFIX", "HF"])
    }
    if (project_key or "").strip().upper() in hotfix_projects:
        return "hotfix"
    return "default"


def get_release_flow_profile(project_key: str = "", requested_profile: str = "auto") -> Dict[str, Any]:
    profiles = load_release_flow_profiles()
    resolved = resolve_profile_name(project_key=project_key, requested_profile=requested_profile)
    profile = profiles.get(resolved) or profiles.get("default") or _default_profile()
    profile = deepcopy(profile)
    profile["name"] = resolved if resolved in profiles else profile.get("name", "default")
    return profile
