# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.vergeio.vergeos.plugins.filter.vergeos_fill_answers import (
    FilterModule as FillAnswersFilterModule,
)
from ansible_collections.vergeio.vergeos.plugins.filter.vergeos_not_applicable import (
    FilterModule as NotApplicableFilterModule,
)
from ansible_collections.vergeio.vergeos.plugins.filter.vergeos_tier_step_unanswered import (
    FilterModule as TierStepFilterModule,
)
from ansible_collections.vergeio.vergeos.plugins.module_utils.recipe_answers import (
    drive_tier_has_no_question,
    fill_recipe_answers,
    recipe_not_applicable,
)


def test_fill_answers_filter():
    filters = FillAnswersFilterModule().filters()
    assert "vergeos_fill_answers" in filters
    questions = [{
        "name": "YB_RAM", "type": "ram", "required": False,
        "default": "1024", "min": 16384,
    }]
    direct = fill_recipe_answers(questions, {"YB_RAM": 1024})
    via_filter = filters["vergeos_fill_answers"]({"YB_RAM": 1024}, questions)
    assert via_filter == direct
    assert via_filter["plain"]["YB_RAM"] == 16384
    assert via_filter["secrets"] == {}


def test_not_applicable_filter():
    filters = NotApplicableFilterModule().filters()
    assert filters["vergeos_not_applicable"] is recipe_not_applicable
    assert filters["vergeos_not_applicable"]({"downloaded": False}) is True
    assert filters["vergeos_not_applicable"]({"downloaded": True}) is False


def test_tier_step_unanswered_filter():
    filters = TierStepFilterModule().filters()
    assert "vergeos_tier_step_unanswered" in filters
    questions = [{"name": "YB_RAM", "type": "ram"}]
    msg = "preferred_tier is required but was ''"
    via_filter = filters["vergeos_tier_step_unanswered"](msg, questions)
    assert via_filter == drive_tier_has_no_question(questions, msg)
