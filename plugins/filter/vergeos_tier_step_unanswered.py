# -*- coding: utf-8 -*-

# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
name: vergeos_tier_step_unanswered
short_description: Determine whether a drive tier step has no corresponding question
version_added: "2.3.0"
description:
  - Check whether a drive creation step failed because C(preferred_tier) was
    empty and the recipe defines no storage tier question to supply it.
options:
  _input:
    description: The failure message or list of messages returned by the module.
    type: raw
    required: true
  questions:
    description: The recipe's question rows.
    type: list
    elements: dict
    required: true
'''

EXAMPLES = r'''
- name: Check if failure was caused by a missing drive tier question
  ansible.builtin.debug:
    msg: "{{ result.msg | vergeio.vergeos.vergeos_tier_step_unanswered(questions) }}"
'''

RETURN = r'''
_value:
  description:
    - C(true) if the preferred tier was empty and no question supplies it, C(false) otherwise.
  type: bool
'''

from ansible_collections.vergeio.vergeos.plugins.module_utils.recipe_answers import (
    drive_tier_has_no_question,
)


def _tier_gap(message, questions):
    return drive_tier_has_no_question(questions, message)


class FilterModule:
    """Tier-gap recipe filter."""

    def filters(self):
        return {
            'vergeos_tier_step_unanswered': _tier_gap,
        }
