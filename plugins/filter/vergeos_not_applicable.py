# -*- coding: utf-8 -*-

# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
name: vergeos_not_applicable
short_description: Determine whether a recipe cannot be deployed on this system
version_added: "2.3.0"
description:
  - Check whether a recipe cannot be simulated or deployed because it is not
    downloaded or published on this VergeOS system.
  - Evaluates either a recipe row dictionary (from M(vergeio.vergeos.vm_recipe_info))
    or a module failure message.
  - A recipe dictionary with C(downloaded) set to false is considered not applicable.
  - A failure message containing the platform's 'not downloaded or published'
    reason is considered not applicable.
options:
  _input:
    description: A recipe dictionary row, or a module failure message string.
    type: raw
    required: true
  message:
    description:
      - Optional failure message string when C(_input) is a recipe dictionary.
    type: str
    required: false
'''

EXAMPLES = r'''
- name: Check if a recipe is not applicable before deployment
  ansible.builtin.debug:
    msg: "{{ recipe | vergeio.vergeos.vergeos_not_applicable }}"

- name: Check if a failure was due to an un-downloaded recipe
  ansible.builtin.debug:
    msg: "{{ result.msg | vergeio.vergeos.vergeos_not_applicable }}"
'''

RETURN = r'''
_value:
  description:
    - C(true) if the recipe is not applicable or not downloaded, C(false) otherwise.
  type: bool
'''

from ansible_collections.vergeio.vergeos.plugins.module_utils.recipe_answers import (
    recipe_not_applicable,
)


class FilterModule:
    """Not-applicable recipe filter."""

    def filters(self):
        return {
            'vergeos_not_applicable': recipe_not_applicable,
        }
