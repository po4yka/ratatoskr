"""Response models must accept the same aliases they serialize to the wire."""

from __future__ import annotations

from typing import Any, get_args

from pydantic import AliasChoices, BaseModel

from app.api.main import app


def _iter_response_models(
    annotation: Any,
    seen: set[type[BaseModel]],
):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        yield annotation
        for field in annotation.model_fields.values():
            yield from _iter_response_models(field.annotation, seen)
        return

    for argument in get_args(annotation):
        yield from _iter_response_models(argument, seen)


def _accepts_alias(validation_alias: Any, serialization_alias: str) -> bool:
    if validation_alias == serialization_alias:
        return True
    return isinstance(validation_alias, AliasChoices) and serialization_alias in (
        validation_alias.choices
    )


def test_response_models_accept_their_serialized_aliases() -> None:
    """Prevent FastAPI from rejecting payloads already serialized by success_response."""
    seen: set[type[BaseModel]] = set()
    failures: list[str] = []

    for route in app.routes:
        effective_routes = (
            route.effective_route_contexts()
            if hasattr(route, "effective_route_contexts")
            else (route,)
        )
        for effective_route in effective_routes:
            for model in _iter_response_models(
                getattr(effective_route, "response_model", None),
                seen,
            ):
                for name, field in model.model_fields.items():
                    alias = field.serialization_alias
                    if (
                        alias
                        and alias != name
                        and not _accepts_alias(field.validation_alias, alias)
                    ):
                        failures.append(f"{model.__module__}.{model.__name__}.{name} -> {alias}")

    assert not failures, "Response aliases are not validation-compatible:\n" + "\n".join(failures)
