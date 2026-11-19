# -*- coding: utf-8 -*-

# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
name: vergeos_fill_answers
short_description: Fill recipe answers from question metadata
description:
  - Build the plain and secret answer dicts for one recipe.
  - Credentials are returned under C(secrets) and must be passed through
    the no_log C(answers) parameter. They are not for display.
options:
  _input:
    description: Answers already chosen, keyed by question name.
    type: dict
    required: true
  questions:
    description: The recipe's question rows.
    type: list
    elements: dict
    required: true
  options:
    description: Valid values for table-backed questions, keyed by question name.
    type: dict
    required: false
'''

EXAMPLES = r'''
- name: Fill what this recipe still needs
  ansible.builtin.debug:
    msg: "{{ (base | vergeio.vergeos.vergeos_fill_answers(questions, options)).plain }}"
'''

RETURN = r'''
_value:
  description:
    - Mapping with C(plain) and C(secrets).
  type: dict
'''

from ansible_collections.vergeio.vergeos.plugins.module_utils.recipe_answers import (
    fill_recipe_answers,
    recipe_not_applicable,
)


def _fill(answers, questions, options=None):
    return fill_recipe_answers(questions, answers, options)


class FilterModule(object):
    """Recipe-matrix helpers."""

    def filters(self):
        return {
            'vergeos_fill_answers': _fill,
            'vergeos_not_applicable': recipe_not_applicable,
        }
