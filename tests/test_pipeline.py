from __future__ import annotations

import pytest

from byoai.errors import ConfigurationError
from byoai.pipeline import FunctionStage, Pipeline


async def _noop(ctx):
    pass


def test_remove_by_type_alone_removes_every_matching_stage():
    # Documented behavior, not a footgun by itself: remove(FunctionStage)
    # removes every bare-function stage, since add() wraps them all in the
    # same FunctionStage type — see the name= tests below for how to target
    # just one.
    pipeline = Pipeline()
    pipeline.add(_noop).add(_noop)
    pipeline.remove(FunctionStage)
    assert pipeline.stages == []


def test_remove_by_name_targets_one_function_stage_without_touching_others():
    async def keep_me(ctx):
        pass

    pipeline = Pipeline()
    pipeline.add(_noop).add(keep_me)
    pipeline.remove(FunctionStage, name="_noop")
    assert [getattr(s, "name", None) for s in pipeline.stages] == ["keep_me"]


def test_replace_by_name_targets_one_function_stage():
    async def keep_me(ctx):
        pass

    replacement = FunctionStage(_noop, name="replacement")
    pipeline = Pipeline()
    pipeline.add(_noop).add(keep_me)
    pipeline.replace(FunctionStage, replacement, name="_noop")
    assert [getattr(s, "name", None) for s in pipeline.stages] == ["replacement", "keep_me"]


def test_remove_with_neither_stage_type_nor_name_raises():
    pipeline = Pipeline()
    pipeline.add(_noop)
    with pytest.raises(ConfigurationError):
        pipeline.remove()


def test_replace_without_replacement_raises():
    pipeline = Pipeline()
    pipeline.add(_noop)
    with pytest.raises(ConfigurationError):
        pipeline.replace(FunctionStage, name="_noop")
