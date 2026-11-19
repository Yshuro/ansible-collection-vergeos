# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible_collections.vergeio.vergeos.plugins.filter.recipe_matrix import (
    FilterModule,
)
from ansible_collections.vergeio.vergeos.plugins.module_utils.recipe_answers import (
    fill_recipe_answers,
    recipe_not_applicable,
)


def test_filters_are_the_module_utils_functions():
    filters = FilterModule().filters()
    assert filters["vergeos_not_applicable"] is recipe_not_applicable
    questions = [{
        "name": "YB_RAM", "type": "ram", "required": False,
        "default": "1024", "min": 16384,
    }]
    direct = fill_recipe_answers(questions, {"YB_RAM": 1024})
    via_filter = filters["vergeos_fill_answers"]({"YB_RAM": 1024}, questions)
    assert via_filter == direct
    assert via_filter["plain"]["YB_RAM"] == 16384
    assert via_filter["secrets"] == {}
