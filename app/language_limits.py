"""Language limits: explicit value, disabled correction, then automatic policy."""
TIME = {"java8": (2, 1000), "python313": (3, 2000), "pypy3": (3, 2000), "kotlin": (2, 1000)}
MEMORY = {"java8": (2, 16), "python313": (2, 32), "pypy3": (2, 128), "kotlin": (2, 16)}
EDITOR_LANGUAGES = {"python3": "python313"}


def effective_limits(time_ms, memory_mb, language, limits=None):
    language = EDITOR_LANGUAGES.get(language, language)
    limits = limits or {}
    disabled = limits.get("disable_auto_adjustment") is True
    tm, tb = (1, 0) if disabled else TIME.get(language, (1, 0))
    mm, mb = (1, 0) if disabled else MEMORY.get(language, (1, 0))
    return {"time_limit_ms": limits.get("time_limit_ms") if limits.get("time_limit_ms") is not None else time_ms * tm + tb,
            "memory_limit_mb": limits.get("memory_limit_mb") if limits.get("memory_limit_mb") is not None else memory_mb * mm + mb}


def exported_limits(limits):
    return {EDITOR_LANGUAGES.get(language, language): dict(value) for language, value in (limits or {}).items()}


def agent_problem(problem):
    # Existing agents already honor explicit values. Resolve the new checkbox
    # before dispatch so mixed-version fleets cannot silently re-enable it.
    limits = {}
    for language, value in (problem.get("language_resource_limits") or {}).items():
        value = value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else dict(value)
        if value.get("disable_auto_adjustment") is True:
            value = effective_limits(problem["time_limit_ms"], problem["memory_limit_mb"], language, value)
        else:
            value.pop("disable_auto_adjustment", None)
        limits[language] = value
    return {**problem, "language_resource_limits": limits}
